import uuid
from collections.abc import Mapping
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import DataSource, SyncRun
from audit.security import (
    InvalidAuditValue,
    SensitiveAuditData,
    normalize_json_without_credentials,
    sanitize_error_summary,
)

MAX_ERROR_SUMMARY_LENGTH = 2000


class InvalidSyncRunTransition(RuntimeError):
    pass


class InvalidSyncRunCount(ValueError):
    pass


class InvalidSyncRunTimestamp(ValueError):
    pass


def _aware_timestamp(value: datetime | None = None) -> datetime:
    result = value or timezone.now()
    if timezone.is_naive(result):
        raise InvalidSyncRunTimestamp("SyncRun timestamps must be timezone-aware.")
    return result


def start_sync_run(
    *,
    job_type: str,
    source: DataSource,
    scope: Mapping[str, object] | None,
    idempotency_key: str,
    code_version: str = "",
    parser_version: str = "",
    started_at: datetime | None = None,
) -> SyncRun:
    normalized_job_type = job_type.strip()
    normalized_key = idempotency_key.strip()
    if not normalized_job_type or not normalized_key:
        raise ValueError("job_type and idempotency_key must not be empty.")
    try:
        normalized_scope = normalize_json_without_credentials(
            dict(scope or {}),
            value_name="SyncRun scope",
        )
    except (InvalidAuditValue, SensitiveAuditData) as error:
        raise ValueError(str(error)) from None
    if not isinstance(normalized_scope, dict):
        raise ValueError("SyncRun scope must be a JSON object.")

    timestamp = _aware_timestamp(started_at)
    with transaction.atomic():
        try:
            with transaction.atomic():
                return SyncRun.objects.create(
                    job_type=normalized_job_type,
                    source=source,
                    scope=normalized_scope,
                    idempotency_key=normalized_key,
                    started_at=timestamp,
                    heartbeat_at=timestamp,
                    code_version=code_version,
                    parser_version=parser_version,
                )
        except IntegrityError:
            existing = SyncRun.objects.filter(
                job_type=normalized_job_type,
                source=source,
                idempotency_key=normalized_key,
            ).first()
            if existing is None:
                raise
            return existing


def update_sync_run_counts(
    sync_run_id: uuid.UUID,
    *,
    fetched_delta: int = 0,
    created_delta: int = 0,
    updated_delta: int = 0,
    skipped_delta: int = 0,
    failed_delta: int = 0,
    heartbeat_at: datetime | None = None,
) -> SyncRun:
    deltas = (fetched_delta, created_delta, updated_delta, skipped_delta, failed_delta)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in deltas):
        raise InvalidSyncRunCount("SyncRun count deltas must be non-negative integers.")

    with transaction.atomic():
        sync_run = SyncRun.objects.select_for_update().get(pk=sync_run_id)
        _require_running(sync_run)
        sync_run.fetched_count += fetched_delta
        sync_run.created_count += created_delta
        sync_run.updated_count += updated_delta
        sync_run.skipped_count += skipped_delta
        sync_run.failed_count += failed_delta
        sync_run.heartbeat_at = _aware_timestamp(heartbeat_at)
        sync_run.save(
            update_fields=(
                "fetched_count",
                "created_count",
                "updated_count",
                "skipped_count",
                "failed_count",
                "heartbeat_at",
            )
        )
        return sync_run


def mark_sync_run_succeeded(
    sync_run_id: uuid.UUID,
    *,
    finished_at: datetime | None = None,
) -> SyncRun:
    return _finish_sync_run(
        sync_run_id,
        status=SyncRun.Status.SUCCEEDED,
        error_summary="",
        finished_at=finished_at,
    )


def mark_sync_run_partial(
    sync_run_id: uuid.UUID,
    *,
    error_summary: str,
    finished_at: datetime | None = None,
) -> SyncRun:
    return _finish_sync_run(
        sync_run_id,
        status=SyncRun.Status.PARTIAL,
        error_summary=error_summary,
        finished_at=finished_at,
    )


def mark_sync_run_failed(
    sync_run_id: uuid.UUID,
    *,
    error_summary: str,
    finished_at: datetime | None = None,
) -> SyncRun:
    return _finish_sync_run(
        sync_run_id,
        status=SyncRun.Status.FAILED,
        error_summary=error_summary,
        finished_at=finished_at,
    )


def _finish_sync_run(
    sync_run_id: uuid.UUID,
    *,
    status: SyncRun.Status,
    error_summary: str,
    finished_at: datetime | None,
) -> SyncRun:
    with transaction.atomic():
        sync_run = SyncRun.objects.select_for_update().get(pk=sync_run_id)
        _require_running(sync_run)
        timestamp = _aware_timestamp(finished_at)
        if timestamp < sync_run.started_at:
            raise InvalidSyncRunTimestamp("finished_at must not be earlier than started_at.")

        sanitized_summary = sanitize_error_summary(
            error_summary,
            maximum_length=MAX_ERROR_SUMMARY_LENGTH,
        )
        if status in (SyncRun.Status.PARTIAL, SyncRun.Status.FAILED) and not sanitized_summary:
            raise ValueError("Partial and failed SyncRuns require a non-empty error summary.")
        if status == SyncRun.Status.SUCCEEDED and sync_run.failed_count:
            raise InvalidSyncRunTransition(
                "A SyncRun with failed records must be marked partial or failed."
            )

        sync_run.status = status
        sync_run.finished_at = timestamp
        sync_run.heartbeat_at = timestamp
        sync_run.error_summary = sanitized_summary
        sync_run.save(update_fields=("status", "finished_at", "heartbeat_at", "error_summary"))
        return sync_run


def _require_running(sync_run: SyncRun) -> None:
    if sync_run.status != SyncRun.Status.RUNNING:
        raise InvalidSyncRunTransition(
            f"SyncRun {sync_run.pk} is {sync_run.status!r}; only running runs can change."
        )
