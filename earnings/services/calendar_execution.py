"""Owned scheduled execution and explicit retry for earnings-calendar windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.models import DataSource, RawDataObservation, SyncRun
from audit.services import (
    mark_sync_run_failed,
    mark_sync_run_partial,
    update_sync_run_counts,
)
from earnings.calendar_parsing import EarningsCalendarParser
from earnings.services.calendar_pagination import (
    EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    EarningsCalendarPageSource,
    EarningsCalendarWindowResult,
    run_earnings_calendar_window,
)
from earnings.services.calendar_replay_foundation import (
    retire_stale_earnings_calendar_replay_run,
)
from earnings.services.calendar_run_ownership import (
    EarningsCalendarRunBusy,
    calendar_run_ownership,
)
from earnings.services.calendar_sync_identity import (
    EARNINGS_CALENDAR_SCOPE_FIELDS,
    EarningsCalendarWindowKind,
    _start_retry_earnings_calendar_sync_run,
    build_earnings_calendar_sync_scope,
    start_scheduled_earnings_calendar_sync_run,
)


class InvalidEarningsCalendarRetry(ValueError):
    """The requested retry has no valid failed or partial source run."""


class EarningsCalendarRetryContextMismatch(InvalidEarningsCalendarRetry):
    """The caller's pool hash disagrees with the original persisted scope."""


class EarningsCalendarRunCountMismatch(RuntimeError):
    """A stale run has more counted pages than persisted raw observations."""


@dataclass(frozen=True, slots=True)
class EarningsCalendarExecutionResult:
    sync_run: SyncRun
    created: bool
    window_result: EarningsCalendarWindowResult | None


def execute_scheduled_earnings_calendar_window(
    *,
    source: DataSource,
    page_source: EarningsCalendarPageSource,
    parser: EarningsCalendarParser,
    provider_key: str,
    provider_version: str,
    window_start: date,
    window_end: date,
    monitoring_pool_as_of: date,
    monitoring_pool_hash: str,
    selector_version: str,
    schedule_bucket: str,
    code_version: str = "",
) -> EarningsCalendarExecutionResult:
    """Own start through finalization, including page fetches outside transactions."""

    _require_autocommit()
    if source.pk is None or source._state.adding:
        raise ValueError("source must be saved before execution.")
    with calendar_run_ownership(source_id=source.pk, job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE):
        _retire_stale_runs(source_id=source.pk)
        started = start_scheduled_earnings_calendar_sync_run(
            source=source,
            provider_key=provider_key,
            window_start=window_start,
            window_end=window_end,
            monitoring_pool_as_of=monitoring_pool_as_of,
            monitoring_pool_hash=monitoring_pool_hash,
            selector_version=selector_version,
            schedule_bucket=schedule_bucket,
            provider_version=provider_version,
            code_version=code_version,
            parser_version=parser.parser_version,
        )
        if not started.created:
            return EarningsCalendarExecutionResult(
                sync_run=started.sync_run, created=False, window_result=None
            )
        window = run_earnings_calendar_window(
            sync_run=started.sync_run,
            page_source=page_source,
            parser=parser,
            provider_key=provider_key,
            provider_version=provider_version,
        )
        return EarningsCalendarExecutionResult(
            sync_run=window.sync_run, created=True, window_result=window
        )


