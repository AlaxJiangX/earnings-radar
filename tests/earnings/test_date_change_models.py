# mypy: ignore-errors
"""Model and constraint tests for EarningsDateChange and schedule precision."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from django.contrib.admin.sites import AdminSite
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from audit.models import AppendOnlyRecordError
from earnings.admin import EarningsDateChangeAdmin
from earnings.models import (
    EarningsDateChange,
    EarningsDateChangeKind,
    EarningsDateHistoryPrecision,
    EarningsDatePrecision,
    EarningsEvent,
)
from tests.earnings.helpers import make_data_change, make_event

_DATE_FIELDS = (
    "estimated_release",
    "confirmed_release",
    "earnings_release",
    "conference_call",
)


def _create_date_change(
    *,
    event: EarningsEvent,
    field_name: str = "estimated_release",
    change_kind: str = EarningsDateChangeKind.VALUE_CHANGE,
    old_precision: str = EarningsDateHistoryPrecision.DATE_ONLY,
    new_precision: str = EarningsDateHistoryPrecision.DATE_ONLY,
    old_date: date | None = date(2026, 10, 24),
    new_date: date | None = date(2026, 10, 25),
    old_datetime: datetime | None = None,
    new_datetime: datetime | None = None,
    old_session: str | None = None,
    new_session: str | None = None,
    suffix: str = "date-change-model",
) -> EarningsDateChange:
    def canonical_value(
        *,
        precision: str,
        value_date: date | None,
        value_datetime: datetime | None,
        session: str | None,
    ) -> object:
        if session is not None:
            return {
                "kind": "session",
                "precision": EarningsDateHistoryPrecision.SESSION_ONLY,
                "value": session,
            }
        if precision == EarningsDateHistoryPrecision.UNKNOWN:
            return None
        if precision == EarningsDateHistoryPrecision.DATE_ONLY:
            return {
                "kind": "date",
                "precision": precision,
                "value": value_date.isoformat() if value_date is not None else None,
            }
        return {
            "kind": "datetime",
            "precision": precision,
            "value": (
                value_datetime.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if value_datetime is not None
                else None
            ),
        }

    data_change = make_data_change(
        event=event,
        field_name=field_name,
        old_value=canonical_value(
            precision=old_precision,
            value_date=old_date,
            value_datetime=old_datetime,
            session=old_session,
        ),
        new_value=canonical_value(
            precision=new_precision,
            value_date=new_date,
            value_datetime=new_datetime,
            session=new_session,
        ),
        suffix=suffix,
    )
    return EarningsDateChange.objects.create(
        earnings_event=event,
        field_name=field_name,
        change_kind=change_kind,
        old_precision=old_precision,
        new_precision=new_precision,
        old_date=old_date,
        new_date=new_date,
        old_datetime=old_datetime,
        new_datetime=new_datetime,
        old_session=old_session,
        new_session=new_session,
        data_change=data_change.change,
        detected_at=datetime(2026, 9, 19, 10, 0, tzinfo=UTC),
    )


@pytest.mark.django_db
class TestEarningsEventScheduleState:
    @pytest.mark.parametrize("prefix", _DATE_FIELDS)
    def test_unknown_state_valid(self, prefix: str) -> None:
        event = make_event()
        assert getattr(event, f"{prefix}_at") is None
        assert getattr(event, f"{prefix}_date") is None
        assert getattr(event, f"{prefix}_precision") == EarningsDatePrecision.UNKNOWN

    @pytest.mark.parametrize("prefix", _DATE_FIELDS)
    def test_date_only_state_valid(self, prefix: str) -> None:
        event = make_event(
            **{
                f"{prefix}_date": date(2026, 10, 24),
                f"{prefix}_precision": EarningsDatePrecision.DATE_ONLY,
            }
        )
        assert getattr(event, f"{prefix}_at") is None
        assert getattr(event, f"{prefix}_date") == date(2026, 10, 24)
        assert getattr(event, f"{prefix}_precision") == EarningsDatePrecision.DATE_ONLY

    @pytest.mark.parametrize("prefix", _DATE_FIELDS)
    def test_exact_datetime_state_valid(self, prefix: str) -> None:
        event = make_event(
            **{
                f"{prefix}_at": datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
                f"{prefix}_precision": EarningsDatePrecision.EXACT_DATETIME,
            }
        )
        assert getattr(event, f"{prefix}_at") == datetime(2026, 10, 24, 20, 30, tzinfo=UTC)
        assert getattr(event, f"{prefix}_date") is None
        assert getattr(event, f"{prefix}_precision") == EarningsDatePrecision.EXACT_DATETIME

    @pytest.mark.parametrize(
        ("prefix", "invalid_values"),
        [
            (
                "estimated_release",
                {
                    "estimated_release_date": date(2026, 10, 24),
                },
            ),
            (
                "confirmed_release",
                {
                    "confirmed_release_at": datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
                },
            ),
            (
                "earnings_release",
                {
                    "earnings_release_date": date(2026, 10, 24),
                    "earnings_release_at": datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
                    "earnings_release_precision": EarningsDatePrecision.DATE_ONLY,
                },
            ),
            (
                "conference_call",
                {
                    "conference_call_precision": EarningsDatePrecision.DATE_ONLY,
                },
            ),
            (
                "estimated_release",
                {
                    "estimated_release_date": date(2026, 10, 24),
                    "estimated_release_precision": EarningsDatePrecision.EXACT_DATETIME,
                },
            ),
            (
                "confirmed_release",
                {
                    "confirmed_release_precision": EarningsDatePrecision.EXACT_DATETIME,
                },
            ),
        ],
    )
    def test_invalid_schedule_states_rejected(
        self, prefix: str, invalid_values: dict[str, object]
    ) -> None:
        del prefix
        with pytest.raises(IntegrityError), transaction.atomic():
            make_event(**invalid_values)

    def test_release_session_defaults_to_unknown(self) -> None:
        event = make_event()
        assert event.release_session == "unknown"

    def test_release_session_null_rejected(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            make_event(release_session=None)

    def test_release_session_invalid_rejected(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            make_event(release_session="sometimes")


@pytest.mark.django_db
class TestEarningsDateChangeConstraints:
    def test_admin_is_read_only(self) -> None:
        model_admin = EarningsDateChangeAdmin(EarningsDateChange, AdminSite())
        assert model_admin.has_add_permission(None) is False
        assert model_admin.has_change_permission(None) is False
        assert model_admin.has_delete_permission(None) is False

    def test_valid_value_change(self) -> None:
        event = make_event()
        change = _create_date_change(event=event)
        assert change.change_kind == EarningsDateChangeKind.VALUE_CHANGE
        assert change.old_date == date(2026, 10, 24)
        assert change.new_date == date(2026, 10, 25)

    def test_valid_precision_refinement(self) -> None:
        event = make_event()
        change = _create_date_change(
            event=event,
            change_kind=EarningsDateChangeKind.PRECISION_REFINEMENT,
            old_date=date(2026, 10, 24),
            new_date=None,
            new_precision=EarningsDateHistoryPrecision.EXACT_DATETIME,
            new_datetime=datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
        )
        assert change.new_datetime == datetime(2026, 10, 24, 20, 30, tzinfo=UTC)

    def test_valid_precision_regression(self) -> None:
        event = make_event()
        change = _create_date_change(
            event=event,
            change_kind=EarningsDateChangeKind.PRECISION_REGRESSION,
            old_date=None,
            new_date=date(2026, 10, 24),
            old_precision=EarningsDateHistoryPrecision.EXACT_DATETIME,
            old_datetime=datetime(2026, 10, 24, 20, 30, tzinfo=UTC),
        )
        assert change.new_date == date(2026, 10, 24)

    def test_valid_session_change(self) -> None:
        event = make_event()
        change = _create_date_change(
            event=event,
            field_name="release_session",
            change_kind=EarningsDateChangeKind.VALUE_CHANGE,
            old_precision=EarningsDateHistoryPrecision.SESSION_ONLY,
            new_precision=EarningsDateHistoryPrecision.SESSION_ONLY,
            old_date=None,
            new_date=None,
            old_session="after_market",
            new_session="pre_market",
        )
        assert change.old_session == "after_market"
        assert change.new_session == "pre_market"

    def test_invalid_field_name_rejected(self) -> None:
        event = make_event()
        with pytest.raises(IntegrityError), transaction.atomic():
            _create_date_change(event=event, field_name="bad_field", suffix="bad-field")

    def test_invalid_shape_rejected(self) -> None:
        event = make_event()
        with pytest.raises(IntegrityError), transaction.atomic():
            _create_date_change(
                event=event,
                old_precision=EarningsDateHistoryPrecision.DATE_ONLY,
                old_date=None,
            )

    def test_data_change_is_one_to_one(self) -> None:
        event = make_event()
        first = _create_date_change(event=event, suffix="one-to-one-first")
        with pytest.raises(IntegrityError), transaction.atomic():
            EarningsDateChange.objects.create(
                earnings_event=event,
                field_name="estimated_release",
                change_kind=EarningsDateChangeKind.VALUE_CHANGE,
                old_precision=EarningsDateHistoryPrecision.DATE_ONLY,
                new_precision=EarningsDateHistoryPrecision.DATE_ONLY,
                old_date=date(2026, 10, 24),
                new_date=date(2026, 10, 26),
                data_change=first.data_change,
            )

    def test_event_delete_is_protected(self) -> None:
        event = make_event()
        _create_date_change(event=event)
        with pytest.raises(ProtectedError):
            event.delete()

    def test_date_change_is_append_only(self) -> None:
        event = make_event()
        change = _create_date_change(event=event)
        change.field_name = "conference_call"
        with pytest.raises(AppendOnlyRecordError):
            change.save()
        with pytest.raises(AppendOnlyRecordError):
            change.delete()
        with pytest.raises(AppendOnlyRecordError):
            EarningsDateChange.objects.filter(pk=change.pk).update(
                change_kind=EarningsDateChangeKind.PRECISION_REFINEMENT
            )
        with pytest.raises(AppendOnlyRecordError):
            EarningsDateChange.objects.filter(pk=change.pk).delete()
