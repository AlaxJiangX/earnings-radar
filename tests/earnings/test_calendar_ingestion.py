from __future__ import annotations

import http.client
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest import mock

import pytest

from audit.models import (
    AuditRecord,
    DataChange,
    DataSource,
    RawDataObservation,
    RawDataParseAttempt,
    RawDataRecord,
    SourceEvidence,
    SyncRun,
)
from audit.services import InvalidRawDataRequest, mark_sync_run_succeeded, start_sync_run
from companies.models import Company, SecurityListing
from earnings.calendar_parsing import (
    FIXTURE_EARNINGS_CALENDAR_FORMAT_VERSION,
    FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION,
    FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
    FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
    EarningsCalendarParser,
    EarningsCalendarParserContextError,
    EarningsCalendarParseResult,
    FixtureEarningsCalendarParser,
)
from earnings.models import (
    EarningsCalendarObservation,
    EarningsDateChange,
    EarningsEvent,
    EarningsReconciliationDecision,
)
from earnings.services import (
    EarningsCalendarIngestionResult,
    EarningsCalendarParserSystemFailure,
    EarningsCalendarPayloadParseFailure,
    EarningsCalendarUnsupportedIdentity,
    InvalidEarningsCalendarIngestion,
    ingest_earnings_calendar_payload,
)
from earnings.services.calendar import (
    EarningsCalendarObservationWriteResult,
)
from earnings.services.calendar import (
    record_earnings_calendar_observation as real_record_earnings_calendar_observation,
)
from indexes.models import IndexMembership

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "earnings_calendar"
FETCHED_AT = datetime(2026, 7, 14, 12, 0, 1, tzinfo=UTC)
SOURCE_URL = "https://fixture-earnings-calendar.test/calendar?fixture=complete"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def _payload_with_events(events: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "fixture_version": FIXTURE_EARNINGS_CALENDAR_FORMAT_VERSION,
            "provider_key": FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            "provider_version": FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
            "events": events,
        }
    ).encode()


def _calendar_source(suffix: str) -> DataSource:
    return DataSource.objects.create(
        key=f"fixture-calendar-ingestion-{suffix}",
        name=f"Fixture calendar ingestion {suffix}",
        source_type=DataSource.SourceType.EARNINGS_CALENDAR,
        base_url="https://fixture-earnings-calendar.test/",
        provider_adapter=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
        license_notes="Synthetic test-only source.",
    )


def _sync_run(source: DataSource, suffix: str) -> SyncRun:
    return start_sync_run(
        job_type="fixture.earnings-calendar-ingestion",
        source=source,
        scope={"fixture": suffix},
        idempotency_key=f"fixture.earnings-calendar-ingestion:{suffix}",
    )


def _ingest(
    *,
    sync_run: SyncRun,
    payload: bytes,
    parser: EarningsCalendarParser | None = None,
    source_url: str = SOURCE_URL,
    provider_key: str = FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
    provider_version: str = FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
) -> EarningsCalendarIngestionResult:
    selected_parser: EarningsCalendarParser
    if parser is None:
        selected_parser = FixtureEarningsCalendarParser()
    else:
        selected_parser = parser
    return ingest_earnings_calendar_payload(
        sync_run=sync_run,
        parser=selected_parser,
        raw_content=payload,
        provider_key=provider_key,
        provider_version=provider_version,
        source_url=source_url,
        fetched_at=FETCHED_AT,
    )


class ObservingFixtureParser:
    parser_version = FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION

    def __init__(self) -> None:
        self.seen_parser_status: str | None = None
        self.seen_observation_count = 0
        self.content_matched_persisted = False

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> EarningsCalendarParseResult:
        raw_record = RawDataRecord.objects.get()
        self.seen_parser_status = raw_record.parser_status
        self.seen_observation_count = RawDataObservation.objects.filter(
            raw_data_record=raw_record
        ).count()
        self.content_matched_persisted = bytes(raw_record.payload) == raw_content
        return FixtureEarningsCalendarParser().parse(
            raw_content,
            provider_key=provider_key,
            provider_version=provider_version,
        )


class RaisingParser:
    parser_version = FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION

    def __init__(self, error: Exception) -> None:
        self.error = error

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> EarningsCalendarParseResult:
        del raw_content, provider_key, provider_version
        raise self.error


