from __future__ import annotations

from datetime import date

import pytest

import companies.services as company_services
from accounts.models import User
from audit.models import AuditRecord, DataChange, SyncRun
from companies.models import Company
from companies.services import (
    MONITORING_STATUS_RULE_VERSION,
    CompanyServiceError,
    recalculate_company_monitoring_status,
    update_company,
)


@pytest.fixture
def actor(db: object) -> User:
    del db
    return User.objects.create_user(email="monitoring-actor@example.com", password="fixture")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("has_index", "has_watchlist", "identity_pending", "expected"),
    (
        (False, False, False, Company.MonitoringStatus.INACTIVE),
        (True, False, False, Company.MonitoringStatus.ACTIVE),
        (False, True, False, Company.MonitoringStatus.ACTIVE),
        (True, True, False, Company.MonitoringStatus.ACTIVE),
        (False, False, True, Company.MonitoringStatus.PENDING_IDENTITY),
        (True, True, True, Company.MonitoringStatus.PENDING_IDENTITY),
    ),
)
def test_recompute_monitoring_status_truth_table(
    actor: User,
    has_index: bool,
    has_watchlist: bool,
    identity_pending: bool,
    expected: str,
) -> None:
    company = Company.objects.create(
        legal_name=f"Truth {has_index} {has_watchlist} {identity_pending}",
        display_name=f"Truth {has_index} {has_watchlist} {identity_pending}",
    )

    result = recalculate_company_monitoring_status(
        company_id=company.pk,
        as_of=date(2026, 10, 18),
        has_enabled_index_membership=has_index,
        has_active_watchlist=has_watchlist,
        identity_pending=identity_pending,
        actor_user=actor,
        reason="Recompute fixture.",
        request_id=f"truth-{company.pk}",
    )

    company.refresh_from_db()
    assert result.current_status == expected
    assert company.monitoring_status == expected
    assert company.monitoring_recalculated_at == result.recalculated_at
    assert result.audit_record.target_type == AuditRecord.TargetType.COMPANY
    assert {change.change.rule_version for change in result.data_changes if change.change} == {
        MONITORING_STATUS_RULE_VERSION
    }


@pytest.mark.django_db
def test_same_status_recompute_only_repeats_timestamp_semantics(actor: User) -> None:
    company = Company.objects.create(
        legal_name="Same Monitoring Inc",
        display_name="Same Monitoring",
        monitoring_status=Company.MonitoringStatus.ACTIVE,
    )
    first = recalculate_company_monitoring_status(
        company_id=company.pk,
        as_of=date(2026, 10, 18),
        has_enabled_index_membership=True,
        has_active_watchlist=False,
        identity_pending=False,
        actor_user=actor,
        reason="First recompute.",
        request_id="monitoring-same-first",
    )
    second = recalculate_company_monitoring_status(
        company_id=company.pk,
        as_of=date(2026, 10, 18),
        has_enabled_index_membership=True,
        has_active_watchlist=False,
        identity_pending=False,
        actor_user=actor,
        reason="Second recompute.",
        request_id="monitoring-same-second",
    )

    assert first.status_changed is False
    assert second.status_changed is False
    assert second.recalculated_at >= first.recalculated_at
    assert not DataChange.objects.filter(
        target_id=company.pk,
        field_name="monitoring_status",
    ).exists()
    assert (
        DataChange.objects.filter(
            target_id=company.pk,
            field_name="monitoring_recalculated_at",
        ).count()
        == 2
    )
    assert (
        AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=company.pk,
        ).count()
        == 2
    )


@pytest.mark.django_db
def test_recompute_requires_context_and_rolls_back_audit_failure(
    actor: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = Company.objects.create(
        legal_name="Rollback Monitoring Inc",
        display_name="Rollback Monitoring",
    )
    with pytest.raises(CompanyServiceError):
        recalculate_company_monitoring_status(
            company_id=company.pk,
            as_of=date(2026, 10, 18),
            has_enabled_index_membership=False,
            has_active_watchlist=False,
            identity_pending=False,
            reason="Missing actor.",
            request_id="missing-actor",
        )

    def fail_audit(**kwargs: object) -> AuditRecord:
        del kwargs
        raise RuntimeError("fixture audit failure")

    monkeypatch.setattr(company_services, "_record_action", fail_audit)
    with pytest.raises(RuntimeError, match="fixture audit failure"):
        recalculate_company_monitoring_status(
            company_id=company.pk,
            as_of=date(2026, 10, 18),
            has_enabled_index_membership=True,
            has_active_watchlist=False,
            identity_pending=False,
            actor_user=actor,
            reason="Rollback fixture.",
            request_id="monitoring-rollback",
        )

    company.refresh_from_db()
    assert company.monitoring_status == Company.MonitoringStatus.PENDING_IDENTITY
    assert company.monitoring_recalculated_at is None
    assert DataChange.objects.filter(target_id=company.pk).count() == 0


@pytest.mark.django_db
def test_update_company_rejects_monitoring_status_bypass(actor: User) -> None:
    company = Company.objects.create(legal_name="Bypass Inc", display_name="Bypass")

    with pytest.raises(CompanyServiceError, match="unsupported fields"):
        update_company(
            company=company,
            changes={"monitoring_status": Company.MonitoringStatus.ACTIVE},
            actor_user=actor,
            reason="Attempt bypass.",
            request_id="monitoring-bypass",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("reason", "request_id"),
    (
        ("", "monitoring-context"),
        ("Monitoring context", ""),
    ),
)
def test_recompute_rejects_incomplete_actor_context(
    actor: User,
    reason: str,
    request_id: str,
) -> None:
    company = Company.objects.create(
        legal_name="Incomplete Context Inc",
        display_name="Incomplete Context",
    )

    with pytest.raises(CompanyServiceError):
        recalculate_company_monitoring_status(
            company_id=company.pk,
            as_of=date(2026, 10, 18),
            has_enabled_index_membership=True,
            has_active_watchlist=False,
            identity_pending=False,
            actor_user=actor,
            reason=reason,
            request_id=request_id,
        )

    company.refresh_from_db()
    assert company.monitoring_status == Company.MonitoringStatus.PENDING_IDENTITY
    assert company.monitoring_recalculated_at is None
    assert not DataChange.objects.filter(target_id=company.pk).exists()
    assert not AuditRecord.objects.filter(target_id=company.pk).exists()


@pytest.mark.django_db
def test_recompute_supports_sync_run_system_context(company_sync_run: SyncRun) -> None:
    company = Company.objects.create(
        legal_name="System Monitoring Inc",
        display_name="System Monitoring",
    )

    result = recalculate_company_monitoring_status(
        company_id=company.pk,
        as_of=date(2026, 10, 18),
        has_enabled_index_membership=True,
        has_active_watchlist=False,
        identity_pending=False,
        sync_run=company_sync_run,
        reason="",
        request_id="monitoring-system-context",
    )

    company.refresh_from_db()
    assert result.current_status == Company.MonitoringStatus.ACTIVE
    assert company.monitoring_status == Company.MonitoringStatus.ACTIVE
    assert company.monitoring_recalculated_at == result.recalculated_at
    assert result.audit_record.actor_user_id is None
    assert result.audit_record.sync_run_id == company_sync_run.pk
    assert result.data_changes
    assert all(
        entry.change is not None and entry.change.sync_run_id == company_sync_run.pk
        for entry in result.data_changes
    )
