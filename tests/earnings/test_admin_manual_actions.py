"""Stage 7.2 admin manual actions: SEC sync trigger and manual reconciliation."""

from __future__ import annotations

import threading
import uuid

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.test import Client, RequestFactory

from accounts.models import User
from audit.models import AuditRecord, DataSource, SyncRun
from earnings.admin import FilingEarningsLinkAdmin
from earnings.models import (
    EarningsReconciliationDecision,
    FilingEarningsLink,
)
from earnings.services import resolve_earnings_reconciliation_manually
from filings.sync import SEC_JOB_TYPE
from tests.earnings.helpers import make_reconciliation_decision, make_user
from tests.earnings.test_sec_sync_command import _patch_provider, _setup
from tests.filings.test_sec_sync import FixtureTransport

TRIGGER_URL = "/admin/earnings/filingearningslink/trigger-sec-sync/"
RESOLVE_URL = "/admin/earnings/earningsreconciliationdecision/{pk}/resolve/"


def _staff(suffix: str, *codenames: str, app: str = "earnings") -> User:
    user = make_user(suffix)
    user.is_staff = True
    user.save()
    for codename in codenames:
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app, codename=codename)
        )
    return user


@pytest.fixture
def sec_trigger_staff() -> User:
    return _staff(
        "sec-trigger",
        "view_filingearningslink",
        "trigger_sec_sync",
    )


@pytest.fixture
def sec_env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DataSource, object]:
    _company, source, _snapshot = _setup(source_key=f"sec-admin-{uuid.uuid4().hex[:8]}")
    _patch_provider(monkeypatch, FixtureTransport())
    return source, _snapshot


@pytest.mark.django_db
def test_sec_trigger_view_denies_staff_without_permission(client: Client) -> None:
    staff = _staff("sec-viewer", "view_filingearningslink")
    client.force_login(staff)

    response = client.get(TRIGGER_URL)

    assert response.status_code == 403


@pytest.mark.django_db
def test_sec_trigger_view_redirects_non_staff_to_login(client: Client) -> None:
    response = client.get(TRIGGER_URL)

    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


@pytest.mark.django_db
def test_sec_trigger_reason_is_required(
    client: Client, sec_trigger_staff: User, sec_env: tuple[DataSource, object]
) -> None:
    client.force_login(sec_trigger_staff)

    response = client.post(TRIGGER_URL, {"source": str(sec_env[0].pk), "reason": ""})

    assert response.status_code == 200
    assert "reason" in response.context["form"].errors
    assert not SyncRun.objects.filter(job_type=SEC_JOB_TYPE).exists()


@pytest.mark.django_db
def test_sec_trigger_runs_command_and_records_manual_sync_audit(
    client: Client, sec_trigger_staff: User, sec_env: tuple[DataSource, object]
) -> None:
    client.force_login(sec_trigger_staff)

    response = client.post(
        TRIGGER_URL, {"source": str(sec_env[0].pk), "reason": "Ops verification run."}
    )

    assert response.status_code == 302
    run = SyncRun.objects.get(job_type=SEC_JOB_TYPE)
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.idempotency_key.startswith("admin-manual-sec:")
    record = AuditRecord.objects.get(
        action=AuditRecord.Action.MANUAL_SYNC,
        target_type=AuditRecord.TargetType.SYNC_RUN,
        target_id=run.pk,
    )
    assert record.actor_user == sec_trigger_staff
    assert record.reason == "Ops verification run."
    assert record.after is not None
    assert record.after["job_type"] == SEC_JOB_TYPE


@pytest.mark.django_db(transaction=True)
def test_sec_trigger_is_refused_while_another_run_owns_the_lock(
    client: Client, sec_trigger_staff: User, sec_env: tuple[DataSource, object]
) -> None:
    from django.db import connection, connections

    from filings.sync import _sec_run_ownership

    entered = threading.Event()
    release = threading.Event()
    errors: list[object] = []

    def hold_lock() -> None:
        try:
            connections.close_all()
            with _sec_run_ownership():
                entered.set()
                assert release.wait(timeout=10)
        except Exception as error:  # pragma: no cover - diagnostics only
            errors.append(error)
        finally:
            connections.close_all()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert entered.wait(timeout=10)
        client.force_login(sec_trigger_staff)
        response = client.post(
            TRIGGER_URL,
            {"source": str(sec_env[0].pk), "reason": "Should be refused."},
        )
        assert response.status_code == 200
        assert "owns the global SEC filing job" in str(response.context["form"].non_field_errors())
        assert not SyncRun.objects.filter(job_type=SEC_JOB_TYPE).exists()
        assert not AuditRecord.objects.filter(action=AuditRecord.Action.MANUAL_SYNC).exists()
    finally:
        release.set()
        holder.join(timeout=15)
    connection.close()
    assert not errors


@pytest.mark.django_db
def test_sec_trigger_action_hidden_without_permission() -> None:
    admin_instance = FilingEarningsLinkAdmin(FilingEarningsLink, AdminSite())
    request = RequestFactory().get("/admin/earnings/filingearningslink/")
    request.user = _staff("sec-hidden", "view_filingearningslink")
    assert "trigger_sec_sync" not in admin_instance.get_actions(request)

    privileged = RequestFactory().get("/admin/earnings/filingearningslink/")
    privileged.user = _staff("sec-shown", "view_filingearningslink", "trigger_sec_sync")
    assert "trigger_sec_sync" in admin_instance.get_actions(privileged)