class MismatchedResultParser:
    parser_version = FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> EarningsCalendarParseResult:
        valid = FixtureEarningsCalendarParser().parse(
            raw_content,
            provider_key=provider_key,
            provider_version=provider_version,
        )
        return EarningsCalendarParseResult(
            provider_key="other-fixture-provider",
            provider_version=valid.provider_version,
            parser_version=valid.parser_version,
            records=valid.records,
        )


class DuplicateRecordParser:
    parser_version = FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> EarningsCalendarParseResult:
        valid = FixtureEarningsCalendarParser().parse(
            raw_content,
            provider_key=provider_key,
            provider_version=provider_version,
        )
        return EarningsCalendarParseResult(
            provider_key=valid.provider_key,
            provider_version=valid.provider_version,
            parser_version=valid.parser_version,
            records=valid.records + (valid.records[0],),
        )


class DuplicatePositionParser:
    parser_version = FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> EarningsCalendarParseResult:
        valid = FixtureEarningsCalendarParser().parse(
            raw_content,
            provider_key=provider_key,
            provider_version=provider_version,
        )
        duplicate_position = replace(
            valid.records[1],
            provider_event_id="fixture-evt-duplicate-position",
            raw_position=valid.records[0].raw_position,
        )
        return EarningsCalendarParseResult(
            provider_key=valid.provider_key,
            provider_version=valid.provider_version,
            parser_version=valid.parser_version,
            records=(valid.records[0], duplicate_position),
        )


class NonStringIdParser:
    parser_version = FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> EarningsCalendarParseResult:
        valid = FixtureEarningsCalendarParser().parse(
            raw_content,
            provider_key=provider_key,
            provider_version=provider_version,
        )
        invalid_record = replace(
            valid.records[0],
            provider_event_id=cast(str, 123),
        )
        return EarningsCalendarParseResult(
            provider_key=valid.provider_key,
            provider_version=valid.provider_version,
            parser_version=valid.parser_version,
            records=(invalid_record,),
        )


def _forbidden_row_counts() -> tuple[int, ...]:
    return (
        EarningsEvent.objects.count(),
        EarningsReconciliationDecision.objects.count(),
        SourceEvidence.objects.count(),
        AuditRecord.objects.count(),
        DataChange.objects.count(),
        EarningsDateChange.objects.count(),
        Company.objects.count(),
        SecurityListing.objects.count(),
        IndexMembership.objects.count(),
    )


@pytest.mark.django_db
def test_successful_ingestion_persists_raw_before_parse_and_materializes_observations() -> None:
    source = _calendar_source("success")
    sync_run = _sync_run(source, "success")
    parser = ObservingFixtureParser()

    result = _ingest(
        sync_run=sync_run,
        payload=_fixture_bytes("complete_payload.json"),
        parser=parser,
    )

    assert parser.seen_parser_status == RawDataRecord.ParserStatus.PENDING
    assert parser.seen_observation_count == 1
    assert parser.content_matched_persisted is True
    assert result.raw_record_created is True
    assert result.raw_observation_created is True
    assert result.raw_data_record.parser_status == RawDataRecord.ParserStatus.PARSED
    assert result.raw_data_record.parser_version == FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION
    assert result.raw_data_record.parse_error == ""
    assert result.parse_attempt.status == RawDataParseAttempt.Status.SUCCEEDED
    assert result.parse_attempt.observation_id == result.raw_data_observation.pk
    assert result.parse_attempt.parser_version == FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION
    assert len(result.observations) == 2
    assert result.observations_created == 2
    assert result.observations_reused == 0
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1
    assert RawDataParseAttempt.objects.count() == 1
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db
def test_multi_record_observations_share_raw_lineage_and_stable_positions() -> None:
    source = _calendar_source("multi-record")
    sync_run = _sync_run(source, "multi-record")

    result = _ingest(
        sync_run=sync_run,
        payload=_fixture_bytes("complete_payload.json"),
    )

    assert [observation.raw_position for observation in result.observations] == [1, 2]
    assert [observation.provider_event_id for observation in result.observations] == [
        "fixture-evt-1001",
        "fixture-evt-1002",
    ]
    for observation in result.observations:
        assert observation.raw_data_record_id == result.raw_data_record.pk
        assert observation.source_id == source.pk
        assert observation.provider_key == FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY
        assert observation.provider_version == FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION
        assert observation.parser_version == FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION


