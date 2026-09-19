# mypy: ignore-errors
"""Service tests for the audited EarningsEvent status lifecycle."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.db import OperationalError, connection, transaction

from accounts.models import User
from audit.models import AuditRecord, DataChange
from earnings.models import EarningsDatePrecision, EarningsEvent, EventStatus
from earnings.services import (
    EARNINGS_STATUS_LIFECYCLE_RULE_VERSION,
    EarningsStatusIdentityUncertain,
    EarningsStatusServiceError,
    InvalidEarningsStatusContext,
    InvalidEarningsStatusEvidence,
    InvalidEarningsStatusReinstatement,
    InvalidEarningsStatusTransition,
    InvalidEarningsTargetStatus,
    cancel_earnings_event,
    confirm_earnings_event,
    correct_earnings_status,
    mark_earnings_released,
    reinstate_earnings_event,
    transition_earnings_status,
)
from tests.earnings.helpers import make_event, make_source_evidence, make_sync_run

_ESTIMATED = EventStatus.SCHEDULED_ESTIMATED
_CONFIRMED = EventStatus.SCHEDULED_CONFIRMED
_RELEASED = EventStatus.RELEASED
_CANCELLED = EventStatus.CANCELLED

_STATUS_TOKENS = {
    _ESTIMATED: "est",
    _CONFIRMED: "conf",
    _RELEASED: "rel",
    _CANCELLED: "canc",
}

_SCHEDULE_FIELDS = (
    "estimated_release_date",
    "confirmed_release_date",
    "earnings_release_date",
    "conference_call_date",
    "estimated_release_at",
    "confirmed_release_at",
    "earnings_release_at",
    "conference_call_at",
    "estimated_release_precision",
    "confirmed_release_precision",
    "earnings_release_precision",
    "conference_call_precision",
    "release_session",
)


def _event(status: str, **overrides: object) -> EarningsEvent:
    return make_event(status=status, **overrides)


def _history_counts(event: EarningsEvent) -> tuple[int, int]:
    return (
        DataChange.objects.filter(
            target_type=DataChange.TargetType.EARNINGS_EVENT,
            target_id=event.pk,
            field_name="status",
        ).count(),
        AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.EARNINGS_EVENT,
            target_id=event.pk,
        ).count(),
    )


def _sync(suffix: str):
    return make_sync_run(f"{suffix}-{uuid.uuid4().hex[:8]}")


def _schedule_snapshot(event: EarningsEvent) -> dict[str, object]:
    event.refresh_from_db()
    return {field_name: getattr(event, field_name) for field_name in _SCHEDULE_FIELDS}


@pytest.mark.django_db
class TestNormalTransitionMatrix:
    @pytest.mark.parametrize(
        ("initial", "target"),
        (
            (_ESTIMATED, _CONFIRMED),
            (_ESTIMATED, _RELEASED),
            (_ESTIMATED, _CANCELLED),
            (_CONFIRMED, _RELEASED),
            (_CONFIRMED, _CANCELLED),
        ),
    )
    def test_normal_transition_applies_and_audits(self, initial: str, target: str) -> None:
        event = _event(initial)
        sync_run = _sync(f"{_STATUS_TOKENS[initial]}-{_STATUS_TOKENS[target]}")

        if target == _CANCELLED:
            result = cancel_earnings_event(
                earnings_event=event,
                affirmative_cancellation=True,
                sync_run=sync_run,
            )
        else:
            result = transition_earnings_status(
                earnings_event=event,
                target_status=target,
                sync_run=sync_run,
            )

        event.refresh_from_db()
        assert result.changed is True
        assert event.status == target
        assert result.previous_status == initial
        assert result.status == target
        assert _history_counts(event) == (1, 1)

    def test_direct_estimated_to_released_does_not_fabricate_confirmation(self) -> None:
        event = _event(_ESTIMATED)
        result = mark_earnings_released(
            earnings_event=event,
            sync_run=_sync("direct-release"),
        )

        assert result.previous_status == _ESTIMATED
        assert result.status == _RELEASED
        assert result.earnings_event.confirmed_release_precision == EarningsDatePrecision.UNKNOWN
        assert result.audit_record is not None
        assert result.audit_record.before == {"status": _ESTIMATED}
        assert result.audit_record.after == {"status": _RELEASED}
        assert result.audit_record.action == AuditRecord.Action.UPDATE

    def test_generic_transition_rejects_cancellation(self) -> None:
        event = _event(_ESTIMATED)

        with pytest.raises(InvalidEarningsStatusTransition, match="cancel_earnings_event"):
            transition_earnings_status(
                earnings_event=event,
                target_status=_CANCELLED,
                sync_run=_sync("generic-cancel"),
            )

        event.refresh_from_db()
        assert event.status == _ESTIMATED
        assert _history_counts(event) == (0, 0)

    def test_confirm_wrapper_uses_manual_audit_action(self) -> None:
        event = _event(_ESTIMATED)
        actor = User.objects.create_user(
            email="lifecycle-confirm@example.com",
            password="test-password-only",
        )

        result = confirm_earnings_event(
            earnings_event=event,
            actor_user=actor,
            reason="IR confirmed the date.",
            request_id="confirm-request-1",
        )

        event.refresh_from_db()
        assert event.status == _CONFIRMED
        assert result.audit_record is not None
        assert result.audit_record.action == AuditRecord.Action.UPDATE
        assert result.audit_record.actor_user_id == actor.pk


@pytest.mark.django_db
class TestSameStateNoOp:
    @pytest.mark.parametrize("status", (_ESTIMATED, _CONFIRMED, _RELEASED, _CANCELLED))
    def test_same_status_is_idempotent_no_op(self, status: str) -> None:
        event = _event(status)
        before = _history_counts(event)
        actor = User.objects.create_user(
            email=f"noop-{status}@example.com",
            password="test-password-only",
        )

        result = correct_earnings_status(
            earnings_event=event,
            target_status=status,
            actor_user=actor,
            reason="Retry of the recorded fact.",
            request_id=f"same-state-{status}",
        )

        assert result.changed is False
        assert result.data_change is None
        assert result.audit_record is None
        assert _history_counts(event) == before

    def test_same_state_replay_after_real_transition_writes_no_second_history(self) -> None:
        event = _event(_ESTIMATED)
        sync_run = _sync("same-state-replay")
        first = confirm_earnings_event(earnings_event=event, sync_run=sync_run)
        assert first.changed is True
        first_counts = _history_counts(event)

        second = confirm_earnings_event(earnings_event=event, sync_run=sync_run)

        assert second.changed is False
        assert _history_counts(event) == first_counts


@pytest.mark.django_db
class TestTerminalAndPathFailClosed:
    @pytest.mark.parametrize(
        ("initial", "target"),
        (
            (_RELEASED, _ESTIMATED),
            (_RELEASED, _CONFIRMED),
            (_RELEASED, _CANCELLED),
            (_CANCELLED, _ESTIMATED),
            (_CANCELLED, _CONFIRMED),
            (_CANCELLED, _RELEASED),
        ),
    )
    def test_normal_api_rejects_terminal_reverse_transition(
        self,
        initial: str,
        target: str,
    ) -> None:
        event = _event(initial)
        before = _history_counts(event)

        with pytest.raises(InvalidEarningsStatusTransition):
            transition_earnings_status(
                earnings_event=event,
                target_status=target,
                sync_run=_sync(f"term-{_STATUS_TOKENS[initial]}-{_STATUS_TOKENS[target]}"),
            )

        event.refresh_from_db()
        assert event.status == initial
        assert _history_counts(event) == before

    def test_correction_api_rejects_unknown_target(self) -> None:
        event = _event(_CONFIRMED)
        actor = User.objects.create_user(
            email="invalid-target@example.com",
            password="test-password-only",
        )

        with pytest.raises(InvalidEarningsTargetStatus):
            correct_earnings_status(
                earnings_event=event,
                target_status="filed",
                actor_user=actor,
                reason="Bad target.",
                request_id="invalid-target-1",
            )

        event.refresh_from_db()
        assert event.status == _CONFIRMED

    def test_reinstatement_api_rejects_non_cancelled_source(self) -> None:
        event = _event(_ESTIMATED)
        actor = User.objects.create_user(
            email="bad-reinstate@example.com",
            password="test-password-only",
        )

        with pytest.raises(InvalidEarningsStatusReinstatement):
            reinstate_earnings_event(
                earnings_event=event,
                target_status=_RELEASED,
                actor_user=actor,
                reason="Not actually cancelled.",
                request_id="bad-reinstate-1",
            )

        event.refresh_from_db()
        assert event.status == _ESTIMATED


@pytest.mark.django_db
class TestCorrectionPath:
    @pytest.mark.parametrize(
        ("initial", "target"),
        (
            (_CONFIRMED, _ESTIMATED),
            (_RELEASED, _CONFIRMED),
            (_RELEASED, _ESTIMATED),
            (_RELEASED, _CANCELLED),
            (_CANCELLED, _ESTIMATED),
            (_CANCELLED, _CONFIRMED),
            (_CANCELLED, _RELEASED),
        ),
    )
    def test_correction_transition_is_audited_as_manual_correction(
        self,
        initial: str,
        target: str,
    ) -> None:
        event = _event(initial)
        actor = User.objects.create_user(
            email=f"correction-{initial}-{target}@example.com",
            password="test-password-only",
        )

        result = correct_earnings_status(
            earnings_event=event,
            target_status=target,
            actor_user=actor,
            reason="The recorded status was a false fact.",
            request_id=f"correction-{initial}-{target}",
        )

        event.refresh_from_db()
        assert event.status == target
        assert result.changed is True
        assert result.audit_record is not None
        assert result.audit_record.action == AuditRecord.Action.MANUAL_CORRECTION
        assert result.audit_record.before == {"status": initial}
        assert result.audit_record.after == {"status": target}
        assert _history_counts(event) == (1, 1)

    def test_correction_requires_actor(self) -> None:
        event = _event(_CONFIRMED)

        with pytest.raises(TypeError):
            correct_earnings_status(  # type: ignore[call-arg]
                earnings_event=event,
                target_status=_ESTIMATED,
                reason="Missing actor.",
                request_id="correction-no-actor",
            )

        event.refresh_from_db()
        assert event.status == _CONFIRMED
        assert _history_counts(event) == (0, 0)

    @pytest.mark.parametrize("missing", ("reason", "request_id"))
    def test_correction_requires_reason_and_request_identity(self, missing: str) -> None:
        event = _event(_CONFIRMED)
        actor = User.objects.create_user(
            email=f"correction-missing-{missing}@example.com",
            password="test-password-only",
        )
        kwargs: dict[str, Any] = {
            "earnings_event": event,
            "target_status": _ESTIMATED,
            "actor_user": actor,
            "reason": "The recorded status was a false fact.",
            "request_id": f"correction-missing-{missing}",
        }
        kwargs[missing] = ""

        with pytest.raises(InvalidEarningsStatusContext):
            correct_earnings_status(**kwargs)

        event.refresh_from_db()
        assert event.status == _CONFIRMED
        assert _history_counts(event) == (0, 0)

    def test_correction_retry_is_idempotent(self) -> None:
        event = _event(_RELEASED)
        actor = User.objects.create_user(
            email="correction-retry@example.com",
            password="test-password-only",
        )
        first = correct_earnings_status(
            earnings_event=event,
            target_status=_CONFIRMED,
            actor_user=actor,
            reason="The release was recorded too early.",
            request_id="correction-retry-1",
        )
        first_counts = _history_counts(event)

        second = correct_earnings_status(
            earnings_event=event,
            target_status=_CONFIRMED,
            actor_user=actor,
            reason="The release was recorded too early.",
            request_id="correction-retry-1",
        )

        assert first.changed is True
        assert second.changed is False
        assert _history_counts(event) == first_counts


@pytest.mark.django_db
class TestReinstatement:
    @pytest.mark.parametrize(
        "target",
        (_ESTIMATED, _CONFIRMED, _RELEASED),
    )
    def test_manual_reinstatement_reuses_same_event_and_identity(self, target: str) -> None:
        event = _event(_CANCELLED)
        original_identity = (
            event.identity_status,
            event.identity_key,
            event.identity_rule_version,
            event.period_end_date,
            event.period_type,
        )
        actor = User.objects.create_user(
            email=f"reinstate-{target}@example.com",
            password="test-password-only",
        )

        result = reinstate_earnings_event(
            earnings_event=event,
            target_status=target,
            actor_user=actor,
            reason="The same event was rescheduled.",
            request_id=f"reinstate-{target}",
        )

        event.refresh_from_db()
        assert result.earnings_event.pk == event.pk
        assert event.status == target
        assert (
            event.identity_status,
            event.identity_key,
            event.identity_rule_version,
            event.period_end_date,
            event.period_type,
        ) == original_identity
        assert result.audit_record is not None
        assert result.audit_record.action == AuditRecord.Action.UPDATE

    def test_source_backed_reinstatement_links_evidence(self) -> None:
        event = _event(_CANCELLED)
        sync_run, evidence = make_source_evidence(
            event=event,
            field_name="status",
            normalized_value=_CONFIRMED,
            suffix="reinstatement-evidence",
        )

        result = reinstate_earnings_event(
            earnings_event=event,
            target_status=_CONFIRMED,
            source_evidence=evidence,
        )

        assert result.changed is True
        assert result.data_change is not None
        assert result.data_change.change.source_evidence_id == evidence.pk
        assert result.audit_record is not None
        assert result.audit_record.sync_run_id == sync_run.pk

    def test_manual_reinstatement_requires_context(self) -> None:
        event = _event(_CANCELLED)

        with pytest.raises(InvalidEarningsStatusContext):
            reinstate_earnings_event(
                earnings_event=event,
                target_status=_CONFIRMED,
            )

        event.refresh_from_db()
        assert event.status == _CANCELLED
        assert _history_counts(event) == (0, 0)

    def test_reinstatement_requires_cancelled_current_state(self) -> None:
        event = _event(_ESTIMATED)
        actor = User.objects.create_user(
            email="reinstate-invalid-current@example.com",
            password="test-password-only",
        )

        with pytest.raises(InvalidEarningsStatusReinstatement):
            reinstate_earnings_event(
                earnings_event=event,
                target_status=_RELEASED,
                actor_user=actor,
                reason="Not cancelled.",
                request_id="reinstate-invalid-current",
            )

        event.refresh_from_db()
        assert event.status == _ESTIMATED

    def test_reinstatement_to_cancelled_target_is_idempotent_no_op(self) -> None:
        event = _event(_CANCELLED)
        actor = User.objects.create_user(
            email="reinstate-cancelled-target@example.com",
            password="test-password-only",
        )

        result = reinstate_earnings_event(
            earnings_event=event,
            target_status=_CANCELLED,
            actor_user=actor,
            reason="Retry of the recorded cancellation.",
            request_id="reinstate-cancelled-target",
        )

        assert result.changed is False
        assert _history_counts(event) == (0, 0)

    def test_reinstatement_retry_is_idempotent(self) -> None:
        event = _event(_CANCELLED)
        actor = User.objects.create_user(
            email="reinstate-retry@example.com",
            password="test-password-only",
        )
        first = reinstate_earnings_event(
            earnings_event=event,
            target_status=_ESTIMATED,
            actor_user=actor,
            reason="Same event reappeared.",
            request_id="reinstate-retry-1",
        )
        first_counts = _history_counts(event)

        second = reinstate_earnings_event(
            earnings_event=event,
            target_status=_ESTIMATED,
            actor_user=actor,
            reason="Same event reappeared.",
            request_id="reinstate-retry-1",
        )

        assert first.changed is True
        assert second.changed is False
        assert _history_counts(event) == first_counts

    def test_reinstatement_can_target_released_without_intermediate_status(self) -> None:
        event = _event(_CANCELLED)
        actor = User.objects.create_user(
            email="reinstate-direct-release@example.com",
            password="test-password-only",
        )

        result = reinstate_earnings_event(
            earnings_event=event,
            target_status=_RELEASED,
            actor_user=actor,
            reason="The event had already released.",
            request_id="reinstate-direct-release",
        )

        assert result.previous_status == _CANCELLED
        assert result.status == _RELEASED
        history = DataChange.objects.filter(
            target_type=DataChange.TargetType.EARNINGS_EVENT,
            target_id=event.pk,
            field_name="status",
        )
        assert history.count() == 1
        assert history.get().new_value == _RELEASED


@pytest.mark.django_db
class TestCancellationGuard:
    def test_same_state_cancellation_retry_is_no_op_before_affirmative_guard(self) -> None:
        event = _event(_CANCELLED)
        before = _history_counts(event)

        result = cancel_earnings_event(earnings_event=event)

        assert result.changed is False
        assert result.status == _CANCELLED
        assert _history_counts(event) == before

    def test_automatic_cancellation_requires_affirmative_intent(self) -> None:
        event = _event(_ESTIMATED)
        sync_run, evidence = make_source_evidence(
            event=event,
            field_name="status",
            normalized_value=_CANCELLED,
            suffix="missing-affirmative",
        )

        with pytest.raises(InvalidEarningsStatusContext, match="affirmative_cancellation"):
            cancel_earnings_event(
                earnings_event=event,
                source_evidence=evidence,
            )

        event.refresh_from_db()
        assert event.status == _ESTIMATED
        assert _history_counts(event) == (0, 0)
        assert sync_run.pk == evidence.sync_run_id

    def test_manual_cancellation_carries_intent_through_actor_context(self) -> None:
        event = _event(_CONFIRMED)
        actor = User.objects.create_user(
            email="manual-cancel@example.com",
            password="test-password-only",
        )

        result = cancel_earnings_event(
            earnings_event=event,
            actor_user=actor,
            reason="Company IR explicitly cancelled the event.",
            request_id="manual-cancel-1",
        )

        event.refresh_from_db()
        assert event.status == _CANCELLED
        assert result.audit_record is not None
        assert result.audit_record.action == AuditRecord.Action.UPDATE

    def test_cancellation_does_not_clear_or_modify_schedule(self) -> None:
        event = _event(
            _CONFIRMED,
            estimated_release_date=date(2026, 10, 24),
            estimated_release_precision=EarningsDatePrecision.DATE_ONLY,
            confirmed_release_date=date(2026, 10, 25),
            confirmed_release_precision=EarningsDatePrecision.DATE_ONLY,
            earnings_release_at=datetime(2026, 10, 25, 20, 30, tzinfo=UTC),
            earnings_release_precision=EarningsDatePrecision.EXACT_DATETIME,
            conference_call_date=date(2026, 10, 25),
            conference_call_precision=EarningsDatePrecision.DATE_ONLY,
            release_session="after_market",
        )
        before = _schedule_snapshot(event)
        actor = User.objects.create_user(
            email="cancel-schedule@example.com",
            password="test-password-only",
        )

        cancel_earnings_event(
            earnings_event=event,
            actor_user=actor,
            reason="Company IR explicitly cancelled the event.",
            request_id="cancel-schedule-1",
        )

        assert _schedule_snapshot(event) == before

    def test_normal_transition_does_not_require_schedule_facts(self) -> None:
        event = _event(_ESTIMATED)

        result = transition_earnings_status(
            earnings_event=event,
            target_status=_CONFIRMED,
            sync_run=make_sync_run("no-schedule-confirmation"),
        )

        assert result.changed is True
        assert result.earnings_event.confirmed_release_date is None
        assert result.earnings_event.confirmed_release_at is None
        assert result.earnings_event.confirmed_release_precision == EarningsDatePrecision.UNKNOWN


@pytest.mark.django_db
class TestEvidenceAndContext:
    def test_source_evidence_is_linked_through_data_change(self) -> None:
        event = _event(_ESTIMATED)
        sync_run, evidence = make_source_evidence(
            event=event,
            field_name="status",
            normalized_value=_CONFIRMED,
            suffix="status-evidence",
        )

        result = confirm_earnings_event(
            earnings_event=event,
            source_evidence=evidence,
        )

        assert result.data_change is not None
        assert result.data_change.change.source_evidence_id == evidence.pk
        assert result.audit_record is not None
        assert result.audit_record.sync_run_id == sync_run.pk

    def test_wrong_evidence_field_is_rejected(self) -> None:
        event = _event(_ESTIMATED)
        _, evidence = make_source_evidence(
            event=event,
            field_name="estimated_release",
            normalized_value={
                "kind": "date",
                "precision": "date_only",
                "value": "2026-10-24",
            },
            suffix="wrong-status-field",
        )

        with pytest.raises(InvalidEarningsStatusEvidence, match="changed domain field"):
            confirm_earnings_event(
                earnings_event=event,
                source_evidence=evidence,
            )

        event.refresh_from_db()
        assert event.status == _ESTIMATED

    def test_wrong_event_evidence_is_rejected(self) -> None:
        event = _event(_ESTIMATED)
        other_event = make_event(period_end_date=date(2026, 6, 30))
        _, evidence = make_source_evidence(
            event=other_event,
            field_name="status",
            normalized_value=_CONFIRMED,
            suffix="wrong-status-event",
        )

        with pytest.raises(InvalidEarningsStatusEvidence, match="same domain target"):
            confirm_earnings_event(
                earnings_event=event,
                source_evidence=evidence,
            )

        event.refresh_from_db()
        assert event.status == _ESTIMATED

    def test_mutation_without_actor_or_machine_provenance_is_rejected(self) -> None:
        event = _event(_ESTIMATED)

        with pytest.raises(InvalidEarningsStatusContext, match="actor or machine provenance"):
            confirm_earnings_event(earnings_event=event)

        event.refresh_from_db()
        assert event.status == _ESTIMATED
        assert _history_counts(event) == (0, 0)

    @pytest.mark.parametrize("missing", ("reason", "request_id"))
    def test_manual_normal_transition_requires_context(self, missing: str) -> None:
        event = _event(_ESTIMATED)
        actor = User.objects.create_user(
            email=f"normal-missing-{missing}@example.com",
            password="test-password-only",
        )
        kwargs: dict[str, Any] = {
            "earnings_event": event,
            "actor_user": actor,
            "reason": "IR confirmed the date.",
            "request_id": f"normal-missing-{missing}",
        }
        kwargs[missing] = ""

        with pytest.raises(InvalidEarningsStatusContext):
            confirm_earnings_event(**kwargs)

        event.refresh_from_db()
        assert event.status == _ESTIMATED
        assert _history_counts(event) == (0, 0)


@pytest.mark.django_db
class TestAuditHistoryContract:
    def test_data_change_uses_status_rule_version_and_canonical_strings(self) -> None:
        event = _event(_ESTIMATED)
        result = confirm_earnings_event(
            earnings_event=event,
            sync_run=make_sync_run("audit-contract"),
        )

        assert result.data_change is not None
        change = result.data_change.change
        assert change is not None
        assert change.target_type == DataChange.TargetType.EARNINGS_EVENT
        assert change.target_id == event.pk
        assert change.field_name == "status"
        assert change.old_value == _ESTIMATED
        assert change.new_value == _CONFIRMED
        assert change.rule_version == EARNINGS_STATUS_LIFECYCLE_RULE_VERSION

        assert result.audit_record is not None
        assert result.audit_record.before == {"status": _ESTIMATED}
        assert result.audit_record.after == {"status": _CONFIRMED}
        assert set(result.audit_record.before) == {"status"}
        assert set(result.audit_record.after) == {"status"}

    def test_manual_status_mutation_records_actor_reason_and_request(self) -> None:
        event = _event(_ESTIMATED)
        actor = User.objects.create_user(
            email="manual-audit-contract@example.com",
            password="test-password-only",
        )
        result = confirm_earnings_event(
            earnings_event=event,
            actor_user=actor,
            reason="IR confirmed the date.",
            request_id="manual-audit-1",
        )

        assert result.audit_record is not None
        assert result.audit_record.actor_user_id == actor.pk
        assert result.audit_record.reason == "IR confirmed the date."
        assert result.audit_record.request_id == "manual-audit-1"
        assert result.data_change is not None
        assert result.data_change.change is not None
        assert result.data_change.change.origin_key == "manual-audit-1"


@pytest.mark.django_db
class TestTransactions:
    def test_audit_failure_rolls_back_status_and_data_change(self, monkeypatch) -> None:
        from earnings.services import lifecycle as lifecycle_module

        event = _event(_ESTIMATED)

        def fail_audit(**_kwargs: object) -> AuditRecord:
            raise RuntimeError("forced audit failure")

        monkeypatch.setattr(lifecycle_module, "record_system_action", fail_audit)

        with pytest.raises(RuntimeError, match="forced audit failure"):
            confirm_earnings_event(
                earnings_event=event,
                sync_run=make_sync_run("rollback-audit"),
            )

        event.refresh_from_db()
        assert event.status == _ESTIMATED
        assert _history_counts(event) == (0, 0)

    def test_data_change_failure_rolls_back_status(self, monkeypatch) -> None:
        from earnings.services import lifecycle as lifecycle_module

        event = _event(_ESTIMATED)

        def fail_data_change(**_kwargs: object) -> object:
            raise RuntimeError("forced data change failure")

        monkeypatch.setattr(lifecycle_module, "record_data_change", fail_data_change)

        with pytest.raises(RuntimeError, match="forced data change failure"):
            confirm_earnings_event(
                earnings_event=event,
                sync_run=make_sync_run("rollback-data-change"),
            )

        event.refresh_from_db()
        assert event.status == _ESTIMATED
        assert _history_counts(event) == (0, 0)

    def test_failed_inner_call_rolls_back_successful_schedule_mutation(self) -> None:
        from earnings.services import update_earnings_schedule

        event = _event(_ESTIMATED)
        sync_run = make_sync_run("outer-transaction-inner-failure")

        with pytest.raises(InvalidEarningsStatusTransition):
            with transaction.atomic():
                update_earnings_schedule(
                    earnings_event=event,
                    changes={"estimated_release": date(2026, 10, 24)},
                    sync_run=sync_run,
                )
                transition_earnings_status(
                    earnings_event=event,
                    target_status=_CONFIRMED,
                    sync_run=sync_run,
                )
                # A normal transition may not move the newly confirmed event
                # backwards; the inner service must fail and roll back 4.1B.
                transition_earnings_status(
                    earnings_event=event,
                    target_status=_ESTIMATED,
                    sync_run=sync_run,
                )

        event.refresh_from_db()
        assert event.status == _ESTIMATED
        assert event.estimated_release_date is None
        assert event.estimated_release_precision == EarningsDatePrecision.UNKNOWN
        assert (
            DataChange.objects.filter(
                target_id=event.pk,
                field_name__in=("status", "estimated_release"),
            ).count()
            == 0
        )

    def test_outer_transaction_failure_rolls_back_schedule_and_status(self) -> None:
        from earnings.services import update_earnings_schedule

        event = _event(_ESTIMATED)
        sync_run = make_sync_run("outer-transaction-both-rollback")

        with pytest.raises(RuntimeError, match="forced outer failure"):
            with transaction.atomic():
                update_earnings_schedule(
                    earnings_event=event,
                    changes={"estimated_release": date(2026, 10, 24)},
                    sync_run=sync_run,
                )
                confirm_earnings_event(
                    earnings_event=event,
                    sync_run=sync_run,
                )
                raise RuntimeError("forced outer failure")

        event.refresh_from_db()
        assert event.status == _ESTIMATED
        assert event.estimated_release_date is None
        assert _history_counts(event) == (0, 0)

    def test_stale_caller_instance_does_not_supply_old_status(self) -> None:
        event = _event(_ESTIMATED)
        stale = EarningsEvent.objects.get(pk=event.pk)
        sync_run = make_sync_run("stale-status")
        confirm_earnings_event(earnings_event=event, sync_run=sync_run)
        assert stale.status == _ESTIMATED

        result = mark_earnings_released(
            earnings_event=stale,
            sync_run=sync_run,
        )

        assert result.previous_status == _CONFIRMED
        data_change = DataChange.objects.filter(
            target_type=DataChange.TargetType.EARNINGS_EVENT,
            target_id=event.pk,
            field_name="status",
            new_value=_RELEASED,
        ).get()
        assert data_change.old_value == _CONFIRMED


@pytest.mark.django_db(transaction=True)
class TestLifecycleConcurrency:
    def _run_concurrently(
        self,
        *,
        event: EarningsEvent,
        worker_a: Callable[..., object],
        worker_b: Callable[..., object],
    ) -> tuple[list[object], list[BaseException]]:
        from django.db import close_old_connections, connections

        barrier = threading.Barrier(2, timeout=10)
        results: list[object] = []
        errors: list[BaseException] = []

        def run(worker: Callable[..., object]) -> None:
            close_old_connections()
            try:
                barrier.wait()
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '5s'")
                    results.append(
                        worker(
                            earnings_event=EarningsEvent.objects.get(pk=event.pk),
                        )
                    )
            except (EarningsStatusServiceError, OperationalError) as error:
                errors.append(error)
            finally:
                for conn in connections.all():
                    conn.close()

        threads = [
            threading.Thread(target=run, args=(worker_a,)),
            threading.Thread(target=run, args=(worker_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        for thread in threads:
            assert not thread.is_alive(), "Concurrent lifecycle thread hung"

        return results, errors

    def test_concurrent_same_transition_is_idempotent(self) -> None:
        event = _event(_ESTIMATED)
        sync_run = make_sync_run("concurrent-same")

        def worker(*, earnings_event: EarningsEvent):
            return confirm_earnings_event(
                earnings_event=earnings_event,
                sync_run=sync_run,
            )

        results, errors = self._run_concurrently(
            event=event,
            worker_a=worker,
            worker_b=worker,
        )

        assert errors == []
        assert len(results) == 2
        assert sum(1 for result in results if result.changed) == 1
        event.refresh_from_db()
        assert event.status == _CONFIRMED
        assert _history_counts(event) == (1, 1)

    def test_concurrent_conflicting_transition_rechecks_locked_state(self) -> None:
        event = _event(_ESTIMATED)
        sync_run = make_sync_run("concurrent-conflict")

        def release_worker(*, earnings_event: EarningsEvent):
            return mark_earnings_released(
                earnings_event=earnings_event,
                sync_run=sync_run,
            )

        def cancel_worker(*, earnings_event: EarningsEvent):
            return cancel_earnings_event(
                earnings_event=earnings_event,
                affirmative_cancellation=True,
                sync_run=sync_run,
            )

        results, errors = self._run_concurrently(
            event=event,
            worker_a=release_worker,
            worker_b=cancel_worker,
        )

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], InvalidEarningsStatusTransition)

        event.refresh_from_db()
        assert event.status in (_CANCELLED, _RELEASED)
        assert _history_counts(event) == (1, 1)

    def test_unpersisted_event_fails_closed(self) -> None:
        event = EarningsEvent(
            company_id=uuid.uuid4(),
            status=_ESTIMATED,
            identity_status="candidate",
        )
        actor = User.objects.create_user(
            email="unpersisted-lifecycle@example.com",
            password="test-password-only",
        )

        with pytest.raises(EarningsStatusIdentityUncertain):
            correct_earnings_status(
                earnings_event=event,
                target_status=_CONFIRMED,
                actor_user=actor,
                reason="Cannot use unsaved event.",
                request_id="unpersisted-lifecycle",
            )
