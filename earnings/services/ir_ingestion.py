"""Raw-first Investor Relations ingestion foundation (Stage 4.5B).

The pipeline is always:

    ProviderResult -> RawDataRecord / RawDataObservation -> persisted bytes
      -> parser -> InvestorRelationsObservation

Provider/parser code never writes domain rows here; decisions and schedule /
lifecycle writes are a separate service.  Malformed items are isolated so one
bad item never blocks its siblings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

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
    record_raw_data_observation,
)
from companies.models import Company
from earnings.ir_parsing import (
    InvestorRelationsItemFailure,
    InvestorRelationsParser,
    InvestorRelationsParseResult,
    NormalizedInvestorRelationsRecord,
)
from earnings.models import InvestorRelationsObservation
from earnings.services.ir_observation import (
    InvalidInvestorRelationsObservation,
    InvestorRelationsObservationWriteResult,
    record_investor_relations_observation,
)

FAILURE_COMPANY_OUT_OF_SCOPE = "COMPANY_OUT_OF_SCOPE"
FAILURE_UNKNOWN_COMPANY = "UNKNOWN_COMPANY"


class InvestorRelationsIngestionError(RuntimeError):
    """Base class for IR ingestion failures."""


class InvalidInvestorRelationsIngestion(InvestorRelationsIngestionError):
    pass


class InvestorRelationsIngestionIntegrityError(InvestorRelationsIngestionError):
    pass


@dataclass(frozen=True, slots=True)
class InvestorRelationsIngestionResult:
    sync_run: SyncRun
    raw_data_record: RawDataRecord
    raw_data_observation: RawDataObservation
    parse_attempt: RawDataParseAttempt
    provider_key: str
    provider_version: str
    parser_version: str
    source_key: str
    observations: tuple[InvestorRelationsObservation, ...]
    item_failures: tuple[InvestorRelationsItemFailure, ...]
    raw_record_created: bool
    raw_observation_created: bool
    observations_created: int
    observations_reused: int


def ingest_investor_relations_payload(
    *,
    sync_run: SyncRun,
    parser: InvestorRelationsParser,
    raw_content: bytes,
    source_key: str | None = None,
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
) -> InvestorRelationsIngestionResult:
    """Persist raw lineage first, then parse persisted bytes into observations."""

    _validate_ingestion_context(
        sync_run=sync_run,
        parser=parser,
        raw_content=raw_content,
        provider_key=provider_key,
        provider_version=provider_version,
    )
    ingest_result = record_raw_data_observation(
        sync_run=sync_run,
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
    parse_attempt, parsed_source_key, observations, failures, created_count, reused_count = (
        process_persisted_investor_relations_payload(
            sync_run=sync_run,
            parser=parser,
            raw_record=ingest_result.record,
            raw_observation=ingest_result.observation,
            source_key=source_key,
            provider_key=provider_key,
            provider_version=provider_version,
        )
    )
    return InvestorRelationsIngestionResult(
        sync_run=sync_run,
        raw_data_record=ingest_result.record,
        raw_data_observation=ingest_result.observation,
        parse_attempt=parse_attempt,
        provider_key=provider_key.strip(),
        provider_version=provider_version.strip(),
        parser_version=parser.parser_version.strip(),
        source_key=parsed_source_key,
        observations=observations,
        item_failures=failures,
        raw_record_created=ingest_result.record_created,
        raw_observation_created=ingest_result.observation_created,
        observations_created=created_count,
        observations_reused=reused_count,
    )


def process_persisted_investor_relations_payload(
    *,
    sync_run: SyncRun,
    parser: InvestorRelationsParser,
    raw_record: RawDataRecord,
    raw_observation: RawDataObservation,
    source_key: str | None,
    provider_key: str,
    provider_version: str,
) -> tuple[
    RawDataParseAttempt,
    str,
    tuple[InvestorRelationsObservation, ...],
    tuple[InvestorRelationsItemFailure, ...],
    int,
    int,
]:
    """Parse one persisted raw record and persist its normalized observations."""

    try:
        parse_result = parser.parse(
            bytes(raw_record.payload),
            provider_key=provider_key,
            provider_version=provider_version,
        )
    except Exception as error:
        message = (
            sanitize_error_summary(f"IR payload parse failed ({type(error).__name__}).")
            or "IR payload parse failed."
        )
        record = mark_raw_data_parse_failed(
            raw_record.pk,
            parser_version=parser.parser_version,
            parse_error=message,
            observation=raw_observation,
        )
        attempt = _load_parse_attempt(
            observation=raw_observation,
            parser_version=parser.parser_version,
        )
        raise InvestorRelationsIngestionIntegrityError(
            f"IR payload {record.pk} could not be parsed: {message}"
        ) from None

    validate_investor_relations_parse_result(
        parse_result=parse_result,
        provider_key=provider_key.strip(),
        provider_version=provider_version.strip(),
        parser_version=parser.parser_version.strip(),
    )
    if source_key is not None and parse_result.source_key != source_key.strip():
        raise InvestorRelationsIngestionIntegrityError(
            "Parser result source_key does not match the frozen scope source_key."
        )

    observations: list[InvestorRelationsObservation] = []
    failures: list[InvestorRelationsItemFailure] = list(parse_result.failures)
    created_count = 0
    reused_count = 0
    scope_company_ids = _scope_company_ids(sync_run)
    companies = Company.objects.in_bulk(
        [normalized.company_id for normalized in parse_result.records]
    )
    for normalized in parse_result.records:
        if str(normalized.company_id) not in scope_company_ids:
            failures.append(
                InvestorRelationsItemFailure(
                    raw_position=normalized.raw_position,
                    reason_code=FAILURE_COMPANY_OUT_OF_SCOPE,
                    message="company_id is not part of the frozen run scope.",
                )
            )
            continue
        company = companies.get(normalized.company_id)
        if company is None:
            failures.append(
                InvestorRelationsItemFailure(
                    raw_position=normalized.raw_position,
                    reason_code=FAILURE_UNKNOWN_COMPANY,
                    message="company_id does not exist.",
                )
            )
            continue
        try:
            write_result = _persist_record(
                sync_run=sync_run,
                raw_record=raw_record,
                company=company,
                provider_key=provider_key.strip(),
                provider_version=provider_version.strip(),
                record=normalized,
            )
        except (InvalidInvestorRelationsObservation, Company.DoesNotExist) as error:
            failures.append(
                InvestorRelationsItemFailure(
                    raw_position=normalized.raw_position,
                    reason_code="OBSERVATION_REJECTED",
                    message=sanitize_error_summary(str(error)) or "observation rejected",
                )
            )
            continue
        observations.append(write_result.observation)
        if write_result.created:
            created_count += 1
        else:
            reused_count += 1

    if failures:
        summary = _failure_summary(failures)
        mark_raw_data_parse_failed(
            raw_record.pk,
            parser_version=parser.parser_version,
            parse_error=summary,
            observation=raw_observation,
        )
    else:
        mark_raw_data_parsed(
            raw_record.pk,
            parser_version=parser.parser_version,
            observation=raw_observation,
        )
    attempt = _load_parse_attempt(
        observation=raw_observation,
        parser_version=parser.parser_version,
    )
    return (
        attempt,
        parse_result.source_key,
        tuple(observations),
        tuple(failures),
        created_count,
        reused_count,
    )


def validate_investor_relations_parse_result(
    *,
    parse_result: InvestorRelationsParseResult,
    provider_key: str,
    provider_version: str,
    parser_version: str,
) -> None:
    if not isinstance(parse_result, InvestorRelationsParseResult):
        raise InvestorRelationsIngestionIntegrityError(
            "Parser must return an InvestorRelationsParseResult."
        )
    if not isinstance(parse_result.records, tuple):
        raise InvestorRelationsIngestionIntegrityError("Parser result records must be a tuple.")
    if parse_result.provider_key != provider_key:
        raise InvestorRelationsIngestionIntegrityError(
            "Parser result provider_key does not match the ingestion context."
        )
    if parse_result.provider_version != provider_version:
        raise InvestorRelationsIngestionIntegrityError(
            "Parser result provider_version does not match the ingestion context."
        )
    if parse_result.parser_version != parser_version:
        raise InvestorRelationsIngestionIntegrityError(
            "Parser result parser_version does not match the parser identity."
        )
    for record in parse_result.records:
        if not isinstance(record, NormalizedInvestorRelationsRecord):
            raise InvestorRelationsIngestionIntegrityError(
                "Parser result contains an invalid normalized record."
            )
        if record.parser_version != parser_version:
            raise InvestorRelationsIngestionIntegrityError(
                "Normalized record parser_version does not match the parser identity."
            )
        if record.raw_position < 1:
            raise InvestorRelationsIngestionIntegrityError(
                "Normalized record raw_position must be 1-based."
            )


def _persist_record(
    *,
    sync_run: SyncRun,
    raw_record: RawDataRecord,
    company: Company,
    provider_key: str,
    provider_version: str,
    record: NormalizedInvestorRelationsRecord,
) -> InvestorRelationsObservationWriteResult:
    return record_investor_relations_observation(
        source=sync_run.source,
        raw_data_record=raw_record,
        company=company,
        provider_key=provider_key,
        provider_version=provider_version,
        parser_version=record.parser_version,
        source_event_identity=record.source_event_identity,
        raw_position=record.raw_position,
        period_end_date=record.period_end_date,
        period_type=record.period_type,
        item_type=record.item_type,
        estimated_release=record.estimated_release.as_schedule_change_value(),
        confirmed_release=record.confirmed_release.as_schedule_change_value(),
        earnings_release=record.earnings_release.as_schedule_change_value(),
        conference_call=record.conference_call.as_schedule_change_value(),
        release_session=record.release_session,
        cancellation=record.cancellation,
        source_observed_at=record.source_observed_at,
        confidence=record.confidence,
    )


def _validate_ingestion_context(
    *,
    sync_run: SyncRun,
    parser: InvestorRelationsParser,
    raw_content: bytes,
    provider_key: str,
    provider_version: str,
) -> None:
    if sync_run._state.adding or sync_run.pk is None:
        raise InvalidInvestorRelationsIngestion("sync_run must be saved before use.")
    try:
        current_run = SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvalidInvestorRelationsIngestion("sync_run no longer exists.") from error
    if current_run.status != SyncRun.Status.RUNNING:
        raise InvalidInvestorRelationsIngestion("sync_run must be running.")
    if current_run.source.source_type != DataSource.SourceType.INVESTOR_RELATIONS:
        raise InvalidInvestorRelationsIngestion(
            "sync_run source must use the investor_relations source type."
        )
    if not isinstance(raw_content, bytes):
        raise InvalidInvestorRelationsIngestion("raw_content must be bytes.")
    if not isinstance(parser, InvestorRelationsParser):
        raise InvalidInvestorRelationsIngestion("parser must implement InvestorRelationsParser.")
    normalized_provider_key = _require_text(provider_key, "provider_key", 64)
    normalized_provider_version = _require_text(provider_version, "provider_version", 100)
    if current_run.source.provider_adapter != normalized_provider_key:
        raise InvalidInvestorRelationsIngestion(
            "provider_key must match the sync_run source provider_adapter."
        )
    if current_run.provider_version != normalized_provider_version:
        raise InvalidInvestorRelationsIngestion(
            "provider_version must match the sync_run persisted provider context."
        )


def _scope_company_ids(sync_run: SyncRun) -> set[str]:
    scope = sync_run.scope if isinstance(sync_run.scope, dict) else {}
    raw_ids = scope.get("company_ids")
    if not isinstance(raw_ids, list):
        raise InvestorRelationsIngestionIntegrityError(
            "IR SyncRun scope must contain a frozen company_ids list."
        )
    return {str(value) for value in raw_ids}


def _failure_summary(failures: list[InvestorRelationsItemFailure]) -> str:
    codes = sorted({failure.reason_code for failure in failures})
    return (
        sanitize_error_summary(f"{len(failures)} IR item(s) failed: {', '.join(codes)}")
        or "IR items failed."
    )


def _load_parse_attempt(
    *,
    observation: RawDataObservation,
    parser_version: str,
) -> RawDataParseAttempt:
    try:
        return RawDataParseAttempt.objects.get(
            observation=observation,
            parser_version=parser_version,
        )
    except RawDataParseAttempt.DoesNotExist as error:
        raise InvestorRelationsIngestionIntegrityError(
            "Parse attempt was not persisted for the raw observation."
        ) from error


def _require_text(value: object, value_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsIngestion(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidInvestorRelationsIngestion(f"{value_name} must not be empty.")
    if len(normalized) > maximum_length:
        raise InvalidInvestorRelationsIngestion(
            f"{value_name} must contain at most {maximum_length} characters."
        )
    return normalized
