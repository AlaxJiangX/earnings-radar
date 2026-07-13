from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from audit.models import DataSource, SyncRun
from audit.services import (
    InvalidSyncRunCount,
    InvalidSyncRunTimestamp,
    InvalidSyncRunTransition,
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    start_sync_run,
    update_sync_run_counts,
)


@pytest.mark.django_db
def test_start_sync_run_is_idempotent_for_same_window(data_source: DataSource) -> None:
    first = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={"date": "2026-07-13"},
        idempotency_key="fixture.sync:2026-07-13",
        code_version="test-code",
        parser_version="test-parser",
    )
    second = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={"date": "ignored-on-retry"},
        idempotency_key="fixture.sync:2026-07-13",
    )

    assert first.pk == second.pk
    assert SyncRun.objects.count() == 1
    assert first.status == SyncRun.Status.RUNNING
    assert first.scope == {"date": "2026-07-13"}
    assert first.started_at == first.heartbeat_at


@pytest.mark.django_db
def test_update_sync_run_counts_updates_all_counters(data_source: DataSource) -> None:
    sync_run = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={},
        idempotency_key="fixture.sync:counts",
    )

    updated = update_sync_run_counts(
        sync_run.pk,
        fetched_delta=10,
        created_delta=2,
        updated_delta=3,
        skipped_delta=4,
        failed_delta=1,
    )

    assert updated.fetched_count == 10
    assert updated.created_count == 2
    assert updated.updated_count == 3
    assert updated.skipped_count == 4
    assert updated.failed_count == 1
    assert updated.heartbeat_at >= updated.started_at


@pytest.mark.django_db
def test_update_sync_run_counts_rejects_negative_delta(data_source: DataSource) -> None:
    sync_run = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={},
        idempotency_key="fixture.sync:negative",
    )

    with pytest.raises(InvalidSyncRunCount):
        update_sync_run_counts(sync_run.pk, fetched_delta=-1)


@pytest.mark.django_db
def test_mark_sync_run_succeeded_sets_terminal_times(data_source: DataSource) -> None:
    started_at = datetime(2026, 7, 13, 1, tzinfo=UTC)
    finished_at = started_at + timedelta(minutes=2)
    sync_run = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={},
        idempotency_key="fixture.sync:success",
        started_at=started_at,
    )

    finished = mark_sync_run_succeeded(sync_run.pk, finished_at=finished_at)

    assert finished.status == SyncRun.Status.SUCCEEDED
    assert finished.finished_at == finished_at
    assert finished.heartbeat_at == finished_at
    assert finished.error_summary == ""


@pytest.mark.django_db
def test_mark_sync_run_partial_sanitizes_error_summary(data_source: DataSource) -> None:
    sync_run = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={},
        idempotency_key="fixture.sync:partial",
    )

    finished = mark_sync_run_partial(
        sync_run.pk,
        error_summary="One record failed; api_key=do-not-store",
    )

    assert finished.status == SyncRun.Status.PARTIAL
    assert "do-not-store" not in finished.error_summary
    assert "[REDACTED]" in finished.error_summary


@pytest.mark.django_db
def test_mark_sync_run_failed_redacts_credentials_and_url_queries(
    data_source: DataSource,
) -> None:
    sync_run = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={},
        idempotency_key="fixture.sync:failed",
    )

    finished = mark_sync_run_failed(
        sync_run.pk,
        error_summary=(
            "Authorization: Bearer secret-token password=secret-password "
            "https://example.test/path?token=url-secret"
        ),
    )

    assert finished.status == SyncRun.Status.FAILED
    assert finished.finished_at is not None
    assert "secret-token" not in finished.error_summary
    assert "secret-password" not in finished.error_summary
    assert "url-secret" not in finished.error_summary
    assert finished.error_summary.count("[REDACTED]") >= 3


@pytest.mark.django_db
def test_finished_sync_run_cannot_transition_again(data_source: DataSource) -> None:
    sync_run = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={},
        idempotency_key="fixture.sync:terminal",
    )
    mark_sync_run_failed(sync_run.pk, error_summary="Permanent fixture failure")

    with pytest.raises(InvalidSyncRunTransition):
        mark_sync_run_succeeded(sync_run.pk)


@pytest.mark.django_db
def test_success_rejects_nonzero_failed_count(data_source: DataSource) -> None:
    sync_run = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={},
        idempotency_key="fixture.sync:failed-count",
    )
    update_sync_run_counts(sync_run.pk, failed_delta=1)

    with pytest.raises(InvalidSyncRunTransition):
        mark_sync_run_succeeded(sync_run.pk)


@pytest.mark.django_db
def test_service_rejects_naive_timestamps(data_source: DataSource) -> None:
    with pytest.raises(InvalidSyncRunTimestamp):
        start_sync_run(
            job_type="fixture.sync",
            source=data_source,
            scope={},
            idempotency_key="fixture.sync:naive",
            started_at=datetime(2026, 7, 13, 1),
        )


@pytest.mark.django_db
def test_service_rejects_finish_before_start(data_source: DataSource) -> None:
    sync_run = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={},
        idempotency_key="fixture.sync:finish-before-start",
    )

    with pytest.raises(InvalidSyncRunTimestamp):
        mark_sync_run_failed(
            sync_run.pk,
            error_summary="Fixture failure",
            finished_at=sync_run.started_at - timedelta(seconds=1),
        )


@pytest.mark.django_db
def test_service_timestamps_remain_timezone_aware(data_source: DataSource) -> None:
    sync_run = start_sync_run(
        job_type="fixture.sync",
        source=data_source,
        scope={},
        idempotency_key="fixture.sync:aware",
    )
    finished = mark_sync_run_failed(sync_run.pk, error_summary="Fixture failure")

    assert timezone.is_aware(finished.started_at)
    assert finished.finished_at is not None
    assert timezone.is_aware(finished.finished_at)
