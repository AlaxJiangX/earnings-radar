"""Offline replay orchestration for persisted earnings-calendar raw evidence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import NoReturn, cast

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import (
    DataSource,
    RawDataObservation,
    RawDataParseAttempt,
    RawDataRecord,
    SyncRun,
)
from audit.security import sanitize_error_summary
from audit.services import (
    RawDataIntegrityError,
    RawDataParseIntegrityError,
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    record_raw_data_parse_attempt,
    record_replay_raw_data_observation,
    update_sync_run_counts,
)
from earnings.calendar_parsing import (
    EarningsCalendarParser,
    EarningsCalendarParserContextError,
    EarningsCalendarPayloadError,
    UnsupportedEarningsCalendarIdentityError,
)
from earnings.services.calendar import (
    EarningsCalendarObservationIntegrityError,
    EarningsCalendarObservationServiceError,
)
from earnings.services.calendar_ingestion import (
    EarningsCalendarIngestionIntegrityError,
    persist_earnings_calendar_parse_result,
    validate_earnings_calendar_parse_result,
)
from earnings.services.calendar_pagination import EARNINGS_CALENDAR_WINDOW_JOB_TYPE
from earnings.services.calendar_replay_foundation import (
    EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
    EarningsCalendarReplayCountMismatch,
    InvalidEarningsCalendarReplay,
    build_earnings_calendar_replay_input_digest,
    load_earnings_calendar_replay_evidence,
    reconcile_earnings_calendar_replayed_count,
    start_earnings_calendar_replay_sync_run,
    validate_earnings_calendar_replay_pool_contract,
    validate_earnings_calendar_replay_source,
)
from earnings.services.calendar_run_ownership import (
    EarningsCalendarRunOwnershipLost,
    assert_calendar_run_ownership,
    calendar_run_ownership,
)

MAX_REPLAY_ERROR_SUMMARY_LENGTH = 2000


class EarningsCalendarReplayOrchestrationError(RuntimeError):
    """Base class for replay orchestration failures after validation."""


class EarningsCalendarReplayExecutionFailure(EarningsCalendarReplayOrchestrationError):
    """A replay run reached a terminal failure state."""

    def __init__(self, message: str, *, sync_run: SyncRun) -> None:
        super().__init__(message)
        self.sync_run = sync_run


class EarningsCalendarReplayPersistenceFailure(EarningsCalendarReplayExecutionFailure):
    """A replay run failed while persisting parse or normalized facts."""


class EarningsCalendarReplayDigestMismatch(EarningsCalendarReplayOrchestrationError):
    """The source evidence manifest changed after replay context was fixed."""


@dataclass(frozen=True, slots=True)
class EarningsCalendarOfflineReplayResult:
    sync_run: SyncRun
    executed: bool
    parse_failures: int


@dataclass(frozen=True, slots=True)
class _ReplayExecutionContext:
    source: DataSource
    source_run: SyncRun
    parser: EarningsCalendarParser
    provider_key: str
    provider_version: str
    parser_version: str
    replay_contract_version: str


def execute_earnings_calendar_offline_replay(
    *,
    source: DataSource,
    source_sync_run: SyncRun | uuid.UUID,
    parser: EarningsCalendarParser,
    monitoring_pool_as_of: date | None = None,
    monitoring_pool_hash: str | None = None,
    selector_version: str | None = None,
    replay_contract_version: str = EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
    code_version: str = "",
    started_at: datetime | None = None,
) -> EarningsCalendarOfflineReplayResult:
    """Execute or reuse one deterministic offline replay run without Provider access."""

    _require_autocommit()
    if not isinstance(source, DataSource) or source._state.adding or source.pk is None:
        raise InvalidEarningsCalendarReplay("source must be a saved DataSource.")
    with calendar_run_ownership(
        source_id=source.pk,
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    ):
        context = _build_execution_context(
            source=source,
            source_sync_run=source_sync_run,
            parser=parser,
            monitoring_pool_as_of=monitoring_pool_as_of,
            monitoring_pool_hash=monitoring_pool_hash,
            selector_version=selector_version,
            replay_contract_version=replay_contract_version,
        )
        stale_cutoff = timezone.now() - timedelta(
            seconds=settings.EARNINGS_CALENDAR_STALE_AFTER_SECONDS
        )
        started = start_earnings_calendar_replay_sync_run(
            source=context.source,
            source_sync_run=context.source_run,
            parser_version=context.parser_version,
            replay_contract_version=context.replay_contract_version,
            monitoring_pool_as_of=monitoring_pool_as_of,
            monitoring_pool_hash=monitoring_pool_hash,
            selector_version=selector_version,
            code_version=code_version,
            started_at=started_at,
            resume_stale_before=stale_cutoff,
        )
        replay_run = started.sync_run
        if not started.created and replay_run.status != SyncRun.Status.RUNNING:
            return EarningsCalendarOfflineReplayResult(
                sync_run=replay_run,
                executed=False,
                parse_failures=0,
            )

        if not started.created:
            update_sync_run_counts(replay_run.pk, heartbeat_at=timezone.now())
            replay_run = reconcile_earnings_calendar_replayed_count(replay_run.pk)

        try:
            return _execute_running_replay(
                context=context,
                replay_run=replay_run,
            )
        except EarningsCalendarRunOwnershipLost:
            raise
        except EarningsCalendarReplayCountMismatch:
            raise
        except Exception as error:
            assert_calendar_run_ownership(
                source_id=context.source.pk,
                job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
            )
            _terminate_replay_failure(
                replay_run=replay_run,
                error=error,
            )


def _execute_running_replay(
    *,
    context: _ReplayExecutionContext,
    replay_run: SyncRun,
) -> EarningsCalendarOfflineReplayResult:
    _verify_replay_digest(context=context, replay_run=replay_run)
    evidence = load_earnings_calendar_replay_evidence(
        source_sync_run=context.source_run,
    )

    parse_failures = 0
    for source_observation in evidence:
        assert_calendar_run_ownership(
            source_id=context.source.pk,
            job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        )
        raw_record = source_observation.raw_data_record
        replay_observation = record_replay_raw_data_observation(
            sync_run=replay_run,
            raw_data_record=raw_record,
        ).observation
        assert_calendar_run_ownership(
            source_id=context.source.pk,
            job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        )
        if _replay_one_evidence(
            context=context,
            replay_observation=replay_observation,
            raw_record=raw_record,
        ):
            parse_failures += 1

    assert_calendar_run_ownership(
        source_id=context.source.pk,
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    )
    _verify_replay_digest(context=context, replay_run=replay_run)
    reconcile_earnings_calendar_replayed_count(replay_run.pk)
    assert_calendar_run_ownership(
        source_id=context.source.pk,
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    )

    if parse_failures:
        update_sync_run_counts(replay_run.pk, failed_delta=parse_failures)
        finalized = mark_sync_run_partial(
            replay_run.pk,
            error_summary=(f"Offline replay completed with {parse_failures} parser failure(s)."),
        )
    elif context.source_run.status != SyncRun.Status.SUCCEEDED:
        finalized = mark_sync_run_partial(
            replay_run.pk,
            error_summary=(
                "Source ingestion run was partial or failed; "
                "offline replay preserves incomplete-window semantics."
            ),
        )
    else:
        finalized = mark_sync_run_succeeded(replay_run.pk)

    return EarningsCalendarOfflineReplayResult(
        sync_run=finalized,
        executed=True,
        parse_failures=parse_failures,
    )


def _replay_one_evidence(
    *,
    context: _ReplayExecutionContext,
    replay_observation: RawDataObservation,
    raw_record: RawDataRecord,
) -> bool:
    parser_started_at = timezone.now()
    try:
        parse_result = context.parser.parse(
            bytes(raw_record.payload),
            provider_key=context.provider_key,
            provider_version=context.provider_version,
        )
    except UnsupportedEarningsCalendarIdentityError as error:
        _record_replay_parse_attempt(
            replay_observation=replay_observation,
            parser_version=context.parser_version,
            status=RawDataParseAttempt.Status.UNSUPPORTED,
            error_summary=_safe_reason(error, default="Missing stable provider_event_id."),
            started_at=parser_started_at,
        )
        return True
    except EarningsCalendarParserContextError as error:
        _record_replay_parse_attempt(
            replay_observation=replay_observation,
            parser_version=context.parser_version,
            status=RawDataParseAttempt.Status.SYSTEM_ERROR,
            error_summary=_safe_reason(
                error, default="Earnings calendar parser context is invalid."
            ),
            started_at=parser_started_at,
        )
        return True
    except EarningsCalendarRunOwnershipLost:
        raise
    except EarningsCalendarPayloadError as error:
        _record_replay_parse_attempt(
            replay_observation=replay_observation,
            parser_version=context.parser_version,
            status=RawDataParseAttempt.Status.DATA_ERROR,
            error_summary=_safe_reason(error, default="Earnings calendar payload is malformed."),
            started_at=parser_started_at,
        )
        return True
    except Exception as error:
        _record_replay_parse_attempt(
            replay_observation=replay_observation,
            parser_version=context.parser_version,
            status=RawDataParseAttempt.Status.SYSTEM_ERROR,
            error_summary=f"Unexpected replay parser error ({type(error).__name__}).",
            started_at=parser_started_at,
        )
        return True

    assert_calendar_run_ownership(
        source_id=context.source.pk,
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    )
    try:
        validate_earnings_calendar_parse_result(
            parse_result=parse_result,
            provider_key=context.provider_key,
            provider_version=context.provider_version,
            parser_version=context.parser_version,
        )
    except EarningsCalendarIngestionIntegrityError as error:
        _record_replay_parse_attempt(
            replay_observation=replay_observation,
            parser_version=context.parser_version,
            status=RawDataParseAttempt.Status.SYSTEM_ERROR,
            error_summary=_safe_reason(error, default="Replay parser result is invalid."),
            started_at=parser_started_at,
        )
        return True

    _record_replay_parse_attempt(
        replay_observation=replay_observation,
        parser_version=context.parser_version,
        status=RawDataParseAttempt.Status.SUCCEEDED,
        error_summary="",
        started_at=parser_started_at,
    )
    assert_calendar_run_ownership(
        source_id=context.source.pk,
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    )
    persist_earnings_calendar_parse_result(
        source=context.source,
        provider_key=context.provider_key,
        provider_version=context.provider_version,
        parser_version=context.parser_version,
        raw_record=raw_record,
        parse_result=parse_result,
    )
    return False


def _record_replay_parse_attempt(
    *,
    replay_observation: RawDataObservation,
    parser_version: str,
    status: str,
    error_summary: str,
    started_at: datetime,
) -> None:
    with transaction.atomic():
        record_raw_data_parse_attempt(
            observation=replay_observation,
            parser_version=parser_version,
            status=status,
            error_summary=error_summary,
            started_at=started_at,
            finished_at=timezone.now(),
        )


def _build_execution_context(
    *,
    source: DataSource,
    source_sync_run: SyncRun | uuid.UUID,
    parser: EarningsCalendarParser,
    monitoring_pool_as_of: date | None,
    monitoring_pool_hash: str | None,
    selector_version: str | None,
    replay_contract_version: str,
) -> _ReplayExecutionContext:
    if not isinstance(source, DataSource) or source._state.adding or source.pk is None:
        raise InvalidEarningsCalendarReplay("source must be a saved DataSource.")
    try:
        persisted_source = DataSource.objects.get(pk=source.pk)
    except DataSource.DoesNotExist as error:
        raise InvalidEarningsCalendarReplay("source no longer exists.") from error
    source_run = validate_earnings_calendar_replay_source(
        source_sync_run=source_sync_run,
        source=persisted_source,
    )
    if not isinstance(parser, EarningsCalendarParser):
        raise InvalidEarningsCalendarReplay("parser must implement EarningsCalendarParser.")
    parser_version = _required_text(parser.parser_version, "parser_version", maximum=100)
    provider_version = _required_text(
        source_run.provider_version,
        "provider_version",
        maximum=100,
    )
    scope = source_run.scope
    if not isinstance(scope, dict):
        raise InvalidEarningsCalendarReplay("source SyncRun scope must be a JSON object.")
    if monitoring_pool_as_of is None:
        monitoring_pool_as_of = date.fromisoformat(cast(str, scope["monitoring_pool_as_of"]))
    if monitoring_pool_hash is None:
        monitoring_pool_hash = cast(str, scope["monitoring_pool_hash"])
    if selector_version is None:
        selector_version = cast(str, scope["selector_version"])
    validate_earnings_calendar_replay_pool_contract(
        source_sync_run=source_run,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
    )
    provider_key = cast(str, scope["provider_key"])
    return _ReplayExecutionContext(
        source=persisted_source,
        source_run=source_run,
        parser=parser,
        provider_key=provider_key,
        provider_version=provider_version,
        parser_version=parser_version,
        replay_contract_version=replay_contract_version,
    )


def _verify_replay_digest(
    *,
    context: _ReplayExecutionContext,
    replay_run: SyncRun,
) -> None:
    rebuilt = build_earnings_calendar_replay_input_digest(
        source_sync_run=context.source_run,
        parser_version=context.parser_version,
        replay_contract_version=context.replay_contract_version,
    )
    if rebuilt != replay_run.replay_input_digest:
        raise EarningsCalendarReplayDigestMismatch(
            "Replay evidence digest changed after the replay identity was fixed."
        )
    if replay_run.provider_version != context.provider_version:
        raise EarningsCalendarReplayDigestMismatch(
            "Replay provider context changed after the replay identity was fixed."
        )


def _terminate_replay_failure(
    *,
    replay_run: SyncRun,
    error: Exception,
) -> NoReturn:
    current = SyncRun.objects.get(pk=replay_run.pk)
    summary = _safe_reason(error, default="Offline replay execution failed.")
    if current.status != SyncRun.Status.RUNNING:
        raise EarningsCalendarReplayExecutionFailure(summary, sync_run=current) from error
    reconciled = reconcile_earnings_calendar_replayed_count(replay_run.pk)
    update_sync_run_counts(replay_run.pk, failed_delta=1)
    if reconciled.replayed_count:
        finalized = mark_sync_run_partial(
            replay_run.pk,
            error_summary=summary,
        )
    else:
        finalized = mark_sync_run_failed(
            replay_run.pk,
            error_summary=summary,
        )
    failure_type: type[EarningsCalendarReplayExecutionFailure]
    if isinstance(
        error,
        (
            RawDataIntegrityError,
            RawDataParseIntegrityError,
            EarningsCalendarIngestionIntegrityError,
            EarningsCalendarObservationIntegrityError,
            EarningsCalendarObservationServiceError,
            IntegrityError,
        ),
    ):
        failure_type = EarningsCalendarReplayPersistenceFailure
    else:
        failure_type = EarningsCalendarReplayExecutionFailure
    raise failure_type(summary, sync_run=finalized) from error


def _require_autocommit() -> None:
    if not transaction.get_autocommit():
        raise RuntimeError("Offline replay must not run inside a transaction.")


def _required_text(value: object, value_name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsCalendarReplay(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise InvalidEarningsCalendarReplay(f"{value_name} must be non-empty and bounded.")
    return normalized


def _safe_reason(error: Exception, *, default: str) -> str:
    try:
        message = str(error)
    except Exception:
        message = default
    sanitized = sanitize_error_summary(
        message,
        maximum_length=MAX_REPLAY_ERROR_SUMMARY_LENGTH,
    )
    return sanitized or default
