# mypy: ignore-errors
"""Service tests for audited EarningsEvent schedule mutations."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from accounts.models import User
from audit.models import AuditRecord, DataChange
from earnings.models import (
    EarningsDateChange,
    EarningsDateChangeKind,
    EarningsDatePrecision,
)
from earnings.services import (
    EarningsDateChangeServiceError,
    InvalidEarningsDateValue,
    update_earnings_schedule,
)
from tests.earnings.helpers import make_event, make_source_evidence, make_sync_run

_DATE_FIELDS = (
    "estimated_release",
    "confirmed_release",
    "earnings_release",
    "conference_call",
)


def _history_counts(event) -> tuple[int, int, int]:
    return (
        DataChange.objects.filter(
            target_type=DataChange.TargetType.EARNINGS_EVENT,
            target_id=event.pk,
        ).count(),
        EarningsDateChange.objects.filter(earnings_event=event).count(),
        AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.EARNINGS_EVENT,
            target_id=event.pk,
        ).count(),
    )


def _date_only_event(field_name: str, value: date):
    return make_event(
        **{
            f"{field_name}_date": value,
            f"{field_name}_precision": EarningsDatePrecision.DATE_ONLY,
        }
    )


def _exact_event(field_name: str, value: datetime):
    return make_event(
        **{
            f"{field_name}_at": value,
            f"{field_name}_precision": EarningsDatePrecision.EXACT_DATETIME,
        }
    )


@pytest.mark.django_db
class TestUpdateEarningsSchedule:
    @pytest.mark.parametrize("field_name", _DATE_FIELDS)
    def test_first_date_only_value_for_each_controlled_date_field(self, field_name: str) -> None:
        event = make_event()
        sync_run = make_sync_run(f"first-{field_name}")

        result = update_earnings_schedule(
            earnings_event=event,
            changes={field_name: date(2026, 10, 24)},
            sync_run=sync_run,
        )

        assert result.changed is True
        assert len(result.data_changes) == 1
        assert len(result.date_changes) == 1
        assert result.audit_record is not None
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.PRECISION_REFINEMENT
        assert getattr(result.earnings_event, f"{field_name}_date") == date(2026, 10, 24)
        assert getattr(result.earnings_event, f"{field_name}_at") is None

    def test_first_exact_datetime(self) -> None:
        event = make_event()
        exact = datetime(2026, 10, 24, 20, 30, tzinfo=UTC)
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": exact},
            sync_run=make_sync_run("first-exact"),
        )

        assert result.changed is True
        assert result.earnings_event.estimated_release_at == exact
        assert result.earnings_event.estimated_release_date is None
        assert (
            result.earnings_event.estimated_release_precision
            == EarningsDatePrecision.EXACT_DATETIME
        )
        assert result.data_changes[0].change.new_value == {
            "kind": "datetime",
            "precision": "exact_datetime",
            "value": "2026-10-24T20:30:00Z",
        }

    def test_estimated_date_change_is_value_change(self) -> None:
        event = _date_only_event("estimated_release", date(2026, 10, 24))
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 25)},
            sync_run=make_sync_run("estimated-value-change"),
        )

        assert result.date_changes[0].change_kind == EarningsDateChangeKind.VALUE_CHANGE
        assert result.earnings_event.estimated_release_date == date(2026, 10, 25)

    def test_confirmed_release_change(self) -> None:
        event = _date_only_event("confirmed_release", date(2026, 10, 24))
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"confirmed_release": date(2026, 10, 26)},
            sync_run=make_sync_run("confirmed-change"),
        )

        assert result.changed is True
        assert result.earnings_event.confirmed_release_date == date(2026, 10, 26)

    def test_earnings_release_change(self) -> None:
        event = _date_only_event("earnings_release", date(2026, 10, 24))
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"earnings_release": date(2026, 10, 27)},
            sync_run=make_sync_run("earnings-release-change"),
        )

        assert result.changed is True
        assert result.earnings_event.earnings_release_date == date(2026, 10, 27)

    def test_conference_call_change(self) -> None:
        event = _date_only_event("conference_call", date(2026, 10, 24))
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"conference_call": date(2026, 10, 28)},
            sync_run=make_sync_run("conference-change"),
        )

        assert result.changed is True
        assert result.earnings_event.conference_call_date == date(2026, 10, 28)

    def test_session_change(self) -> None:
        event = make_event()
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"release_session": "after_market"},
            sync_run=make_sync_run("session-change"),
        )

        assert result.changed is True
        assert result.earnings_event.release_session == "after_market"
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.PRECISION_REFINEMENT


@pytest.mark.django_db
class TestPrecisionChanges:
    def test_unknown_to_date_only_is_refinement(self) -> None:
        event = make_event()
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            sync_run=make_sync_run("unknown-date"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.PRECISION_REFINEMENT

    def test_date_only_to_exact_same_market_date_is_refinement(self) -> None:
        event = _date_only_event("estimated_release", date(2026, 10, 24))
        exact = datetime(2026, 10, 24, 16, 30, tzinfo=ZoneInfo("America/New_York"))
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": exact},
            sync_run=make_sync_run("same-date-refinement"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.PRECISION_REFINEMENT
        assert result.earnings_event.estimated_release_at == datetime(
            2026, 10, 24, 20, 30, tzinfo=UTC
        )
        assert result.earnings_event.estimated_release_date is None

    def test_date_only_to_exact_different_market_date_is_value_change(self) -> None:
        event = _date_only_event("estimated_release", date(2026, 10, 24))
        exact = datetime(2026, 10, 25, 13, 30, tzinfo=UTC)
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": exact},
            sync_run=make_sync_run("different-date-refinement"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.VALUE_CHANGE

    def test_exact_to_date_only_same_market_date_is_regression(self) -> None:
        event = _exact_event(
            "estimated_release",
            datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
        )
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            sync_run=make_sync_run("same-date-regression"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.PRECISION_REGRESSION
        assert result.earnings_event.estimated_release_date == date(2026, 10, 24)
        assert result.earnings_event.estimated_release_at is None

    def test_session_unknown_to_concrete_is_refinement(self) -> None:
        event = make_event()
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"release_session": "after_market"},
            sync_run=make_sync_run("session-refinement"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.PRECISION_REFINEMENT

    def test_session_concrete_to_unknown_is_regression(self) -> None:
        event = make_event(release_session="after_market")
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"release_session": "unknown"},
            sync_run=make_sync_run("session-regression"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.PRECISION_REGRESSION

    def test_exact_to_date_only_different_market_date_is_value_change(self) -> None:
        event = _exact_event(
            "estimated_release",
            datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
        )
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 25)},
            sync_run=make_sync_run("different-date-regression"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.VALUE_CHANGE

    def test_exact_to_unknown_is_regression(self) -> None:
        event = _exact_event(
            "estimated_release",
            datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
        )
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": None},
            sync_run=make_sync_run("exact-to-unknown"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.PRECISION_REGRESSION

    def test_date_only_to_unknown_is_regression(self) -> None:
        event = _date_only_event("estimated_release", date(2026, 10, 24))
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": None},
            sync_run=make_sync_run("date-to-unknown"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.PRECISION_REGRESSION

    def test_concrete_session_to_different_concrete_session_is_value_change(self) -> None:
        event = make_event(release_session="after_market")
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"release_session": "pre_market"},
            sync_run=make_sync_run("session-value-change"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.VALUE_CHANGE

    def test_exact_time_change_on_same_market_date_is_value_change(self) -> None:
        event = _exact_event(
            "estimated_release",
            datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
        )
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": datetime(2026, 10, 24, 20, 45, tzinfo=UTC)},
            sync_run=make_sync_run("same-date-time-change"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.VALUE_CHANGE


@pytest.mark.django_db
class TestMarketDateAndTimezone:
    @pytest.mark.parametrize(
        ("exact", "market_date"),
        (
            (datetime(2026, 10, 24, 0, 30, tzinfo=UTC), date(2026, 10, 23)),
            (datetime(2026, 10, 24, 4, 30, tzinfo=UTC), date(2026, 10, 24)),
            (datetime(2026, 10, 24, 23, 30, tzinfo=UTC), date(2026, 10, 24)),
            (datetime(2026, 3, 8, 6, 30, tzinfo=UTC), date(2026, 3, 8)),
            (datetime(2026, 3, 8, 7, 30, tzinfo=UTC), date(2026, 3, 8)),
            (datetime(2026, 11, 1, 5, 30, tzinfo=UTC), date(2026, 11, 1)),
            (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), date(2026, 11, 1)),
        ),
    )
    def test_exact_datetime_uses_new_york_market_date(
        self,
        exact: datetime,
        market_date: date,
    ) -> None:
        event = _date_only_event("estimated_release", market_date)
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": exact},
            sync_run=make_sync_run("market-date"),
        )
        assert result.date_changes[0].change_kind == EarningsDateChangeKind.PRECISION_REFINEMENT

    def test_date_only_value_is_not_timezone_converted(self) -> None:
        event = _date_only_event("estimated_release", date(2026, 10, 24))
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            sync_run=make_sync_run("date-only-no-timezone"),
        )
        assert result.changed is False


@pytest.mark.django_db
class TestNoOpAndIdempotency:
    def test_same_date_only_is_no_op(self) -> None:
        event = _date_only_event("estimated_release", date(2026, 10, 24))
        before = _history_counts(event)
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            sync_run=make_sync_run("date-no-op"),
        )
        assert result.changed is False
        assert _history_counts(event) == before

    def test_same_session_is_no_op(self) -> None:
        event = make_event(release_session="after_market")
        before = _history_counts(event)
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"release_session": "after_market"},
            sync_run=make_sync_run("session-no-op"),
        )
        assert result.changed is False
        assert _history_counts(event) == before

    def test_same_instant_different_timezone_is_no_op(self) -> None:
        event = _exact_event(
            "estimated_release",
            datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
        )
        before = _history_counts(event)
        result = update_earnings_schedule(
            earnings_event=event,
            changes={
                "estimated_release": datetime(
                    2026,
                    10,
                    24,
                    16,
                    30,
                    tzinfo=ZoneInfo("America/New_York"),
                )
            },
            sync_run=make_sync_run("timezone-no-op"),
        )
        assert result.changed is False
        assert _history_counts(event) == before

    def test_replay_does_not_duplicate_history(self) -> None:
        event = make_event()
        sync_run = make_sync_run("replay")
        first = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            sync_run=sync_run,
        )
        first_counts = _history_counts(event)
        second = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            sync_run=sync_run,
        )
        assert first.changed is True
        assert second.changed is False
        assert _history_counts(event) == first_counts

    def test_manual_replay_does_not_duplicate_history(self) -> None:
        event = make_event()
        actor = User.objects.create_user(
            email="date-change-actor@example.com",
            password="test-password-only",
        )
        first = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            actor_user=actor,
            reason="Manual schedule correction.",
            request_id="manual-request-1",
        )
        first_counts = _history_counts(event)
        second = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            actor_user=actor,
            reason="Manual schedule correction.",
            request_id="manual-request-1",
        )
        assert first.audit_record.action == AuditRecord.Action.MANUAL_CORRECTION
        assert second.changed is False
        assert _history_counts(event) == first_counts

    def test_same_fact_from_new_evidence_does_not_create_second_domain_history(self) -> None:
        event = make_event()
        _, first_evidence = make_source_evidence(
            event=event,
            field_name="estimated_release",
            normalized_value={
                "kind": "date",
                "precision": "date_only",
                "value": "2026-10-24",
            },
            suffix="same-fact-first",
        )
        first = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            source_evidence=first_evidence,
        )
        first_counts = _history_counts(event)

        _, second_evidence = make_source_evidence(
            event=event,
            field_name="estimated_release",
            normalized_value={
                "kind": "date",
                "precision": "date_only",
                "value": "2026-10-24",
            },
            suffix="same-fact-second",
        )
        second = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            source_evidence=second_evidence,
        )

        assert first.changed is True
        assert second.changed is False
        assert _history_counts(event) == first_counts


@pytest.mark.django_db
class TestAuditAndEvidence:
    def test_source_evidence_is_linked_through_data_change(self) -> None:
        event = make_event()
        sync_run, evidence = make_source_evidence(
            event=event,
            field_name="estimated_release",
            normalized_value={
                "kind": "date",
                "precision": "date_only",
                "value": "2026-10-24",
            },
        )
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            source_evidence=evidence,
        )

        assert result.data_changes[0].change.source_evidence_id == evidence.pk
        assert result.date_changes[0].data_change.source_evidence_id == evidence.pk
        assert result.audit_record.sync_run_id == sync_run.pk
        assert result.audit_record.target_type == AuditRecord.TargetType.EARNINGS_EVENT

    def test_mismatched_evidence_field_is_rejected(self) -> None:
        event = make_event()
        _, evidence = make_source_evidence(
            event=event,
            field_name="confirmed_release",
            normalized_value={
                "kind": "date",
                "precision": "date_only",
                "value": "2026-10-24",
            },
        )
        with pytest.raises(EarningsDateChangeServiceError, match="changed domain field"):
            update_earnings_schedule(
                earnings_event=event,
                changes={"estimated_release": date(2026, 10, 24)},
                source_evidence=evidence,
            )

    def test_release_session_source_evidence_is_allowed(self) -> None:
        event = make_event()
        _, evidence = make_source_evidence(
            event=event,
            field_name="release_session",
            normalized_value={
                "kind": "session",
                "precision": "session_only",
                "value": "after_market",
            },
        )
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"release_session": "after_market"},
            source_evidence=evidence,
        )
        assert result.data_changes[0].change.source_evidence_id == evidence.pk

    def test_multi_field_mutation_has_one_audit_and_per_field_history(self) -> None:
        event = make_event()
        result = update_earnings_schedule(
            earnings_event=event,
            changes={
                "estimated_release": date(2026, 10, 24),
                "release_session": "after_market",
            },
            sync_run=make_sync_run("multi-field"),
        )

        assert len(result.data_changes) == 2
        assert len(result.date_changes) == 2
        assert result.audit_record is not None
        assert set(result.audit_record.before) == {"estimated_release", "release_session"}
        assert set(result.audit_record.after) == {"estimated_release", "release_session"}

    def test_source_evidence_must_reference_the_same_event(self) -> None:
        event = make_event()
        other_event = make_event()
        _, evidence = make_source_evidence(
            event=other_event,
            field_name="estimated_release",
            normalized_value={
                "kind": "date",
                "precision": "date_only",
                "value": "2026-10-24",
            },
        )
        with pytest.raises(EarningsDateChangeServiceError, match="same domain target"):
            update_earnings_schedule(
                earnings_event=event,
                changes={"estimated_release": date(2026, 10, 24)},
                source_evidence=evidence,
            )


@pytest.mark.django_db
class TestTransactionAndValidation:
    def test_audit_failure_rolls_back_current_state_and_history(self, monkeypatch) -> None:
        from earnings.services import date_changes as service_module

        event = make_event()
        sync_run = make_sync_run("rollback")

        def fail_audit(**_kwargs):
            raise RuntimeError("forced audit failure")

        monkeypatch.setattr(service_module, "record_system_action", fail_audit)

        with pytest.raises(RuntimeError, match="forced audit failure"):
            update_earnings_schedule(
                earnings_event=event,
                changes={"estimated_release": date(2026, 10, 24)},
                sync_run=sync_run,
            )

        event.refresh_from_db()
        assert event.estimated_release_date is None
        assert event.estimated_release_precision == EarningsDatePrecision.UNKNOWN
        assert _history_counts(event) == (0, 0, 0)

    def test_second_field_failure_rolls_back_first_field(self, monkeypatch) -> None:
        from earnings.services import date_changes as service_module

        event = make_event()
        sync_run = make_sync_run("multi-field-rollback")
        original = service_module.record_data_change
        calls = 0

        def fail_second_call(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("forced second field failure")
            return original(**kwargs)

        monkeypatch.setattr(service_module, "record_data_change", fail_second_call)

        with pytest.raises(RuntimeError, match="forced second field failure"):
            update_earnings_schedule(
                earnings_event=event,
                changes={
                    "estimated_release": date(2026, 10, 24),
                    "release_session": "after_market",
                },
                sync_run=sync_run,
            )

        event.refresh_from_db()
        assert event.estimated_release_date is None
        assert event.release_session == "unknown"
        assert _history_counts(event) == (0, 0, 0)

    def test_stale_caller_object_does_not_supply_old_value(self) -> None:
        event = make_event()
        stale = type(event).objects.get(pk=event.pk)
        first_sync_run = make_sync_run("stale-first")
        update_earnings_schedule(
            earnings_event=event,
            changes={"estimated_release": date(2026, 10, 24)},
            sync_run=first_sync_run,
        )
        update_earnings_schedule(
            earnings_event=stale,
            changes={"estimated_release": date(2026, 10, 25)},
            sync_run=make_sync_run("stale-second"),
        )

        latest = (
            DataChange.objects.filter(
                target_type=DataChange.TargetType.EARNINGS_EVENT,
                target_id=event.pk,
                field_name="estimated_release",
            )
            .order_by("-changed_at")
            .first()
        )
        assert latest.old_value == {
            "kind": "date",
            "precision": "date_only",
            "value": "2026-10-24",
        }

    def test_invalid_precision_value_combination_is_rejected(self) -> None:
        event = make_event()
        with pytest.raises(InvalidEarningsDateValue, match="date_only"):
            update_earnings_schedule(
                earnings_event=event,
                changes={
                    "estimated_release": {
                        "precision": "date_only",
                        "value": datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
                    }
                },
                sync_run=make_sync_run("invalid-combination"),
            )

    def test_naive_datetime_is_rejected(self) -> None:
        event = make_event()
        with pytest.raises(InvalidEarningsDateValue, match="timezone-aware"):
            update_earnings_schedule(
                earnings_event=event,
                changes={"estimated_release": datetime(2026, 10, 24, 20, 30)},
                sync_run=make_sync_run("naive-datetime"),
            )

    def test_unsupported_field_is_rejected(self) -> None:
        event = make_event()
        with pytest.raises(InvalidEarningsDateValue, match="Unsupported"):
            update_earnings_schedule(
                earnings_event=event,
                changes={"status": "released"},
                sync_run=make_sync_run("unsupported-field"),
            )

    def test_invalid_release_session_is_rejected(self) -> None:
        event = make_event()
        with pytest.raises(InvalidEarningsDateValue, match="release_session"):
            update_earnings_schedule(
                earnings_event=event,
                changes={"release_session": "sometimes"},
                sync_run=make_sync_run("invalid-session"),
            )

    def test_no_audit_context_is_rejected(self) -> None:
        event = make_event()
        with pytest.raises(EarningsDateChangeServiceError, match="SyncRun or SourceEvidence"):
            update_earnings_schedule(
                earnings_event=event,
                changes={"estimated_release": date(2026, 10, 24)},
            )
