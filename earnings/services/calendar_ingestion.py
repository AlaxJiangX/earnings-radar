"""Raw-first earnings calendar ingestion foundation.

This service owns one payload's raw lineage, parse attempt, and normalized
observation materialization.  It deliberately does not own pagination, SyncRun
scope/idempotency, windows, locks, or replay orchestration; those belong to
later 4.2C sub-stages.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from audit.models import (
    DataSource,
    RawDataObservation,
    RawDataParseAttempt,
    RawDataRecord,
    SyncRun,
)
from audit.security import ProviderRequestContextDescriptor, sanitize_error_summary
from audit.services import (
    mark_raw_data_parse_failed,
    mark_raw_data_parsed,
    mark_raw_data_system_error,
    mark_raw_data_unsupported,
    record_raw_data_observation,
)
from earnings.calendar_parsing import (
    EarningsCalendarParser,
    EarningsCalendarParserContextError,
    EarningsCalendarParseResult,
    EarningsCalendarPayloadError,
    NormalizedEarningsCalendarRecord,
    UnsupportedEarningsCalendarIdentityError,
)
from earnings.models import EarningsCalendarObservation
from earnings.services.calendar import record_earnings_calendar_observation


class EarningsCalendarIngestionError(RuntimeError):
    """Base class for earnings calendar ingestion failures."""


class InvalidEarningsCalendarIngestion(EarningsCalendarIngestionError):
    """Raised before raw persistence when ingestion context is invalid."""


class EarningsCalendarIngestionIntegrityError(EarningsCalendarIngestionError):
    """Raised when a parser result violates the persistence contract."""


class EarningsCalendarParseFailure(EarningsCalendarIngestionError):
    """Raised after the raw payload and failed parse attempt are persisted."""

    def __init__(
        self,
        *,
        message: str,
        raw_data_record: RawDataRecord,
        raw_data_observation: RawDataObservation,
        parse_attempt: RawDataParseAttempt,
    ) -> None:
        super().__init__(message)
        self.raw_data_record = raw_data_record
        self.raw_data_observation = raw_data_observation
        self.parse_attempt = parse_attempt


class EarningsCalendarPayloadParseFailure(EarningsCalendarParseFailure):
    """Parser rejected malformed or inconsistent payload data."""


class EarningsCalendarUnsupportedIdentity(EarningsCalendarParseFailure):
    """Parser could not find a stable upstream provider event identity."""


class EarningsCalendarParserSystemFailure(EarningsCalendarParseFailure):
    """Parser context or unexpected system error occurred."""


@dataclass(frozen=True, slots=True)
class EarningsCalendarIngestionResult:
    """Persisted result of one raw-first earnings calendar payload."""

    sync_run: SyncRun
    raw_data_record: RawDataRecord
    raw_data_observation: RawDataObservation
    parse_attempt: RawDataParseAttempt
    provider_key: str
    provider_version: str
    parser_version: str
    observations: tuple[EarningsCalendarObservation, ...]
    raw_record_created: bool
    raw_observation_created: bool
    observations_created: int
    observations_reused: int


@dataclass(frozen=True, slots=True)
class _IngestionContext:
    sync_run: SyncRun
    provider_key: str
    provider_version: str
    parser_version: str


@dataclass(frozen=True, slots=True)
class _FailureWrite:
    raw_data_record: RawDataRecord
    parse_attempt: RawDataParseAttempt


def ingest_earnings_calendar_payload(
    *,
    sync_run: SyncRun,
    parser: EarningsCalendarParser,
    raw_content: bytes,
    provider_key: str,
    provider_version: str,
    source_url: str,
    fetched_at: datetime,
    observed_at: datetime | None = None,
    request_method: str = "GET",
    request_identity: Mapping[str, object] | None = None,
    http_status: int | None = 200,
    content_type: str = "application/json",
    encoding: str = "utf-8",
    request_descriptor: ProviderRequestContextDescriptor | None = None,
) -> EarningsCalendarIngestionResult:
    """Persist raw lineage, parse one payload, and materialize observations.

    ``sync_run`` is caller-owned and must already be running.  This service does
    not create SyncRuns, finish them, update their counts, or implement window
    scope/idempotency.
    """

    context = _validate_ingestion_context(
        sync_run=sync_run,
        parser=parser,
        raw_content=raw_content,
        provider_key=provider_key,
        provider_version=provider_version,
    )

    ingest_result = record_raw_data_observation(
        sync_run=context.sync_run,
        source_url=source_url,
        payload=raw_content,
        request_method=request_method,
        request_identity=request_identity,
        fetched_at=fetched_at,
        observed_at=observed_at,
        http_status=http_status,
        content_type=content_type,
        encoding=encoding,
        request_descriptor=request_descriptor,
    )
    raw_record = ingest_result.record
    raw_observation = ingest_result.observation

    try:
        parse_result = parser.parse(
            bytes(raw_record.payload),
            provider_key=context.provider_key,
            provider_version=context.provider_version,
        )
    except UnsupportedEarningsCalendarIdentityError as error:
        failure_write = _record_unsupported_identity(
            context=context,
            raw_record=raw_record,
            raw_observation=raw_observation,
            error=error,
        )
        raise EarningsCalendarUnsupportedIdentity(
            message=_safe_reason(error, default="Missing stable provider_event_id."),
            raw_data_record=failure_write.raw_data_record,
            raw_data_observation=raw_observation,
            parse_attempt=failure_write.parse_attempt,
        ) from None
    except EarningsCalendarParserContextError as error:
        message = _safe_reason(error, default="Earnings calendar parser context is invalid.")
        failure_write = _record_system_failure(
            context=context,
            raw_record=raw_record,
            raw_observation=raw_observation,
            error_summary=message,
        )
        raise EarningsCalendarParserSystemFailure(
            message=message,
            raw_data_record=failure_write.raw_data_record,
            raw_data_observation=raw_observation,
            parse_attempt=failure_write.parse_attempt,
        ) from None
    except EarningsCalendarPayloadError as error:
        message = _safe_reason(error, default="Earnings calendar payload is malformed.")
        failure_write = _record_payload_failure(
            context=context,
            raw_record=raw_record,
            raw_observation=raw_observation,
            error_summary=message,
        )
        raise EarningsCalendarPayloadParseFailure(
            message=message,
            raw_data_record=failure_write.raw_data_record,
            raw_data_observation=raw_observation,
            parse_attempt=failure_write.parse_attempt,
        ) from None
    except Exception as error:
        message = f"Unexpected earnings calendar parser error ({type(error).__name__})."
        failure_write = _record_system_failure(
            context=context,
            raw_record=raw_record,
            raw_observation=raw_observation,
            error_summary=message,
        )
        raise EarningsCalendarParserSystemFailure(
            message=message,
            raw_data_record=failure_write.raw_data_record,
            raw_data_observation=raw_observation,
            parse_attempt=failure_write.parse_attempt,
        ) from None

    try:
        _validate_parse_result(parse_result=parse_result, context=context)
    except EarningsCalendarIngestionIntegrityError as error:
        failure_write = _record_system_failure(
            context=context,
            raw_record=raw_record,
            raw_observation=raw_observation,
            error_summary=str(error),
        )
        raise EarningsCalendarParserSystemFailure(
            message=str(error),
            raw_data_record=failure_write.raw_data_record,
            raw_data_observation=raw_observation,
            parse_attempt=failure_write.parse_attempt,
        ) from None

    updated_record = mark_raw_data_parsed(
        raw_record.pk,
        parser_version=context.parser_version,
        observation=raw_observation,
    )
    parse_attempt = _load_parse_attempt(
        raw_observation=raw_observation,
        parser_version=context.parser_version,
    )
    observations, observations_created, observations_reused = _persist_observations(
        context=context,
        raw_record=raw_record,
        parse_result=parse_result,
    )
    return EarningsCalendarIngestionResult(
        sync_run=context.sync_run,
        raw_data_record=updated_record,
        raw_data_observation=raw_observation,
        parse_attempt=parse_attempt,
        provider_key=context.provider_key,
        provider_version=context.provider_version,
        parser_version=context.parser_version,
        observations=observations,
        raw_record_created=ingest_result.record_created,
        raw_observation_created=ingest_result.observation_created,
        observations_created=observations_created,
        observations_reused=observations_reused,
    )


def _validate_ingestion_context(
    *,
    sync_run: SyncRun,
    parser: EarningsCalendarParser,
    raw_content: bytes,
    provider_key: str,
    provider_version: str,
) -> _IngestionContext:
    if sync_run._state.adding or sync_run.pk is None:
        raise InvalidEarningsCalendarIngestion("sync_run must be saved before use.")
    try:
        current_run = SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvalidEarningsCalendarIngestion("sync_run no longer exists.") from error
    if current_run.status != SyncRun.Status.RUNNING:
        raise InvalidEarningsCalendarIngestion("sync_run must be running.")
    if current_run.source.source_type != DataSource.SourceType.EARNINGS_CALENDAR:
        raise InvalidEarningsCalendarIngestion(
            "sync_run source must use the earnings_calendar source type."
        )
    if not isinstance(raw_content, bytes):
        raise InvalidEarningsCalendarIngestion("raw_content must be bytes.")
    if not isinstance(parser, EarningsCalendarParser):
        raise InvalidEarningsCalendarIngestion("parser must implement EarningsCalendarParser.")

    normalized_provider_key = _require_text(
        provider_key,
        value_name="provider_key",
        maximum_length=64,
    )
    normalized_provider_version = _require_text(
        provider_version,
        value_name="provider_version",
        maximum_length=100,
    )
    if current_run.source.provider_adapter != normalized_provider_key:
        raise InvalidEarningsCalendarIngestion(
            "provider_key must match the sync_run source provider_adapter."
        )
    normalized_parser_version = _require_text(
        parser.parser_version,
        value_name="parser_version",
        maximum_length=100,
    )
    return _IngestionContext(
        sync_run=current_run,
        provider_key=normalized_provider_key,
        provider_version=normalized_provider_version,
        parser_version=normalized_parser_version,
    )


def _validate_parse_result(
    *,
    parse_result: EarningsCalendarParseResult,
    context: _IngestionContext,
) -> None:
    if not isinstance(parse_result, EarningsCalendarParseResult):
        raise EarningsCalendarIngestionIntegrityError(
            "Parser must return an EarningsCalendarParseResult."
        )
    if not isinstance(parse_result.records, tuple):
        raise EarningsCalendarIngestionIntegrityError("Parser result records must be a tuple.")
    if parse_result.provider_key != context.provider_key:
        raise EarningsCalendarIngestionIntegrityError(
            "Parser result provider_key does not match the ingestion context."
        )
    if parse_result.provider_version != context.provider_version:
        raise EarningsCalendarIngestionIntegrityError(
            "Parser result provider_version does not match the ingestion context."
        )
    if parse_result.parser_version != context.parser_version:
        raise EarningsCalendarIngestionIntegrityError(
            "Parser result parser_version does not match the parser identity."
        )

    seen_event_ids: set[str] = set()
    seen_positions: set[int] = set()
    for record in parse_result.records:
        if not isinstance(record, NormalizedEarningsCalendarRecord):
            raise EarningsCalendarIngestionIntegrityError(
                "Parser result contains an invalid normalized record."
            )
        if record.parser_version != context.parser_version:
            raise EarningsCalendarIngestionIntegrityError(
                "Normalized record parser_version does not match the parser identity."
            )
        if not isinstance(record.provider_event_id, str) or not record.provider_event_id.strip():
            raise EarningsCalendarIngestionIntegrityError(
                "Normalized record provider_event_id must not be blank."
            )
        if record.provider_event_id in seen_event_ids:
            raise EarningsCalendarIngestionIntegrityError(
                "Parser result contains duplicate provider_event_id values."
            )
        if (
            isinstance(record.raw_position, bool)
            or not isinstance(record.raw_position, int)
            or record.raw_position < 1
        ):
            raise EarningsCalendarIngestionIntegrityError(
                "Normalized record raw_position must be 1-based."
            )
        if record.raw_position in seen_positions:
            raise EarningsCalendarIngestionIntegrityError(
                "Parser result contains duplicate raw_position values."
            )
        seen_event_ids.add(record.provider_event_id)
        seen_positions.add(record.raw_position)


def _record_payload_failure(
    *,
    context: _IngestionContext,
    raw_record: RawDataRecord,
    raw_observation: RawDataObservation,
    error_summary: str,
) -> _FailureWrite:
    updated_record = mark_raw_data_parse_failed(
        raw_record.pk,
        parser_version=context.parser_version,
        parse_error=error_summary,
        observation=raw_observation,
    )
    return _FailureWrite(
        raw_data_record=updated_record,
        parse_attempt=_load_parse_attempt(
            raw_observation=raw_observation,
            parser_version=context.parser_version,
        ),
    )


def _record_unsupported_identity(
    *,
    context: _IngestionContext,
    raw_record: RawDataRecord,
    raw_observation: RawDataObservation,
    error: Exception,
) -> _FailureWrite:
    updated_record = mark_raw_data_unsupported(
        raw_record.pk,
        parser_version=context.parser_version,
        error_summary=_safe_reason(error, default="Missing stable provider_event_id."),
        observation=raw_observation,
    )
    return _FailureWrite(
        raw_data_record=updated_record,
        parse_attempt=_load_parse_attempt(
            raw_observation=raw_observation,
            parser_version=context.parser_version,
        ),
    )


def _record_system_failure(
    *,
    context: _IngestionContext,
    raw_record: RawDataRecord,
    raw_observation: RawDataObservation,
    error_summary: str,
) -> _FailureWrite:
    updated_record = mark_raw_data_system_error(
        raw_record.pk,
        parser_version=context.parser_version,
        parse_error=error_summary,
        observation=raw_observation,
    )
    return _FailureWrite(
        raw_data_record=updated_record,
        parse_attempt=_load_parse_attempt(
            raw_observation=raw_observation,
            parser_version=context.parser_version,
        ),
    )


def _load_parse_attempt(
    *,
    raw_observation: RawDataObservation,
    parser_version: str,
) -> RawDataParseAttempt:
    try:
        return RawDataParseAttempt.objects.get(
            observation=raw_observation,
            parser_version=parser_version,
        )
    except RawDataParseAttempt.DoesNotExist as error:
        raise EarningsCalendarIngestionIntegrityError(
            "Parse attempt was not persisted for the raw observation."
        ) from error


def _persist_observations(
    *,
    context: _IngestionContext,
    raw_record: RawDataRecord,
    parse_result: EarningsCalendarParseResult,
) -> tuple[tuple[EarningsCalendarObservation, ...], int, int]:
    observations: list[EarningsCalendarObservation] = []
    created_count = 0
    reused_count = 0
    for record in parse_result.records:
        write_result = record_earnings_calendar_observation(
            source=context.sync_run.source,
            raw_data_record=raw_record,
            provider_key=context.provider_key,
            provider_version=context.provider_version,
            parser_version=context.parser_version,
            provider_event_id=record.provider_event_id,
            raw_position=record.raw_position,
            cik=record.cik,
            ticker=record.ticker,
            exchange=record.exchange,
            provider_symbol=record.provider_symbol,
            company_name=record.company_name,
            fiscal_label_raw=record.fiscal_label_raw,
            fiscal_year=record.fiscal_year,
            period_end_date=record.period_end_date,
            period_type=record.period_type,
            fiscal_calendar_type=record.fiscal_calendar_type,
            period_length_weeks=record.period_length_weeks,
            estimated_release=_estimated_release(record),
            estimated_release_precision=record.estimated_release_precision,
            release_session=record.release_session,
            source_observed_at=record.source_observed_at,
            confidence=record.confidence,
        )
        observations.append(write_result.observation)
        if write_result.created:
            created_count += 1
        else:
            reused_count += 1
    return tuple(observations), created_count, reused_count


def _estimated_release(
    record: NormalizedEarningsCalendarRecord,
) -> date | datetime | None:
    if record.estimated_release_date is not None:
        return record.estimated_release_date
    return record.estimated_release_at


def _require_text(value: object, *, value_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsCalendarIngestion(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidEarningsCalendarIngestion(f"{value_name} must not be empty.")
    if len(normalized) > maximum_length:
        raise InvalidEarningsCalendarIngestion(
            f"{value_name} must contain at most {maximum_length} characters."
        )
    return normalized


def _safe_reason(error: Exception, *, default: str) -> str:
    return sanitize_error_summary(str(error)) or default