def execute_retry_earnings_calendar_window(
    *,
    previous_run: SyncRun,
    request_id: str,
    expected_pool_hash: str,
    page_source: EarningsCalendarPageSource,
    parser: EarningsCalendarParser,
    provider_version: str,
    code_version: str = "",
) -> EarningsCalendarExecutionResult:
    """Refetch a failed window from its first page under a new run identity."""

    _require_autocommit()
    if previous_run.pk is None or previous_run._state.adding:
        raise InvalidEarningsCalendarRetry("previous_run must be saved.")
    original = SyncRun.objects.select_related("source").get(pk=previous_run.pk)
    if original.job_type != EARNINGS_CALENDAR_WINDOW_JOB_TYPE:
        raise InvalidEarningsCalendarRetry("previous_run has the wrong job type.")
    with calendar_run_ownership(
        source_id=original.source_id, job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE
    ):
        _retire_stale_runs(source_id=original.source_id)
        original.refresh_from_db()
        if original.status not in (SyncRun.Status.FAILED, SyncRun.Status.PARTIAL):
            raise InvalidEarningsCalendarRetry("previous_run must be failed or partial.")
        scope = _validated_original_scope(original.scope)
        if expected_pool_hash != scope["monitoring_pool_hash"]:
            raise EarningsCalendarRetryContextMismatch(
                "Retry pool hash differs from the original SyncRun scope."
            )
        provider_key = cast(str, scope["provider_key"])
        started = _start_retry_earnings_calendar_sync_run(
            source=original.source,
            provider_key=provider_key,
            window_start=date.fromisoformat(cast(str, scope["window_start"])),
            window_end=date.fromisoformat(cast(str, scope["window_end"])),
            monitoring_pool_as_of=date.fromisoformat(cast(str, scope["monitoring_pool_as_of"])),
            monitoring_pool_hash=cast(str, scope["monitoring_pool_hash"]),
            selector_version=cast(str, scope["selector_version"]),
            request_id=request_id,
            provider_version=provider_version,
            code_version=code_version,
            parser_version=parser.parser_version,
        )
        if not started.created:
            return EarningsCalendarExecutionResult(
                sync_run=started.sync_run, created=False, window_result=None
            )
        window = run_earnings_calendar_window(
            sync_run=started.sync_run,
            page_source=page_source,
            parser=parser,
            provider_key=provider_key,
            provider_version=provider_version,
        )
        return EarningsCalendarExecutionResult(
            sync_run=window.sync_run, created=True, window_result=window
        )


def _require_autocommit() -> None:
    if not transaction.get_autocommit():
        raise RuntimeError("Earnings calendar execution must not run inside a transaction.")


def _retire_stale_runs(*, source_id: UUID) -> None:
    """Resolve orphaned runs only while this session owns the source/job lock."""

    cutoff = timezone.now() - timedelta(seconds=settings.EARNINGS_CALENDAR_STALE_AFTER_SECONDS)
    with transaction.atomic():
        running = list(
            SyncRun.objects.select_for_update()
            .filter(
                source_id=source_id,
                job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
                status=SyncRun.Status.RUNNING,
            )
            .order_by("started_at", "pk")
        )
        if any(sync_run.heartbeat_at > cutoff for sync_run in running):
            raise EarningsCalendarRunBusy(
                "An earnings calendar run is still within its heartbeat grace period."
            )
        for sync_run in running:
            if sync_run.run_mode == SyncRun.RunMode.REPLAY:
                retire_stale_earnings_calendar_replay_run(
                    sync_run,
                    cutoff=cutoff,
                )
                continue
            observation_count = RawDataObservation.objects.filter(sync_run=sync_run).count()
            if sync_run.fetched_count > observation_count:
                raise EarningsCalendarRunCountMismatch(
                    "A stale earnings calendar run counted pages with no raw observation."
                )
            if sync_run.fetched_count < observation_count:
                update_sync_run_counts(
                    sync_run.pk, fetched_delta=observation_count - sync_run.fetched_count
                )
            update_sync_run_counts(sync_run.pk, failed_delta=1)
            summary = "Earnings calendar run lost ownership and exceeded its heartbeat threshold."
            if observation_count:
                mark_sync_run_partial(sync_run.pk, error_summary=summary)
            else:
                mark_sync_run_failed(sync_run.pk, error_summary=summary)


def _validated_original_scope(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(EARNINGS_CALENDAR_SCOPE_FIELDS):
        raise InvalidEarningsCalendarRetry("Previous SyncRun scope is not canonical.")
    try:
        canonical = build_earnings_calendar_sync_scope(
            provider_key=value["provider_key"],
            window_kind=EarningsCalendarWindowKind(value["window_kind"]),
            window_start=date.fromisoformat(value["window_start"]),
            window_end=date.fromisoformat(value["window_end"]),
            monitoring_pool_as_of=date.fromisoformat(value["monitoring_pool_as_of"]),
            monitoring_pool_hash=value["monitoring_pool_hash"],
            selector_version=value["selector_version"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidEarningsCalendarRetry("Previous SyncRun scope is invalid.") from error
    if canonical != value:
        raise InvalidEarningsCalendarRetry("Previous SyncRun scope is not canonical.")
    return canonical
