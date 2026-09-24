import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
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
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidSyncRunTransition(RuntimeError):
    pass


class InvalidSyncRunCount(ValueError):
    pass


class InvalidSyncRunTimestamp(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SyncRunStartResult:
    sync_run: SyncRun
    created: bool


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
    run_mode: SyncRun.RunMode | str = SyncRun.RunMode.INGESTION,
    replay_source_sync_run: SyncRun | None = None,
    replay_contract_version: str = "",
    replay_input_digest: str = "",
) -> SyncRun:
    return start_sync_run_with_result(
        job_type=job_type,
        source=source,
        scope=scope,
        idempotency_key=idempotency_key,
        code_version=code_version,
        parser_version=parser_version,
        started_at=started_at,
        run_mode=run_mode,
        replay_source_sync_run=replay_source_sync_run,
        replay_contract_version=replay_contract_version,
        replay_input_digest=replay_input_digest,
    ).sync_run


def start_sync_run_with_result(
    *,
    job_type: str,
    source: DataSource,
    scope: Mapping[str, object] | None,
    idempotency_key: str,
    code_version: str = "",
    parser_version: str = "",
    started_at: datetime | None = None,
    run_mode: SyncRun.RunMode | str = SyncRun.RunMode.INGESTION,
    replay_source_sync_run: SyncRun | None = None,
    replay_contract_version: str = "",
    replay_input_digest: str = "",
) -> SyncRunStartResult:
    """Start a run and report whether this caller created it.

    The ``created`` flag lets orchestration code distinguish ownership from a
    concurrent/idempotent replay without weakening the database unique
    constraint.  ``start_sync_run`` remains the compatibility wrapper for
    callers that only need the run object.
    """
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
    try:
        normalized_run_mode = SyncRun.RunMode(run_mode)
    except ValueError as error:
        raise ValueError("run_mode must be ingestion or replay.") from error
    if not isinstance(replay_contract_version, str) or not isinstance(replay_input_digest, str):
        raise ValueError("Replay contract metadata must be strings.")
    normalized_contract_version = replay_contract_version.strip()
    normalized_input_digest = replay_input_digest.strip()
    is_replay_scope = normalized_scope.get("window_kind") == "replay"
    if normalized_run_mode == SyncRun.RunMode.INGESTION and (
        is_replay_scope
        or replay_source_sync_run is not None
        or normalized_contract_version
        or normalized_input_digest
    ):
        raise ValueError("Ingestion SyncRuns cannot contain replay metadata.")
    if normalized_run_mode == SyncRun.RunMode.REPLAY:
        if not is_replay_scope:
            raise ValueError('Replay SyncRuns require scope window_kind="replay".')
        if replay_source_sync_run is None:
            raise ValueError("Replay SyncRuns require a source SyncRun.")
        if (
            replay_source_sync_run._state.adding
            or replay_source_sync_run.pk is None
            or replay_source_sync_run.run_mode != SyncRun.RunMode.INGESTION
        ):
            raise ValueError("Replay source SyncRun must be a saved ingestion run.")
        if replay_source_sync_run.source_id != source.pk:
            raise ValueError("Replay source SyncRun must belong to the same DataSource.")
        if replay_source_sync_run.job_type != normalized_job_type:
            raise ValueError("Replay source SyncRun must use the same job_type.")
        if not normalized_contract_version or len(normalized_contract_version) > 100:
            raise ValueError("Replay SyncRuns require replay contract metadata.")
        if not _SHA256_HEX_RE.fullmatch(normalized_input_digest):
            raise ValueError("Replay input digest must be a SHA-256 hex digest.")
        if not isinstance(parser_version, str) or not parser_version.strip():
            raise ValueError("Replay SyncRuns require a parser version.")
    timestamp = _aware_timestamp(started_at)
    with transaction.atomic():
        try:
            with transaction.atomic():
                return SyncRunStartResult(
                    sync_run=SyncRun.objects.create(
                        job_type=normalized_job_type,
                        source=source,
                        scope=normalized_scope,
                        idempotency_key=normalized_key,
                        started_at=timestamp,
                        heartbeat_at=timestamp,
                        code_version=code_version,
                        parser_version=parser_version,
                        run_mode=normalized_run_mode,
                        replay_source_sync_run=replay_source_sync_run,
                        replay_contract_version=normalized_contract_version,
                        replay_input_digest=normalized_input_digest,
                    ),
                    created=True,
                )
        except IntegrityError:
            existing = SyncRun.objects.filter(
                job_type=normalized_job_type,
                source=source,
                idempotency_key=normalized_key,
            ).first()
            if existing is None:
                raise
            _validate_existing_start_context(
                existing,
                source=source,
                job_type=normalized_job_type,
                scope=normalized_scope,
                run_mode=normalized_run_mode,
                replay_source_sync_run=replay_source_sync_run,
                replay_contract_version=normalized_contract_version,
                replay_input_digest=normalized_input_digest,
            )
            return SyncRunStartResult(sync_run=existing, created=False)


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
        if sync_run.run_mode == SyncRun.RunMode.REPLAY and fetched_delta:
            raise InvalidSyncRunCount("Replay SyncRuns cannot record provider fetches.")
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


def _validate_existing_start_context(
    sync_run: SyncRun,
    *,
    source: DataSource,
    job_type: str,
    scope: Mapping[str, object],
    run_mode: SyncRun.RunMode,
    replay_source_sync_run: SyncRun | None,
    replay_contract_version: str,
    replay_input_digest: str,
) -> None:
    if run_mode == SyncRun.RunMode.INGESTION:
        if (
            sync_run.source_id != source.pk
            or sync_run.job_type != job_type
            or sync_run.run_mode != SyncRun.RunMode.INGESTION
            or sync_run.replay_source_sync_run_id is not None
            or sync_run.replay_contract_version
            or sync_run.replay_input_digest
        ):
            raise ValueError("Existing ingestion SyncRun contains replay metadata.")
        return
    if (
        sync_run.source_id != source.pk
        or sync_run.job_type != job_type
        or sync_run.scope != dict(scope)
        or sync_run.run_mode != run_mode
        or replay_source_sync_run is None
        or sync_run.replay_source_sync_run_id != replay_source_sync_run.pk
        or sync_run.replay_contract_version != replay_contract_version
        or sync_run.replay_input_digest != replay_input_digest
    ):
        raise ValueError("Existing replay SyncRun has different replay context.")
