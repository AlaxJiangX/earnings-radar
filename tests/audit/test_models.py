from datetime import UTC, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import (
    AppendOnlyRecordError,
    DataSource,
    RawDataObservation,
    RawDataParseAttempt,
    SyncRun,
)


@pytest.mark.django_db
def test_data_source_key_is_unique(data_source: DataSource) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        DataSource.objects.create(
            key=data_source.key,
            name="Duplicate source",
            source_type=DataSource.SourceType.MANUAL,
        )


@pytest.mark.django_db
def test_data_source_rejects_unknown_source_type(data_source: DataSource) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        DataSource.objects.filter(pk=data_source.pk).update(source_type="unknown")


@pytest.mark.parametrize(
    "base_url",
    (
        "https://fixture-user:fixture-password@example.test/data",
        "https://example.test/data?key=fixture-secret",
        "https://example.test/data?AuTh=fixture-secret",
        "https://example.test/data?API-KEY=fixture-secret",
        "https://example.test/data?next=Bearer%20fixture-token",
    ),
)
def test_data_source_base_url_model_validation_rejects_credentials(base_url: str) -> None:
    source = DataSource(
        key="unsafe-fixture-source",
        name="Unsafe fixture source",
        source_type=DataSource.SourceType.MANUAL,
        base_url=base_url,
    )

    with pytest.raises(ValidationError) as error:
        source.full_clean(validate_unique=False, validate_constraints=False)

    message = str(error.value)
    assert "fixture-secret" not in message
    assert "fixture-password" not in message
    assert "fixture-token" not in message


def test_data_source_base_url_model_validation_allows_safe_query_conditions() -> None:
    source = DataSource(
        key="safe-fixture-source",
        name="Safe fixture source",
        source_type=DataSource.SourceType.MANUAL,
        base_url="https://example.test/data?limit=25&page=2",
    )

    source.full_clean(validate_unique=False, validate_constraints=False)


@pytest.mark.django_db
def test_sync_run_window_key_is_unique_per_source_and_job(data_source: DataSource) -> None:
    values = {
        "source": data_source,
        "job_type": "fixture.sync",
        "idempotency_key": "fixture.sync:2026-07-13",
    }
    SyncRun.objects.create(**values)

    with pytest.raises(IntegrityError), transaction.atomic():
        SyncRun.objects.create(**values)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field_name",
    ("fetched_count", "created_count", "updated_count", "skipped_count", "failed_count"),
)
def test_sync_run_counts_cannot_be_negative(
    data_source: DataSource,
    field_name: str,
) -> None:
    sync_run = SyncRun.objects.create(
        source=data_source,
        job_type="fixture.sync",
        idempotency_key=f"fixture.sync:{field_name}",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        SyncRun.objects.filter(pk=sync_run.pk).update(**{field_name: -1})


@pytest.mark.django_db
def test_sync_run_finished_at_cannot_precede_started_at(data_source: DataSource) -> None:
    started_at = timezone.now()
    sync_run = SyncRun.objects.create(
        source=data_source,
        job_type="fixture.sync",
        idempotency_key="fixture.sync:bad-finish",
        started_at=started_at,
        heartbeat_at=started_at,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        SyncRun.objects.filter(pk=sync_run.pk).update(
            status=SyncRun.Status.FAILED,
            finished_at=started_at - timedelta(seconds=1),
        )


@pytest.mark.django_db
def test_sync_run_terminal_status_requires_finished_at(data_source: DataSource) -> None:
    sync_run = SyncRun.objects.create(
        source=data_source,
        job_type="fixture.sync",
        idempotency_key="fixture.sync:missing-finish",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        SyncRun.objects.filter(pk=sync_run.pk).update(status=SyncRun.Status.SUCCEEDED)


@pytest.mark.django_db
def test_sync_run_rejects_unknown_status(data_source: DataSource) -> None:
    sync_run = SyncRun.objects.create(
        source=data_source,
        job_type="fixture.sync",
        idempotency_key="fixture.sync:unknown-status",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        SyncRun.objects.filter(pk=sync_run.pk).update(status="unknown")


@pytest.mark.django_db
def test_sync_run_timestamps_are_timezone_aware_and_utc(data_source: DataSource) -> None:
    sync_run = SyncRun.objects.create(
        source=data_source,
        job_type="fixture.sync",
        idempotency_key="fixture.sync:utc",
    )
    sync_run.refresh_from_db()

    assert timezone.is_aware(sync_run.started_at)
    assert timezone.is_aware(sync_run.heartbeat_at)
    assert sync_run.started_at.astimezone(UTC).utcoffset() == timedelta(0)
    assert sync_run.heartbeat_at.astimezone(UTC).utcoffset() == timedelta(0)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status,error_summary",
    (
        (RawDataParseAttempt.Status.SUCCEEDED, ""),
        (RawDataParseAttempt.Status.DATA_ERROR, "Controlled data error."),
        (RawDataParseAttempt.Status.SYSTEM_ERROR, "Controlled system error."),
        (RawDataParseAttempt.Status.UNSUPPORTED, "Controlled unsupported format."),
    ),
)
def test_raw_data_parse_attempt_can_record_terminal_outcome(
    raw_data_observation: RawDataObservation,
    status: str,
    error_summary: str,
) -> None:
    started_at = timezone.now()
    finished_at = started_at + timedelta(seconds=1)
    attempt = RawDataParseAttempt.objects.create(
        observation=raw_data_observation,
        parser_version="test-parser",
        status=status,
        error_summary=error_summary,
        started_at=started_at,
        finished_at=finished_at,
    )

    assert attempt.observation_id == raw_data_observation.pk
    assert attempt.status == status
    assert attempt.error_summary == error_summary
    assert list(raw_data_observation.parse_attempts.all()) == [attempt]


@pytest.mark.django_db
def test_raw_data_parse_attempt_enforces_canonical_outcome_per_observation_and_version(
    raw_data_observation: RawDataObservation,
) -> None:
    started_at = timezone.now()
    RawDataParseAttempt.objects.create(
        observation=raw_data_observation,
        parser_version="test-parser",
        status=RawDataParseAttempt.Status.SUCCEEDED,
        error_summary="",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        RawDataParseAttempt.objects.create(
            observation=raw_data_observation,
            parser_version="test-parser",
            status=RawDataParseAttempt.Status.DATA_ERROR,
            error_summary="Different outcome.",
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=2),
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "error_summary"),
    (
        (RawDataParseAttempt.Status.SUCCEEDED, "Must be empty."),
        (RawDataParseAttempt.Status.DATA_ERROR, ""),
        (RawDataParseAttempt.Status.DATA_ERROR, "   "),
        (RawDataParseAttempt.Status.SYSTEM_ERROR, ""),
        (RawDataParseAttempt.Status.UNSUPPORTED, ""),
    ),
)
def test_raw_data_parse_attempt_constraints_reject_inconsistent_state(
    raw_data_observation: RawDataObservation,
    status: str,
    error_summary: str,
) -> None:
    started_at = timezone.now()
    with pytest.raises(IntegrityError), transaction.atomic():
        RawDataParseAttempt.objects.create(
            observation=raw_data_observation,
            parser_version="test-parser",
            status=status,
            error_summary=error_summary,
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=1),
        )


@pytest.mark.django_db
@pytest.mark.parametrize("parser_version", ("", "   "))
def test_raw_data_parse_attempt_rejects_blank_parser_version(
    raw_data_observation: RawDataObservation,
    parser_version: str,
) -> None:
    started_at = timezone.now()
    with pytest.raises(IntegrityError), transaction.atomic():
        RawDataParseAttempt.objects.create(
            observation=raw_data_observation,
            parser_version=parser_version,
            status=RawDataParseAttempt.Status.SUCCEEDED,
            error_summary="",
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=1),
        )


