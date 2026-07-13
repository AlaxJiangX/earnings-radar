import hashlib
from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from audit.constants import RAW_DATA_PAYLOAD_DB_LIMIT_BYTES
from audit.models import DataSource, RawDataObservation, RawDataRecord, SyncRun
from audit.services import (
    InvalidRawDataRequest,
    InvalidRawDataTimestamp,
    PayloadTooLarge,
    build_request_fingerprint,
    mark_raw_data_parse_failed,
    mark_raw_data_parsed,
    mark_raw_data_unsupported,
    mark_sync_run_succeeded,
    record_raw_data_observation,
    start_sync_run,
)


def test_request_fingerprint_is_stable_and_ignores_secret_values() -> None:
    first = build_request_fingerprint(
        method="get",
        source_url="https://EXAMPLE.test/data?page=2&api_key=first-secret",
        request_identity={"cursor": "next", "Authorization": "Bearer first"},
    )
    second = build_request_fingerprint(
        method="GET",
        source_url="https://example.test/data?api_key=second-secret&page=2",
        request_identity={"Authorization": "Bearer second", "cursor": "next"},
    )

    assert first == second
    assert len(first) == 64


@pytest.mark.django_db
def test_raw_data_service_calculates_hash_size_and_sanitizes_url(sync_run: SyncRun) -> None:
    payload = b'{"fixture": true}'

    result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data?page=1&token=do-not-store",
        payload=payload,
        http_status=200,
        content_type="application/json",
        encoding="utf-8",
    )

    assert result.record_created is True
    assert result.observation_created is True
    assert result.record.content_hash == hashlib.sha256(payload).hexdigest()
    assert result.record.payload_size_bytes == len(payload)
    assert bytes(result.record.payload) == payload
    assert result.record.parser_status == RawDataRecord.ParserStatus.PENDING
    assert "do-not-store" not in result.record.source_url
    assert "REDACTED" in result.record.source_url
    assert timezone.is_aware(result.record.fetched_at)
    assert timezone.is_aware(result.observation.observed_at)


@pytest.mark.django_db
def test_same_run_and_payload_are_fully_idempotent(sync_run: SyncRun) -> None:
    first = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data?page=1",
        payload=b"same-payload",
    )
    second = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data?page=1",
        payload=b"same-payload",
    )

    assert first.record.pk == second.record.pk
    assert first.observation.pk == second.observation.pk
    assert second.record_created is False
    assert second.observation_created is False
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1


@pytest.mark.django_db
def test_new_run_reuses_payload_and_adds_observation(
    data_source: DataSource,
    sync_run: SyncRun,
) -> None:
    first = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data?page=1",
        payload=b"same-payload",
    )
    later_run = start_sync_run(
        job_type="fixture.raw-data",
        source=data_source,
        scope={"fixture": True},
        idempotency_key="fixture.raw-data:later",
    )

    second = record_raw_data_observation(
        sync_run=later_run,
        source_url="https://example.test/data?page=1",
        payload=b"same-payload",
    )

    assert second.record.pk == first.record.pk
    assert second.record.first_sync_run_id == sync_run.pk
    assert second.record_created is False
    assert second.observation_created is True
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 2


@pytest.mark.django_db
def test_changed_payload_creates_new_raw_record(sync_run: SyncRun) -> None:
    first = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data",
        payload=b"version-one",
    )
    second = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data",
        payload=b"version-two",
    )

    assert first.record.pk != second.record.pk
    assert RawDataRecord.objects.count() == 2
    assert RawDataObservation.objects.count() == 2


@pytest.mark.django_db
@override_settings(RAW_DATA_MAX_PAYLOAD_BYTES=8)
def test_payload_limit_is_enforced_before_storage(sync_run: SyncRun) -> None:
    with pytest.raises(PayloadTooLarge):
        record_raw_data_observation(
            sync_run=sync_run,
            source_url="https://example.test/data",
            payload=b"123456789",
        )

    assert RawDataRecord.objects.count() == 0
    assert RawDataObservation.objects.count() == 0