@pytest.mark.django_db
def test_empty_payload_succeeds_with_zero_observations() -> None:
    source = _calendar_source("empty")
    sync_run = _sync_run(source, "empty")

    result = _ingest(
        sync_run=sync_run,
        payload=_fixture_bytes("empty_payload.json"),
    )

    assert result.raw_record_created is True
    assert result.raw_observation_created is True
    assert result.parse_attempt.status == RawDataParseAttempt.Status.SUCCEEDED
    assert result.observations == ()
    assert result.observations_created == 0
    assert result.observations_reused == 0
    assert result.raw_data_record.parser_status == RawDataRecord.ParserStatus.PARSED
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_raw_persisted_callback_runs_after_raw_and_before_parse() -> None:
    source = _calendar_source("raw-callback")
    sync_run = _sync_run(source, "raw-callback")
    observed: list[str] = []

    class OrderingParser:
        parser_version = FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION

        def parse(
            self,
            raw_content: bytes,
            *,
            provider_key: str,
            provider_version: str,
        ) -> EarningsCalendarParseResult:
            observed.append("parse")
            return FixtureEarningsCalendarParser().parse(
                raw_content,
                provider_key=provider_key,
                provider_version=provider_version,
            )

    def on_raw_persisted() -> None:
        observed.append("raw")
        assert RawDataRecord.objects.count() == 1
        assert RawDataObservation.objects.count() == 1

    result = ingest_earnings_calendar_payload(
        sync_run=sync_run,
        parser=OrderingParser(),
        raw_content=_fixture_bytes("empty_payload.json"),
        provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
        provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        source_url=SOURCE_URL,
        fetched_at=FETCHED_AT,
        on_raw_persisted=on_raw_persisted,
    )

    assert observed == ["raw", "parse"]
    assert result.parse_attempt.status == RawDataParseAttempt.Status.SUCCEEDED


@pytest.mark.django_db
def test_raw_persisted_callback_runs_when_parse_fails() -> None:
    source = _calendar_source("raw-callback-parse-failure")
    sync_run = _sync_run(source, "raw-callback-parse-failure")
    callback_called = False

    def on_raw_persisted() -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(EarningsCalendarPayloadParseFailure):
        ingest_earnings_calendar_payload(
            sync_run=sync_run,
            parser=FixtureEarningsCalendarParser(),
            raw_content=b"{invalid-json",
            provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
            source_url=SOURCE_URL,
            fetched_at=FETCHED_AT,
            on_raw_persisted=on_raw_persisted,
        )

    assert callback_called is True
    assert RawDataRecord.objects.count() == 1
    assert RawDataParseAttempt.objects.get().status == RawDataParseAttempt.Status.DATA_ERROR


@pytest.mark.django_db
def test_raw_persisted_callback_is_not_called_when_raw_persistence_fails() -> None:
    source = _calendar_source("raw-callback-raw-failure")
    sync_run = _sync_run(source, "raw-callback-raw-failure")
    callback_called = False

    def on_raw_persisted() -> None:
        nonlocal callback_called
        callback_called = True

    with pytest.raises(InvalidRawDataRequest):
        ingest_earnings_calendar_payload(
            sync_run=sync_run,
            parser=FixtureEarningsCalendarParser(),
            raw_content=b'{"authorization":"Bearer fixture-token"}',
            provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
            source_url=SOURCE_URL,
            fetched_at=FETCHED_AT,
            on_raw_persisted=on_raw_persisted,
        )

    assert callback_called is False
    assert RawDataRecord.objects.count() == 0


@pytest.mark.django_db
def test_missing_provider_event_id_preserves_raw_and_records_unsupported_attempt() -> None:
    source = _calendar_source("missing-id")
    sync_run = _sync_run(source, "missing-id")

    with pytest.raises(EarningsCalendarUnsupportedIdentity) as excinfo:
        _ingest(
            sync_run=sync_run,
            payload=_fixture_bytes("missing_provider_event_id.json"),
        )

    failure = excinfo.value
    assert failure.parse_attempt.status == RawDataParseAttempt.Status.UNSUPPORTED
    assert failure.parse_attempt.observation_id == failure.raw_data_observation.pk
    assert "raw_position 2" in failure.parse_attempt.error_summary
    assert failure.raw_data_record.parser_status == RawDataRecord.ParserStatus.UNSUPPORTED
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1
    assert RawDataParseAttempt.objects.count() == 1
    assert EarningsCalendarObservation.objects.count() == 0
    assert EarningsEvent.objects.count() == 0
    assert EarningsReconciliationDecision.objects.count() == 0


