import hashlib
from datetime import UTC, datetime
from urllib.parse import unquote

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


@pytest.mark.parametrize("credential_field", ("key", "auth", "api_key", "X-API-KEY"))
def test_request_fingerprint_is_stable_and_ignores_secret_values(
    credential_field: str,
) -> None:
    first = build_request_fingerprint(
        method="get",
        source_url=f"https://EXAMPLE.test/data?page=2&{credential_field}=first-fixture-secret",
        request_identity={"cursor": "next", "Authorization": "Bearer first-fixture-token"},
    )
    second = build_request_fingerprint(
        method="GET",
        source_url=(f"https://example.test/data?{credential_field}=second-fixture-secret&page=2"),
        request_identity={"Authorization": "Bearer second-fixture-token", "cursor": "next"},
    )

    assert first == second
    assert len(first) == 64


def test_request_fingerprint_preserves_safe_request_conditions() -> None:
    first = build_request_fingerprint(
        method="GET",
        source_url="https://example.test/data?limit=25&page=1",
        request_identity={"cursor": "first"},
    )
    reordered = build_request_fingerprint(
        method="get",
        source_url="https://EXAMPLE.test/data?page=1&limit=25#ignored-fragment",
        request_identity={"cursor": "first"},
    )
    different_page = build_request_fingerprint(
        method="GET",
        source_url="https://example.test/data?limit=25&page=2",
        request_identity={"cursor": "first"},
    )

    assert first == reordered
    assert first != different_page


def test_request_fingerprint_redacts_nested_identity_credentials() -> None:
    first = build_request_fingerprint(
        method="GET",
        source_url="https://example.test/data",
        request_identity={
            "nested": [
                {"AuTh": "Basic dXNlcjpwYXNz"},
                ({"headers": {"Authorization": "Bearer first-fixture-token"}},),
            ]
        },
    )
    second = build_request_fingerprint(
        method="GET",
        source_url="https://example.test/data",
        request_identity={
            "nested": [
                {"AuTh": "Basic YWRtaW46c2VjcmV0"},
                ({"headers": {"Authorization": "Bearer second-fixture-token"}},),
            ]
        },
    )

    assert first == second


def test_plain_basic_and_bearer_words_are_not_treated_as_credentials() -> None:
    basic = build_request_fingerprint(
        method="GET",
        source_url="https://example.test/data",
        request_identity={"description": "basic"},
    )
    bearer = build_request_fingerprint(
        method="GET",
        source_url="https://example.test/data",
        request_identity={"description": "bearer"},
    )

    assert basic != bearer


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
@pytest.mark.parametrize("credential_field", ("key", "auth", "api_key", "API-KEY"))
def test_raw_data_service_redacts_sensitive_query_values(
    sync_run: SyncRun,
    credential_field: str,
) -> None:
    result = record_raw_data_observation(
        sync_run=sync_run,
        source_url=(
            f"https://example.test/data?page=1&{credential_field}=fixture-query-secret"
            "#fragment-secret"
        ),
        payload=b"safe-payload",
    )

    stored_url = unquote(result.record.source_url)
    assert "fixture-query-secret" not in stored_url
    assert "fragment-secret" not in stored_url
    assert "[REDACTED]" in stored_url
    assert "page=1" in stored_url


@pytest.mark.django_db
def test_raw_data_service_rejects_url_userinfo_without_echoing_it(sync_run: SyncRun) -> None:
    source_url = "https://fixture-user:fixture-password@example.test/data"

    with pytest.raises(InvalidRawDataRequest) as error:
        record_raw_data_observation(
            sync_run=sync_run,
            source_url=source_url,
            payload=b"safe-payload",
        )

    assert "fixture-user" not in str(error.value)
    assert "fixture-password" not in str(error.value)
    assert RawDataRecord.objects.count() == 0


@pytest.mark.django_db
def test_raw_data_service_rejects_credential_payload_without_echoing_it(
    sync_run: SyncRun,
) -> None:
    payload = b'{"nested":{"ToKeN":"fixture-payload-secret"}}'

    with pytest.raises(InvalidRawDataRequest) as error:
        record_raw_data_observation(
            sync_run=sync_run,
            source_url="https://example.test/data",
            payload=payload,
        )

    assert "fixture-payload-secret" not in str(error.value)
    assert RawDataRecord.objects.count() == 0


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