@pytest.mark.django_db
def test_raw_data_parse_attempt_rejects_finished_before_started(
    raw_data_observation: RawDataObservation,
) -> None:
    started_at = timezone.now()
    with pytest.raises(IntegrityError), transaction.atomic():
        RawDataParseAttempt.objects.create(
            observation=raw_data_observation,
            parser_version="test-parser",
            status=RawDataParseAttempt.Status.SUCCEEDED,
            error_summary="",
            started_at=started_at,
            finished_at=started_at - timedelta(seconds=1),
        )


@pytest.mark.django_db
def test_raw_data_parse_attempt_rejects_unknown_status(
    raw_data_observation: RawDataObservation,
) -> None:
    started_at = timezone.now()
    with pytest.raises(IntegrityError), transaction.atomic():
        RawDataParseAttempt.objects.create(
            observation=raw_data_observation,
            parser_version="test-parser",
            status="unknown",
            error_summary="",
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=1),
        )


@pytest.mark.django_db
def test_raw_data_parse_attempt_is_append_only(
    raw_data_observation: RawDataObservation,
) -> None:
    started_at = timezone.now()
    attempt = RawDataParseAttempt.objects.create(
        observation=raw_data_observation,
        parser_version="test-parser",
        status=RawDataParseAttempt.Status.SUCCEEDED,
        error_summary="",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
    )

    with pytest.raises(AppendOnlyRecordError):
        attempt.save()
    with pytest.raises(AppendOnlyRecordError):
        attempt.delete()
    with pytest.raises(AppendOnlyRecordError):
        RawDataParseAttempt.objects.filter(pk=attempt.pk).update(status="data_error")
    with pytest.raises(AppendOnlyRecordError):
        RawDataParseAttempt.objects.filter(pk=attempt.pk).delete()

    attempt.refresh_from_db()
    assert attempt.status == RawDataParseAttempt.Status.SUCCEEDED


@pytest.mark.django_db
def test_raw_data_parse_attempt_rejects_bulk_update(
    raw_data_observation: RawDataObservation,
) -> None:
    started_at = timezone.now()
    attempt = RawDataParseAttempt.objects.create(
        observation=raw_data_observation,
        parser_version="test-parser",
        status=RawDataParseAttempt.Status.SUCCEEDED,
        error_summary="",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
    )

    with pytest.raises(AppendOnlyRecordError):
        RawDataParseAttempt.objects.bulk_update([attempt], fields=["status"])

    attempt.refresh_from_db()
    assert attempt.status == RawDataParseAttempt.Status.SUCCEEDED


@pytest.mark.django_db
def test_raw_data_parse_attempt_allows_distinct_parser_versions(
    raw_data_observation: RawDataObservation,
) -> None:
    started_at = timezone.now()
    first = RawDataParseAttempt.objects.create(
        observation=raw_data_observation,
        parser_version="parser-v1",
        status=RawDataParseAttempt.Status.SUCCEEDED,
        error_summary="",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
    )
    second = RawDataParseAttempt.objects.create(
        observation=raw_data_observation,
        parser_version="parser-v2",
        status=RawDataParseAttempt.Status.DATA_ERROR,
        error_summary="Parser v2 failed.",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=2),
    )

    assert set(raw_data_observation.parse_attempts.all()) == {first, second}
