# mypy: ignore-errors
"""Persistence and concurrency tests for earnings calendar observations."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest import mock

import pytest
from django.db import (
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)

from earnings.models import EarningsCalendarObservation
from earnings.services.calendar import (
    EarningsCalendarObservationIntegrityError,
    InvalidEarningsCalendarObservation,
    record_earnings_calendar_observation,
)
from tests.earnings.helpers import make_calendar_raw_record, make_calendar_source


def _record(source, raw_data_record, **overrides):
    values = {
        "source": source,
        "raw_data_record": raw_data_record,
        "provider_key": source.provider_adapter,
        "provider_version": "fixture-provider-v1",
        "parser_version": "fixture-parser-v1",
        "provider_event_id": "evt-001",
        "raw_position": 1,
        "ticker": "FAKE",
        "exchange": "NASDAQ",
        "provider_symbol": "FAKE",
        "company_name": "Fixture Calendar Corp",
        "fiscal_label_raw": "Q1",
        "fiscal_year": 2026,
        "period_end_date": date(2026, 3, 31),
        "period_type": "Q1",
        "fiscal_calendar_type": "month_based",
        "estimated_release": date(2026, 4, 22),
        "estimated_release_precision": "date_only",
        "release_session": "after_market",
        "confidence": Decimal("0.9000"),
    }
    values.update(overrides)
    return record_earnings_calendar_observation(**values)


@pytest.mark.django_db
class TestEarningsCalendarObservationPersistence:
    def test_first_create_and_replay(self) -> None:
        source = make_calendar_source("replay")
        raw_data_record = make_calendar_raw_record(source, "replay")

        first = _record(source, raw_data_record)
        second = _record(source, raw_data_record)

        assert first.created is True
        assert second.created is False
        assert second.observation.pk == first.observation.pk
        assert EarningsCalendarObservation.objects.count() == 1

    def test_replay_returns_persisted_observation(self) -> None:
        source = make_calendar_source("persisted")
        raw_data_record = make_calendar_raw_record(source, "persisted")

        first = _record(source, raw_data_record)
        second = _record(source, raw_data_record)

        persisted = EarningsCalendarObservation.objects.get(pk=first.observation.pk)
        assert second.observation.pk == persisted.pk
        assert second.observation.provider_event_id == "evt-001"
        assert second.observation.parser_version == "fixture-parser-v1"

    def test_cik_is_normalized(self) -> None:
        source = make_calendar_source("cik")
        raw_data_record = make_calendar_raw_record(source, "cik")

        result = _record(source, raw_data_record, cik="123")

        assert result.observation.cik == "0000000123"

    def test_source_and_raw_data_record_mismatch_is_rejected(self) -> None:
        first_source = make_calendar_source("source-one")
        raw_data_record = make_calendar_raw_record(first_source, "source-one")
        second_source = make_calendar_source("source-two")

        with pytest.raises(InvalidEarningsCalendarObservation, match="belong"):
            _record(second_source, raw_data_record)

    def test_provider_key_mismatch_is_rejected(self) -> None:
        source = make_calendar_source("provider-mismatch")
        raw_data_record = make_calendar_raw_record(source, "provider-mismatch")

        with pytest.raises(InvalidEarningsCalendarObservation, match="provider_adapter"):
            _record(source, raw_data_record, provider_key="other-provider")

    @pytest.mark.parametrize(
        ("field_name", "value"),
        (
            ("provider_event_id", ""),
            ("provider_version", ""),
            ("parser_version", ""),
        ),
    )
    def test_missing_identity_fields_are_rejected(
        self,
        field_name: str,
        value: str,
    ) -> None:
        source = make_calendar_source("missing-identity")
        raw_data_record = make_calendar_raw_record(source, "missing-identity")

        with pytest.raises(InvalidEarningsCalendarObservation, match=field_name):
            _record(source, raw_data_record, **{field_name: value})

    def test_duplicate_row_with_different_immutable_fields_fails_closed(self) -> None:
        source = make_calendar_source("immutable")
        raw_data_record = make_calendar_raw_record(source, "immutable")
        _record(source, raw_data_record, ticker="FAKE")

        with pytest.raises(EarningsCalendarObservationIntegrityError, match="ticker"):
            _record(source, raw_data_record, ticker="OTHER")

    def test_estimated_release_datetime_mapping(self) -> None:
        source = make_calendar_source("datetime")
        raw_data_record = make_calendar_raw_record(source, "datetime")

        result = _record(
            source,
            raw_data_record,
            estimated_release=datetime(2026, 4, 22, 20, 30, tzinfo=UTC),
            estimated_release_precision="exact_datetime",
        )

        assert result.observation.estimated_release_date is None
        assert result.observation.estimated_release_at == datetime(2026, 4, 22, 20, 30, tzinfo=UTC)
        assert result.observation.estimated_release_precision == "exact_datetime"

    def test_naive_source_observed_at_is_rejected(self) -> None:
        source = make_calendar_source("naive")
        raw_data_record = make_calendar_raw_record(source, "naive")

        with pytest.raises(InvalidEarningsCalendarObservation, match="timezone-aware"):
            _record(
                source,
                raw_data_record,
                source_observed_at=datetime(2026, 4, 22, 20, 30),
            )

    def test_confidence_is_normalized(self) -> None:
        source = make_calendar_source("confidence")
        raw_data_record = make_calendar_raw_record(source, "confidence")

        result = _record(source, raw_data_record, confidence="0.9")

        assert result.observation.confidence == Decimal("0.9000")

    def test_unknown_integrity_error_is_not_swallowed(self) -> None:
        source = make_calendar_source("unknown-integrity")
        raw_data_record = make_calendar_raw_record(source, "unknown-integrity")

        with mock.patch.object(
            EarningsCalendarObservation.objects,
            "create",
            side_effect=IntegrityError("unknown integrity failure"),
        ):
            with pytest.raises(IntegrityError, match="unknown integrity failure"):
                _record(source, raw_data_record)


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_creates_one_row() -> None:
    source = make_calendar_source("concurrent")
    raw_data_record = make_calendar_raw_record(source, "concurrent")
    barrier = threading.Barrier(2, timeout=10)
    results: list[object] = []
    errors: list[BaseException] = []

    def worker() -> None:
        close_old_connections()
        try:
            barrier.wait()
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '5s'")
                results.append(_record(source, raw_data_record))
        except Exception as error:
            errors.append(error)
        finally:
            for current_connection in connections.all():
                current_connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    for thread in threads:
        assert not thread.is_alive(), "Concurrent observation thread hung"

    assert errors == []
    assert len(results) == 2
    assert sorted(result.created for result in results) == [False, True]
    assert EarningsCalendarObservation.objects.count() == 1
