from datetime import UTC, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import DataSource, SyncRun


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