@pytest.mark.django_db
def test_blank_provider_event_id_records_unsupported_attempt() -> None:
    source = _calendar_source("blank-id")
    sync_run = _sync_run(source, "blank-id")

    with pytest.raises(EarningsCalendarUnsupportedIdentity) as excinfo:
        _ingest(
            sync_run=sync_run,
            payload=_payload_with_events([{"provider_event_id": "   "}]),
        )

    assert excinfo.value.parse_attempt.status == RawDataParseAttempt.Status.UNSUPPORTED
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_malformed_payload_preserves_raw_and_records_data_error() -> None:
    source = _calendar_source("malformed-payload")
    sync_run = _sync_run(source, "malformed-payload")

    with pytest.raises(EarningsCalendarPayloadParseFailure) as excinfo:
        _ingest(sync_run=sync_run, payload=b"{invalid-json")

    failure = excinfo.value
    assert failure.parse_attempt.status == RawDataParseAttempt.Status.DATA_ERROR
    assert failure.raw_data_record.parser_status == RawDataRecord.ParserStatus.FAILED
    assert failure.raw_data_record.parse_error
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_partially_malformed_payload_fails_closed_without_partial_observations() -> None:
    source = _calendar_source("partial-malformed")
    sync_run = _sync_run(source, "partial-malformed")

    with pytest.raises(EarningsCalendarPayloadParseFailure) as excinfo:
        _ingest(
            sync_run=sync_run,
            payload=_fixture_bytes("partially_malformed.json"),
        )

    assert excinfo.value.parse_attempt.status == RawDataParseAttempt.Status.DATA_ERROR
    assert "raw_position 2" in excinfo.value.parse_attempt.error_summary
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_parser_context_error_records_system_error_after_raw_persistence() -> None:
    source = _calendar_source("context-error")
    sync_run = _sync_run(source, "context-error")

    with pytest.raises(EarningsCalendarParserSystemFailure) as excinfo:
        _ingest(
            sync_run=sync_run,
            payload=_fixture_bytes("complete_payload.json"),
            parser=RaisingParser(EarningsCalendarParserContextError("bad parser context")),
        )

    assert excinfo.value.parse_attempt.status == RawDataParseAttempt.Status.SYSTEM_ERROR
    assert excinfo.value.raw_data_record.parser_status == RawDataRecord.ParserStatus.FAILED
    assert RawDataRecord.objects.count() == 1
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_unexpected_parser_exception_is_redacted_in_system_error_attempt() -> None:
    source = _calendar_source("unexpected-parser")
    sync_run = _sync_run(source, "unexpected-parser")

    with pytest.raises(EarningsCalendarParserSystemFailure) as excinfo:
        _ingest(
            sync_run=sync_run,
            payload=_fixture_bytes("complete_payload.json"),
            parser=RaisingParser(RuntimeError("password=fixture-secret-token")),
        )

    assert excinfo.value.parse_attempt.status == RawDataParseAttempt.Status.SYSTEM_ERROR
    assert "fixture-secret-token" not in excinfo.value.parse_attempt.error_summary
    assert "Unexpected earnings calendar parser error" in excinfo.value.parse_attempt.error_summary
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_same_raw_and_parser_version_replay_does_not_duplicate_rows() -> None:
    source = _calendar_source("same-run-replay")
    sync_run = _sync_run(source, "same-run-replay")
    payload = _fixture_bytes("complete_payload.json")

    first = _ingest(sync_run=sync_run, payload=payload)
    second = _ingest(sync_run=sync_run, payload=payload)

    assert first.raw_record_created is True
    assert second.raw_record_created is False
    assert second.raw_observation_created is False
    assert second.parse_attempt.pk == first.parse_attempt.pk
    assert second.observations_created == 0
    assert second.observations_reused == 2
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1
    assert RawDataParseAttempt.objects.count() == 1
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db
def test_new_sync_run_reuses_raw_record_and_normalized_observations() -> None:
    source = _calendar_source("new-run-replay")
    first_run = _sync_run(source, "new-run-replay-1")
    second_run = _sync_run(source, "new-run-replay-2")
    payload = _fixture_bytes("complete_payload.json")

    first = _ingest(sync_run=first_run, payload=payload)
    second = _ingest(sync_run=second_run, payload=payload)

    assert first.raw_record_created is True
    assert second.raw_record_created is False
    assert second.raw_observation_created is True
    assert second.observations_created == 0
    assert second.observations_reused == 2
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 2
    assert RawDataParseAttempt.objects.count() == 2
    assert EarningsCalendarObservation.objects.count() == 2
    assert second.raw_data_record.pk == first.raw_data_record.pk


