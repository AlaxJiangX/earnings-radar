# mypy: ignore-errors
"""Model and DB constraint tests for EarningsCalendarObservation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError

from audit.models import AppendOnlyRecordError
from earnings.models import EarningsCalendarObservation
from tests.earnings.helpers import (
    make_calendar_observation,
    make_calendar_raw_record,
    make_calendar_source,
)


def _constraint_name(error: IntegrityError) -> str | None:
    cause = error.__cause__
    return getattr(getattr(cause, "diag", None), "constraint_name", None)


def _source_and_record(suffix: str = "models") -> tuple[object, object]:
    source = make_calendar_source(suffix)
    return source, make_calendar_raw_record(source, suffix)


@pytest.mark.django_db
class TestEarningsCalendarObservationModel:
    def test_minimal_valid_observation(self) -> None:
        observation = make_calendar_observation()

        assert observation.pk is not None
        assert observation.provider_key == "fixture-calendar-provider"
        assert observation.estimated_release_precision == "date_only"
        assert observation.created_at is not None
        assert str(observation) == (f"{observation.source_id}:{observation.provider_event_id}@1")

    def test_unique_record_parser_event(self) -> None:
        source, raw_data_record = _source_and_record("unique")
        values = {
            "source": source,
            "raw_data_record": raw_data_record,
            "provider_event_id": "evt-1",
            "parser_version": "parser-1",
        }
        make_calendar_observation(**values)

        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_calendar_observation(**values)

        assert _constraint_name(exc_info.value) == (
            "earnings_calendar_observation_record_parser_event_unique"
        )

    def test_same_provider_event_different_raw_record_allowed(self) -> None:
        source, first_raw = _source_and_record("different-raw")
        second_raw = make_calendar_raw_record(source, "different-raw-2")

        make_calendar_observation(
            source=source,
            raw_data_record=first_raw,
            provider_event_id="evt-1",
            parser_version="parser-1",
        )
        make_calendar_observation(
            source=source,
            raw_data_record=second_raw,
            provider_event_id="evt-1",
            parser_version="parser-1",
        )

        assert EarningsCalendarObservation.objects.count() == 2

    def test_same_provider_event_different_parser_version_allowed(self) -> None:
        source, raw_data_record = _source_and_record("different-parser")

        make_calendar_observation(
            source=source,
            raw_data_record=raw_data_record,
            provider_event_id="evt-1",
            parser_version="parser-1",
        )
        make_calendar_observation(
            source=source,
            raw_data_record=raw_data_record,
            provider_event_id="evt-1",
            parser_version="parser-2",
        )

        assert EarningsCalendarObservation.objects.count() == 2

    def test_provider_key_invalid(self) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_calendar_observation(provider_key="Bad Key")

        assert _constraint_name(exc_info.value) == (
            "earnings_calendar_observation_provider_key_valid"
        )

    @pytest.mark.parametrize(
        "field_name",
        ("provider_version", "parser_version", "provider_event_id"),
    )
    def test_identity_fields_must_not_be_blank(self, field_name: str) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_calendar_observation(**{field_name: ""})

        assert _constraint_name(exc_info.value) == (
            "earnings_calendar_observation_identity_not_blank"
        )

    @pytest.mark.parametrize("cik", ("", "0000000001"))
    def test_cik_valid_values(self, cik: str) -> None:
        observation = make_calendar_observation(cik=cik)
        assert observation.cik == cik

    def test_cik_invalid(self) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_calendar_observation(cik="123")

        assert _constraint_name(exc_info.value) == ("earnings_calendar_observation_cik_valid")

    def test_period_type_invalid(self) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_calendar_observation(period_type="Q4")

        assert _constraint_name(exc_info.value) == (
            "earnings_calendar_observation_period_type_valid"
        )

    def test_fiscal_calendar_type_invalid(self) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_calendar_observation(fiscal_calendar_type="weekly")

        assert _constraint_name(exc_info.value) == (
            "earnings_calendar_observation_calendar_type_valid"
        )

    def test_release_session_invalid(self) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_calendar_observation(release_session="midnight")

        assert _constraint_name(exc_info.value) == (
            "earnings_calendar_observation_release_session_valid"
        )

    def test_estimated_release_unknown_state(self) -> None:
        observation = make_calendar_observation(
            estimated_release_date=None,
            estimated_release_at=None,
            estimated_release_precision="unknown",
        )

        assert observation.estimated_release_date is None
        assert observation.estimated_release_at is None
        assert observation.estimated_release_precision == "unknown"

    def test_estimated_release_exact_datetime_state(self) -> None:
        observation = make_calendar_observation(
            estimated_release_date=None,
            estimated_release_at=datetime(2026, 4, 22, 20, 30, tzinfo=UTC),
            estimated_release_precision="exact_datetime",
        )

        assert observation.estimated_release_date is None
        assert observation.estimated_release_at == datetime(2026, 4, 22, 20, 30, tzinfo=UTC)
        assert observation.estimated_release_precision == "exact_datetime"

    @pytest.mark.parametrize(
        "overrides",
        (
            {
                "estimated_release_date": date(2026, 4, 22),
                "estimated_release_at": None,
                "estimated_release_precision": "unknown",
            },
            {
                "estimated_release_date": None,
                "estimated_release_at": datetime(2026, 4, 22, 20, 30, tzinfo=UTC),
                "estimated_release_precision": "unknown",
            },
            {
                "estimated_release_date": date(2026, 4, 22),
                "estimated_release_at": datetime(2026, 4, 22, 20, 30, tzinfo=UTC),
                "estimated_release_precision": "date_only",
            },
            {
                "estimated_release_date": None,
                "estimated_release_at": None,
                "estimated_release_precision": "date_only",
            },
            {
                "estimated_release_date": None,
                "estimated_release_at": None,
                "estimated_release_precision": "exact_datetime",
            },
            {
                "estimated_release_date": date(2026, 4, 22),
                "estimated_release_at": None,
                "estimated_release_precision": "exact_datetime",
            },
        ),
    )
    def test_estimated_release_invalid_combinations(
        self,
        overrides: dict[str, object],
    ) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_calendar_observation(**overrides)

        assert _constraint_name(exc_info.value) == (
            "earnings_calendar_observation_estimated_precision_valid"
        )

    @pytest.mark.parametrize(
        "confidence",
        (Decimal("0"), Decimal("1"), Decimal("0.5000"), None),
    )
    def test_confidence_valid_values(self, confidence: Decimal | None) -> None:
        observation = make_calendar_observation(confidence=confidence)
        assert observation.confidence == confidence

    @pytest.mark.parametrize("confidence", (Decimal("-0.0001"), Decimal("1.0001")))
    def test_confidence_invalid(self, confidence: Decimal) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_calendar_observation(confidence=confidence)

        assert _constraint_name(exc_info.value) == (
            "earnings_calendar_observation_confidence_range"
        )

    def test_foreign_keys_are_protected(self) -> None:
        source, raw_data_record = _source_and_record("protect")
        make_calendar_observation(source=source, raw_data_record=raw_data_record)

        with pytest.raises(ProtectedError):
            raw_data_record.delete()
        with pytest.raises(ProtectedError):
            source.delete()

    def test_observation_is_append_only(self) -> None:
        observation = make_calendar_observation()
        observation.provider_event_id = "evt-changed"

        with pytest.raises(AppendOnlyRecordError):
            observation.save()
        with pytest.raises(AppendOnlyRecordError):
            EarningsCalendarObservation.objects.filter(pk=observation.pk).update(
                provider_event_id="evt-changed"
            )
        with pytest.raises(AppendOnlyRecordError):
            EarningsCalendarObservation.objects.filter(pk=observation.pk).delete()
        with pytest.raises(AppendOnlyRecordError):
            observation.delete()
        with pytest.raises(AppendOnlyRecordError):
            EarningsCalendarObservation.objects.bulk_update(
                [observation],
                ["provider_event_id"],
            )

    def test_source_provider_event_index_exists(self) -> None:
        index_fields = [list(index.fields) for index in EarningsCalendarObservation._meta.indexes]

        assert ["source", "provider_event_id"] in index_fields
        assert ["source", "provider_event_id", "created_at"] not in index_fields