@pytest.mark.django_db
def test_database_rejects_payload_size_mismatch(sync_run: SyncRun) -> None:
    result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data",
        payload=b"payload",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        RawDataRecord.objects.filter(pk=result.record.pk).update(payload_size_bytes=1)


@pytest.mark.django_db
def test_database_enforces_hard_payload_limit(sync_run: SyncRun) -> None:
    payload = b"x" * (RAW_DATA_PAYLOAD_DB_LIMIT_BYTES + 1)

    with pytest.raises(IntegrityError), transaction.atomic():
        RawDataRecord.objects.create(
            source=sync_run.source,
            first_sync_run=sync_run,
            source_url="https://example.test/oversized",
            request_fingerprint=hashlib.sha256(b"oversized-request").hexdigest(),
            fetched_at=timezone.now(),
            content_hash=hashlib.sha256(payload).hexdigest(),
            payload=payload,
            payload_size_bytes=len(payload),
        )


@pytest.mark.django_db
def test_database_rejects_invalid_hash_format(sync_run: SyncRun) -> None:
    result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data",
        payload=b"payload",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        RawDataRecord.objects.filter(pk=result.record.pk).update(content_hash="invalid")


@pytest.mark.django_db
def test_observation_is_unique_per_run_and_record(sync_run: SyncRun) -> None:
    result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data",
        payload=b"payload",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        RawDataObservation.objects.create(
            sync_run=sync_run,
            raw_data_record=result.record,
        )


@pytest.mark.django_db
def test_parse_status_transitions_and_error_redaction(sync_run: SyncRun) -> None:
    result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data",
        payload=b"payload",
    )

    failed = mark_raw_data_parse_failed(
        result.record.pk,
        parser_version="parser-v1",
        parse_error="password=do-not-store malformed fixture",
    )
    assert failed.parser_status == RawDataRecord.ParserStatus.FAILED
    assert "do-not-store" not in failed.parse_error
    assert "REDACTED" in failed.parse_error

    parsed = mark_raw_data_parsed(result.record.pk, parser_version="parser-v2")
    assert parsed.parser_status == RawDataRecord.ParserStatus.PARSED
    assert parsed.parser_version == "parser-v2"
    assert parsed.parse_error == ""

    unsupported = mark_raw_data_unsupported(result.record.pk, parser_version="parser-v3")
    assert unsupported.parser_status == RawDataRecord.ParserStatus.UNSUPPORTED
    assert unsupported.parser_version == "parser-v3"
    assert unsupported.parse_error == ""


@pytest.mark.django_db
def test_database_rejects_inconsistent_parser_state(sync_run: SyncRun) -> None:
    result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data",
        payload=b"payload",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        RawDataRecord.objects.filter(pk=result.record.pk).update(
            parser_status=RawDataRecord.ParserStatus.FAILED,
            parser_version="parser-v1",
            parse_error="",
        )


@pytest.mark.django_db
def test_service_rejects_naive_raw_timestamps(sync_run: SyncRun) -> None:
    with pytest.raises(InvalidRawDataTimestamp):
        record_raw_data_observation(
            sync_run=sync_run,
            source_url="https://example.test/data",
            payload=b"payload",
            fetched_at=datetime(2026, 7, 13, 1),
        )


@pytest.mark.django_db
def test_service_rejects_finished_sync_run(sync_run: SyncRun) -> None:
    mark_sync_run_succeeded(sync_run.pk)

    with pytest.raises(InvalidRawDataRequest):
        record_raw_data_observation(
            sync_run=sync_run,
            source_url="https://example.test/data",
            payload=b"payload",
        )


@pytest.mark.django_db
def test_explicit_utc_timestamps_are_preserved(sync_run: SyncRun) -> None:
    timestamp = datetime(2026, 7, 13, 1, tzinfo=UTC)

    result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/data",
        payload=b"payload",
        fetched_at=timestamp,
        observed_at=timestamp,
    )

    assert result.record.fetched_at == timestamp
    assert result.observation.observed_at == timestamp