@pytest.mark.django_db
def test_parser_result_metadata_mismatch_fails_closed_before_observations() -> None:
    source = _calendar_source("metadata-mismatch")
    sync_run = _sync_run(source, "metadata-mismatch")

    with pytest.raises(EarningsCalendarParserSystemFailure) as excinfo:
        _ingest(
            sync_run=sync_run,
            payload=_fixture_bytes("complete_payload.json"),
            parser=MismatchedResultParser(),
        )

    assert excinfo.value.parse_attempt.status == RawDataParseAttempt.Status.SYSTEM_ERROR
    assert "provider_key" in excinfo.value.parse_attempt.error_summary
    assert RawDataRecord.objects.count() == 1
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_duplicate_normalized_provider_event_ids_fail_closed_before_observations() -> None:
    source = _calendar_source("duplicate-result")
    sync_run = _sync_run(source, "duplicate-result")

    with pytest.raises(EarningsCalendarParserSystemFailure) as excinfo:
        _ingest(
            sync_run=sync_run,
            payload=_fixture_bytes("complete_payload.json"),
            parser=DuplicateRecordParser(),
        )

    assert excinfo.value.parse_attempt.status == RawDataParseAttempt.Status.SYSTEM_ERROR
    assert "duplicate provider_event_id" in excinfo.value.parse_attempt.error_summary
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_duplicate_normalized_raw_positions_fail_closed_before_observations() -> None:
    source = _calendar_source("duplicate-position")
    sync_run = _sync_run(source, "duplicate-position")

    with pytest.raises(EarningsCalendarParserSystemFailure) as excinfo:
        _ingest(
            sync_run=sync_run,
            payload=_fixture_bytes("complete_payload.json"),
            parser=DuplicatePositionParser(),
        )

    assert excinfo.value.parse_attempt.status == RawDataParseAttempt.Status.SYSTEM_ERROR
    assert "raw_position" in excinfo.value.parse_attempt.error_summary
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_non_string_provider_event_id_fails_closed_before_observations() -> None:
    source = _calendar_source("non-string-id")
    sync_run = _sync_run(source, "non-string-id")

    with pytest.raises(EarningsCalendarParserSystemFailure) as excinfo:
        _ingest(
            sync_run=sync_run,
            payload=_fixture_bytes("complete_payload.json"),
            parser=NonStringIdParser(),
        )

    assert excinfo.value.parse_attempt.status == RawDataParseAttempt.Status.SYSTEM_ERROR
    assert "provider_event_id" in excinfo.value.parse_attempt.error_summary
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_non_earnings_source_is_rejected_before_raw_persistence() -> None:
    source = DataSource.objects.create(
        key="fixture-manual-ingestion-source",
        name="Fixture manual source",
        source_type=DataSource.SourceType.MANUAL,
        base_url="https://fixture-manual.test/",
        provider_adapter=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
        license_notes="Synthetic test-only source.",
    )
    sync_run = _sync_run(source, "manual-source")

    with pytest.raises(InvalidEarningsCalendarIngestion, match="earnings_calendar"):
        _ingest(sync_run=sync_run, payload=_fixture_bytes("empty_payload.json"))

    assert RawDataRecord.objects.count() == 0
    assert RawDataObservation.objects.count() == 0
    assert RawDataParseAttempt.objects.count() == 0