# ---------------------------------------------------------------------------
# Manual reconciliation decision


def _open_conflict_decision() -> EarningsReconciliationDecision:
    # system-produced open decisions carry a sync run and no actor (model
    # context constraint); the admin action supplies the actor + reason
    return make_reconciliation_decision(
        decision_type="conflict",
        status="open",
        target_event=None,
    )


@pytest.fixture
def resolve_staff() -> User:
    return _staff(
        "resolve",
        "view_earningsreconciliationdecision",
        "manual_reconciliation",
    )


@pytest.mark.django_db
def test_resolve_records_decision_with_actor_and_reason(
    client: Client, resolve_staff: User
) -> None:
    decision = _open_conflict_decision()
    client.force_login(resolve_staff)

    response = client.post(
        RESOLVE_URL.format(pk=decision.pk),
        {"decision_type": "no_match", "reason": "Source row refers to another company."},
    )

    assert response.status_code == 302
    manual = (
        EarningsReconciliationDecision.objects.filter(
            observation=decision.observation,
            actor_user=resolve_staff,
        )
        .exclude(pk=decision.pk)
        .get()
    )
    assert manual.decision_type == "no_match"
    assert manual.status == "rejected"
    assert manual.reason == "Source row refers to another company."
    assert manual.request_id.startswith("admin-reconciliation:")
    assert AuditRecord.objects.filter(
        target_type=AuditRecord.TargetType.EARNINGS_RECONCILIATION_DECISION,
        target_id=manual.pk,
        actor_user=resolve_staff,
    ).exists()


@pytest.mark.django_db
def test_resolve_replay_with_same_request_id_is_idempotent(
    resolve_staff: User,
) -> None:
    observation = _open_conflict_decision().observation

    first = resolve_earnings_reconciliation_manually(
        observation=observation,
        decision_type="no_match",
        actor_user=resolve_staff,
        reason="Duplicate replay probe.",
        request_id="replay-probe-1",
    )
    second = resolve_earnings_reconciliation_manually(
        observation=observation,
        decision_type="no_match",
        actor_user=resolve_staff,
        reason="Duplicate replay probe.",
        request_id="replay-probe-1",
    )

    assert second.decision.pk == first.decision.pk
    assert second.decision_created is False
    assert (
        EarningsReconciliationDecision.objects.filter(
            observation=observation, actor_user=resolve_staff
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_resolve_reason_is_required(client: Client, resolve_staff: User) -> None:
    decision = _open_conflict_decision()
    client.force_login(resolve_staff)

    response = client.post(
        RESOLVE_URL.format(pk=decision.pk),
        {"decision_type": "no_match", "reason": ""},
    )

    assert response.status_code == 200
    assert "reason" in response.context["form"].errors
    assert not EarningsReconciliationDecision.objects.filter(actor_user=resolve_staff).exists()


@pytest.mark.django_db
def test_resolve_binding_decision_requires_target(client: Client, resolve_staff: User) -> None:
    decision = _open_conflict_decision()
    client.force_login(resolve_staff)

    response = client.post(
        RESOLVE_URL.format(pk=decision.pk),
        {"decision_type": "matched_canonical", "reason": "Missing target probe."},
    )

    assert response.status_code == 200
    assert "target_event_id" in response.context["form"].errors


@pytest.mark.django_db
def test_resolve_denies_staff_without_permission(client: Client) -> None:
    decision = _open_conflict_decision()
    staff = _staff("resolve-viewer", "view_earningsreconciliationdecision")
    client.force_login(staff)

    response = client.post(
        RESOLVE_URL.format(pk=decision.pk),
        {"decision_type": "no_match", "reason": "Should be denied."},
    )

    assert response.status_code == 403
    assert not EarningsReconciliationDecision.objects.filter(actor_user=staff).exists()


@pytest.mark.django_db
def test_only_open_decisions_can_be_resolved(client: Client, resolve_staff: User) -> None:
    decision = make_reconciliation_decision()  # default resolved matched_canonical
    client.force_login(resolve_staff)

    response = client.post(
        RESOLVE_URL.format(pk=decision.pk),
        {"decision_type": "no_match", "reason": "Should not run."},
    )

    assert response.status_code == 302
    assert response["Location"].endswith("/admin/earnings/earningsreconciliationdecision/")
    assert not EarningsReconciliationDecision.objects.filter(actor_user=resolve_staff).exists()


@pytest.mark.django_db
def test_resolve_action_requires_exactly_one_selection(client: Client, resolve_staff: User) -> None:
    first = _open_conflict_decision()
    second = _open_conflict_decision()
    client.force_login(resolve_staff)

    response = client.post(
        "/admin/earnings/earningsreconciliationdecision/",
        {
            "action": "resolve_manually",
            "_selected_action": [str(first.pk), str(second.pk)],
        },
    )

    assert response.status_code == 302
    assert response["Location"].endswith("/admin/earnings/earningsreconciliationdecision/")


@pytest.mark.django_db
def test_conflict_queue_filter_returns_only_open_conflicts(
    client: Client, resolve_staff: User
) -> None:
    conflict = _open_conflict_decision()
    make_reconciliation_decision()  # resolved matched_canonical row
    client.force_login(resolve_staff)

    response = client.get(
        "/admin/earnings/earningsreconciliationdecision/",
        {"decision_type__exact": "conflict", "status__exact": "open"},
    )

    assert response.status_code == 200
    changelist = response.context["cl"]
    assert [obj.pk for obj in changelist.queryset] == [conflict.pk]