@pytest.mark.django_db
def test_provider_key_mismatch_is_rejected_before_raw_persistence() -> None:
    source = _calendar_source("provider-mismatch")
    sync_run = _sync_run(source, "provider-mismatch")

    with pytest.raises(InvalidEarningsCalendarIngestion, match="provider_adapter"):
        _ingest(
            sync_run=sync_run,
            payload=_fixture_bytes("empty_payload.json"),
            provider_key="other-fixture-provider",
        )

    assert RawDataRecord.objects.count() == 0


@pytest.mark.django_db
def test_finished_sync_run_is_rejected_before_raw_persistence() -> None:
    source = _calendar_source("finished-run")
    sync_run = _sync_run(source, "finished-run")
    mark_sync_run_succeeded(sync_run.pk)

    with pytest.raises(InvalidEarningsCalendarIngestion, match="running"):
        _ingest(sync_run=sync_run, payload=_fixture_bytes("empty_payload.json"))

    assert RawDataRecord.objects.count() == 0


@pytest.mark.django_db
def test_ingestion_writes_only_raw_lineage_and_normalized_observations() -> None:
    source = _calendar_source("side-effects")
    sync_run = _sync_run(source, "side-effects")
    sync_runs_before = SyncRun.objects.count()
    forbidden_before = _forbidden_row_counts()

    _ingest(
        sync_run=sync_run,
        payload=_fixture_bytes("complete_payload.json"),
    )

    assert SyncRun.objects.count() == sync_runs_before
    assert _forbidden_row_counts() == forbidden_before
    sync_run.refresh_from_db()
    assert sync_run.status == SyncRun.Status.RUNNING
    assert sync_run.fetched_count == 0
    assert sync_run.created_count == 0
    assert sync_run.updated_count == 0
    assert sync_run.skipped_count == 0
    assert sync_run.failed_count == 0
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1
    assert RawDataParseAttempt.objects.count() == 1
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db
def test_ingestion_does_not_open_network_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ingestion must not open network connections")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", fail_connect)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", fail_connect)

    source = _calendar_source("no-network")
    sync_run = _sync_run(source, "no-network")
    result = _ingest(
        sync_run=sync_run,
        payload=_fixture_bytes("complete_payload.json"),
    )

    assert len(result.observations) == 2


@pytest.mark.django_db
def test_observation_persistence_failure_keeps_raw_and_first_observation() -> None:
    source = _calendar_source("persistence-failure")
    sync_run = _sync_run(source, "persistence-failure")
    real_record = cast(
        Callable[..., EarningsCalendarObservationWriteResult],
        real_record_earnings_calendar_observation,
    )
    call_count = 0

    def fail_on_second_record(**kwargs: object) -> EarningsCalendarObservationWriteResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return real_record(**kwargs)
        raise RuntimeError("simulated observation persistence failure")

    with mock.patch(
        "earnings.services.calendar_ingestion.record_earnings_calendar_observation",
        side_effect=fail_on_second_record,
    ):
        with pytest.raises(RuntimeError, match="simulated observation persistence failure"):
            _ingest(
                sync_run=sync_run,
                payload=_fixture_bytes("complete_payload.json"),
            )

    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1
    assert RawDataParseAttempt.objects.get().status == RawDataParseAttempt.Status.SUCCEEDED
    assert EarningsCalendarObservation.objects.count() == 1


@pytest.mark.django_db
def test_replay_after_partial_persistence_failure_materializes_remaining_observation() -> None:
    source = _calendar_source("persistence-recovery")
    sync_run = _sync_run(source, "persistence-recovery")
    real_record = cast(
        Callable[..., EarningsCalendarObservationWriteResult],
        real_record_earnings_calendar_observation,
    )
    call_count = 0

    def fail_on_second_record(**kwargs: object) -> EarningsCalendarObservationWriteResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return real_record(**kwargs)
        raise RuntimeError("simulated observation persistence failure")

    with mock.patch(
        "earnings.services.calendar_ingestion.record_earnings_calendar_observation",
        side_effect=fail_on_second_record,
    ):
        with pytest.raises(RuntimeError):
            _ingest(
                sync_run=sync_run,
                payload=_fixture_bytes("complete_payload.json"),
            )

    recovered = _ingest(
        sync_run=sync_run,
        payload=_fixture_bytes("complete_payload.json"),
    )

    assert recovered.observations_created == 1
    assert recovered.observations_reused == 1
    assert EarningsCalendarObservation.objects.count() == 2
