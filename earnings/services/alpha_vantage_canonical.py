"""Stage 4.2F-B Alpha Vantage v2 candidate-only canonical sync.

The service implements the frozen ADR-020 contract without adding schema or
migrations.  It keeps Alpha Vantage observations and candidates incomplete
(period_type NULL) and never calls canonical promotion.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import cast
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from audit.models import (
    AuditRecord,
    DataSource,
    RawDataObservation,
    RawDataRecord,
    SourceEvidence,
    SyncRun,
)
from audit.services import (
    RawDataIngestResult,
    mark_raw_data_parse_failed,
    mark_raw_data_parsed,
    mark_raw_data_system_error,
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    record_raw_data_observation,
    record_source_evidence,
    record_system_action,
    start_sync_run_with_result,
    update_sync_run_counts,
)
from companies.models import Company, SecurityListing
from companies.services import CompanyServiceError, normalize_ticker
from earnings.alpha_vantage_canonical_parser import (
    ALPHA_VANTAGE_CANONICAL_PARSER_VERSION,
    AlphaVantageCanonicalParseResult,
    AlphaVantageCanonicalParseStatus,
    AlphaVantageCanonicalRow,
    parse_alpha_vantage_canonical_calendar,
)
from earnings.models import (
    EarningsCalendarObservation,
    EarningsDatePrecision,
    EarningsEvent,
    EarningsReconciliationDecision,
    EventStatus,
    FiscalCalendarType,
    IdentityStatus,
    ReleaseSession,
)
from earnings.services.calendar import record_earnings_calendar_observation
from earnings.services.calendar_pagination import EARNINGS_CALENDAR_WINDOW_JOB_TYPE
from earnings.services.calendar_run_ownership import calendar_run_ownership
from earnings.services.calendar_sync_identity import (
    EarningsCalendarWindowKind,
    build_earnings_calendar_sync_scope,
)
from earnings.services.date_changes import update_earnings_schedule
from earnings.services.monitoring_pool import (
    MonitoringPoolSnapshotReference,
    resolve_monitoring_pool_snapshot,
    resolve_monitoring_pool_snapshot_contract,
)
from earnings.services.reconciliation import record_earnings_reconciliation_decision
from providers.alpha_vantage_canonical import (
    ALPHA_VANTAGE_CANONICAL_PROVIDER_VERSION,
    AlphaVantageCanonicalProvider,
)
from providers.alpha_vantage_reference import ALPHA_VANTAGE_REFERENCE_URL
from providers.exceptions import ProviderError
from providers.types import ProviderCapability, ProviderRequest

EARNINGS_SOURCE_EVENT_IDENTITY_VERSION_V2 = "earnings-source-event-identity-v2"
EARNINGS_INCOMPLETE_CANDIDATE_IDENTITY_VERSION_V2 = "earnings-incomplete-candidate-identity-v2"
ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2 = "alpha-vantage-company-match-v2"
ALPHA_VANTAGE_WINDOW_CAPABILITY_VERSION = "alpha-vantage-window-capability-v1"
ALPHA_VANTAGE_FORWARD_CAPABILITY = "nominal_3month"
ALPHA_VANTAGE_PAST_CORRECTION_CAPABILITY = "unsupported"

ALPHA_VANTAGE_SOURCE_IDENTITY_PREFIX = "internal:v2:"

_AV_V2_NAMESPACE = "av_v2"
_SCHEDULED_IDEMPOTENCY_PREFIX = "earnings-calendar-av-v2-scheduled:v1:"
_REQUEST_IDEMPOTENCY_PREFIX = "earnings-calendar-av-v2-request:v1:"
_CANDIDATE_UUID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://earnings-radar.example/alpha-vantage-canonical/v2",
)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class AlphaVantageCanonicalSyncError(RuntimeError):
    """The canonical Alpha Vantage run cannot safely start or complete."""


class AlphaVantageCanonicalIntegrityError(RuntimeError):
    """Persisted canonical Alpha Vantage facts violate the ADR-020 contract."""


class AlphaVantageCanonicalReplayIntegrityError(RuntimeError):
    """A v2 replay could not reproduce the persisted source identities."""


class CompanyResolutionOutcome(StrEnum):
    RESOLVED = "RESOLVED"
    UNMATCHED = "UNMATCHED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class CompanyResolution:
    outcome: CompanyResolutionOutcome
    provider_symbol: str
    company: Company | None
    listings: tuple[SecurityListing, ...]
    exact_company_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class AlphaVantageCandidateWrite:
    candidate: EarningsEvent | None
    decision: EarningsReconciliationDecision | None
    candidate_created: bool
    decision_created: bool
    schedule_changed: bool
    collision: bool


@dataclass(frozen=True, slots=True)
class AlphaVantageCanonicalSyncResult:
    sync_run: SyncRun
    created: bool
    parse_status: str | None
    resolved_row_count: int
    candidate_created_count: int
    candidate_reused_count: int
    skipped_count: int
    failed_count: int


@dataclass(frozen=True, slots=True)
class AlphaVantageCanonicalReplayResult:
    sync_run: SyncRun
    raw_record_count: int
    resolved_row_count: int
    unresolved_row_count: int
    ambiguous_row_count: int
    identity_digest: str


def nominal_window_end(run_date: date) -> date:
    """Return the deterministic nominal end of the provider's 3-month horizon."""

    if isinstance(run_date, datetime) or not isinstance(run_date, date):
        raise ValueError("run_date must be a date.")
    year = run_date.year + (run_date.month - 1 + 3) // 12
    month = (run_date.month - 1 + 3) % 12 + 1
    day = min(run_date.day, calendar.monthrange(year, month)[1])
    return date(year, month, day) - timedelta(days=1)


def build_alpha_vantage_canonical_scope(
    *,
    provider_key: str,
    run_date: date,
    monitoring_pool_as_of: date,
    monitoring_pool_hash: str,
    selector_version: str,
    window_kind: EarningsCalendarWindowKind | str,
) -> dict[str, object]:
    """Build the canonical 8-field scope with the nominal 3-month envelope."""

    return build_earnings_calendar_sync_scope(
        provider_key=provider_key,
        window_kind=window_kind,
        window_start=run_date,
        window_end=nominal_window_end(run_date),
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
    )


def derive_alpha_vantage_source_identity_v2(
    *,
    source_key: str,
    provider_key: str,
    company_id: UUID,
    period_end_date: date,
) -> str:
    """Derive the versioned Company-scoped Layer 2 identity."""

    payload = {
        "source_event_identity_version": EARNINGS_SOURCE_EVENT_IDENTITY_VERSION_V2,
        "source_key": source_key,
        "provider_key": provider_key,
        "company_id": str(company_id),
        "period_end_date": period_end_date.isoformat(),
    }
    return f"{ALPHA_VANTAGE_SOURCE_IDENTITY_PREFIX}{_sha256_json(payload)}"


def derive_alpha_vantage_resolution_key(
    *,
    source_key: str,
    provider_key: str,
    provider_symbol: str,
    period_end_date: date,
) -> str:
    """Derive the provider-scoped key used for cross-Company collision checks."""

    return _sha256_json(
        {
            "resolution_rule_version": "alpha-vantage-resolution-key-v1",
            "source_key": source_key,
            "provider_key": provider_key,
            "normalized_provider_symbol": _normalize_provider_symbol(provider_symbol),
            "period_end_date": period_end_date.isoformat(),
        }
    )


def derive_alpha_vantage_candidate_family_key(
    *,
    source_identity: str,
    source_key: str,
    provider_key: str,
    company_id: UUID,
    period_end_date: date,
) -> str:
    """Derive the stable incomplete-candidate family key."""

    return _sha256_json(
        {
            "candidate_identity_version": EARNINGS_INCOMPLETE_CANDIDATE_IDENTITY_VERSION_V2,
            "source_event_identity": source_identity,
            "source_key": source_key,
            "provider_key": provider_key,
            "company_id": str(company_id),
            "period_end_date": period_end_date.isoformat(),
            "matcher_version": ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2,
        }
    )


def derive_alpha_vantage_candidate_uuid(family_key: str) -> UUID:
    if not isinstance(family_key, str) or not _SHA256_HEX_RE.fullmatch(family_key):
        raise AlphaVantageCanonicalIntegrityError("Candidate family key must be SHA-256.")
    return uuid.uuid5(_CANDIDATE_UUID_NAMESPACE, family_key)


def resolve_alpha_vantage_provider_symbol(
    *,
    snapshot_reference: MonitoringPoolSnapshotReference,
    provider_symbol: str,
) -> CompanyResolution:
    """Resolve an exact AV symbol through the persisted frozen snapshot basis."""

    normalized_symbol = _normalize_provider_symbol(provider_symbol)
    basis_company_by_listing = _basis_company_by_listing(snapshot_reference)
    listings = tuple(
        SecurityListing.objects.select_related("company")
        .filter(
            id__in=basis_company_by_listing,
            ticker=normalized_symbol,
            effective_from__lte=snapshot_reference.snapshot.as_of_date,
        )
        .filter(
            Q(effective_to__isnull=True)
            | Q(effective_to__gt=snapshot_reference.snapshot.as_of_date)
        )
        .order_by("company_id", "id")
    )
    for listing in listings:
        if basis_company_by_listing.get(listing.pk) != listing.company_id:
            raise AlphaVantageCanonicalIntegrityError(
                "Frozen monitoring pool basis does not match its listing Company."
            )
    company_ids = tuple(sorted({listing.company_id for listing in listings}, key=str))
    if not company_ids:
        return CompanyResolution(
            outcome=CompanyResolutionOutcome.UNMATCHED,
            provider_symbol=normalized_symbol,
            company=None,
            listings=(),
            exact_company_ids=(),
        )
    if len(company_ids) > 1:
        return CompanyResolution(
            outcome=CompanyResolutionOutcome.AMBIGUOUS,
            provider_symbol=normalized_symbol,
            company=None,
            listings=listings,
            exact_company_ids=company_ids,
        )
    try:
        company = Company.objects.get(pk=company_ids[0])
    except Company.DoesNotExist as error:
        raise AlphaVantageCanonicalIntegrityError(
            "Frozen monitoring pool references a missing Company."
        ) from error
    return CompanyResolution(
        outcome=CompanyResolutionOutcome.RESOLVED,
        provider_symbol=normalized_symbol,
        company=company,
        listings=listings,
        exact_company_ids=company_ids,
    )


def execute_alpha_vantage_canonical_sync(
    *,
    source: DataSource,
    run_date: date,
    monitoring_pool_as_of: date,
    monitoring_pool_hash: str,
    selector_version: str,
    provider: AlphaVantageCanonicalProvider | None = None,
    request_id: str | None = None,
) -> AlphaVantageCanonicalSyncResult:
    """Execute one raw-first Alpha Vantage canonical candidate-only sync."""

    if not transaction.get_autocommit():
        raise AlphaVantageCanonicalSyncError(
            "Canonical Alpha Vantage synchronization requires autocommit."
        )
    for value, value_name in (
        (run_date, "run_date"),
        (monitoring_pool_as_of, "monitoring_pool_as_of"),
    ):
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ValueError(f"{value_name} must be a date.")
    if request_id is not None and (not request_id.strip() or len(request_id) > 100):
        raise ValueError("request_id must contain 1 to 100 characters.")
    if source.pk is None or source._state.adding:
        raise AlphaVantageCanonicalSyncError("DataSource must be persisted.")

    active_provider = provider or AlphaVantageCanonicalProvider.from_environment()
    if not isinstance(active_provider, AlphaVantageCanonicalProvider):
        raise AlphaVantageCanonicalSyncError(
            "Only the approved Alpha Vantage canonical provider is allowed."
        )
    if active_provider.provider_version != ALPHA_VANTAGE_CANONICAL_PROVIDER_VERSION:
        raise AlphaVantageCanonicalSyncError(
            "Alpha Vantage canonical provider version is not supported."
        )

    current_source = DataSource.objects.get(pk=source.pk)
    if (
        current_source.source_type != DataSource.SourceType.EARNINGS_CALENDAR
        or not current_source.is_enabled
        or current_source.provider_adapter != active_provider.provider_key
        or current_source.base_url.rstrip("/") != "https://www.alphavantage.co"
    ):
        raise AlphaVantageCanonicalSyncError(
            "DataSource is not an enabled Alpha Vantage earnings calendar."
        )

    snapshot_reference = resolve_monitoring_pool_snapshot_contract(
        as_of=monitoring_pool_as_of,
        selector_version=selector_version,
        pool_hash=monitoring_pool_hash,
    )
    window_kind = (
        EarningsCalendarWindowKind.MANUAL
        if request_id is not None
        else EarningsCalendarWindowKind.SCHEDULED
    )
    scope = build_alpha_vantage_canonical_scope(
        provider_key=active_provider.provider_key,
        run_date=run_date,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
        window_kind=window_kind,
    )
    idempotency_key = _build_idempotency_key(
        source=current_source,
        scope=scope,
        provider_version=active_provider.provider_version,
        parser_version=ALPHA_VANTAGE_CANONICAL_PARSER_VERSION,
        request_id=request_id,
    )

    with calendar_run_ownership(
        source_id=current_source.pk,
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    ):
        _retire_stale_canonical_runs(source_id=current_source.pk)
        started = start_sync_run_with_result(
            job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
            source=current_source,
            scope=scope,
            idempotency_key=idempotency_key,
            parser_version=ALPHA_VANTAGE_CANONICAL_PARSER_VERSION,
            provider_version=active_provider.provider_version,
            require_provider_version=True,
        )
        if not started.created:
            existing = started.sync_run
            if existing.status == SyncRun.Status.SUCCEEDED:
                return _result_from_existing(existing)
            raise AlphaVantageCanonicalSyncError(
                "Existing canonical Alpha Vantage run did not succeed; "
                "use a new request_id for retry."
            )

        run = started.sync_run
        request = ProviderRequest(
            capability=ProviderCapability.EARNINGS_CALENDAR,
            scope=scope,
            request_started_at=timezone.now(),
            source_url=ALPHA_VANTAGE_REFERENCE_URL,
            request_identity={"horizon": "3month"},
        )
        try:
            descriptor = active_provider.describe_request(request)
            result = active_provider.fetch(request)
            raw = record_raw_data_observation(
                sync_run=run,
                source_url=result.source_url,
                payload=result.raw_content,
                request_method=result.request_method,
                request_identity=result.request_identity,
                fetched_at=result.fetched_at,
                http_status=result.http_status,
                content_type=result.content_type,
                request_descriptor=descriptor,
            )
            update_sync_run_counts(run.pk, fetched_delta=1)
            parsed = _parse_payload(raw=raw, parser_version=ALPHA_VANTAGE_CANONICAL_PARSER_VERSION)
            if parsed.status == AlphaVantageCanonicalParseStatus.PAGE_ERROR:
                mark_raw_data_parse_failed(
                    raw.record.pk,
                    parser_version=ALPHA_VANTAGE_CANONICAL_PARSER_VERSION,
                    parse_error=f"Alpha Vantage canonical CSV {parsed.error_code}.",
                    observation=raw.observation,
                )
                update_sync_run_counts(run.pk, failed_delta=1)
                failed = mark_sync_run_failed(
                    run.pk,
                    error_summary="Alpha Vantage canonical CSV is invalid.",
                )
                return AlphaVantageCanonicalSyncResult(
                    sync_run=failed,
                    created=True,
                    parse_status=parsed.status.value,
                    resolved_row_count=0,
                    candidate_created_count=0,
                    candidate_reused_count=0,
                    skipped_count=0,
                    failed_count=1,
                )
            mark_raw_data_parsed(
                raw.record.pk,
                parser_version=ALPHA_VANTAGE_CANONICAL_PARSER_VERSION,
                observation=raw.observation,
            )
            return _process_parsed_rows(
                run=run,
                source=current_source,
                raw_record=raw.record,
                snapshot_reference=snapshot_reference,
                parsed=parsed,
                provider=active_provider,
            )
        except ProviderError:
            mark_sync_run_failed(
                run.pk,
                error_summary="Alpha Vantage canonical provider fetch failed.",
            )
            raise
        except Exception:
            if SyncRun.objects.get(pk=run.pk).status == SyncRun.Status.RUNNING:
                mark_sync_run_failed(
                    run.pk,
                    error_summary="Alpha Vantage canonical synchronization failed.",
                )
            raise


def verify_alpha_vantage_canonical_replay(
    *,
    sync_run: SyncRun,
) -> AlphaVantageCanonicalReplayResult:
    """Read-only replay verification over persisted raw and frozen snapshot."""

    if not isinstance(sync_run, SyncRun) or sync_run._state.adding or sync_run.pk is None:
        raise AlphaVantageCanonicalReplayIntegrityError("sync_run must be persisted.")
    current = SyncRun.objects.get(pk=sync_run.pk)
    if current.job_type != EARNINGS_CALENDAR_WINDOW_JOB_TYPE:
        raise AlphaVantageCanonicalReplayIntegrityError(
            "Replay verification requires the canonical earnings-calendar job type."
        )
    if current.provider_version != ALPHA_VANTAGE_CANONICAL_PROVIDER_VERSION:
        raise AlphaVantageCanonicalReplayIntegrityError(
            "Replay verification requires the canonical Alpha Vantage provider version."
        )
    if current.status == SyncRun.Status.RUNNING:
        raise AlphaVantageCanonicalReplayIntegrityError(
            "Replay verification requires a terminal source run."
        )
    if current.status == SyncRun.Status.FAILED:
        raise AlphaVantageCanonicalReplayIntegrityError(
            "Failed source runs cannot be replay-verified."
        )

    snapshot_reference = resolve_monitoring_pool_snapshot(current)
    raw_observations = tuple(
        RawDataObservation.objects.filter(sync_run=current)
        .select_related("raw_data_record")
        .order_by("observed_at", "pk")
    )
    resolved_count = 0
    unresolved_count = 0
    ambiguous_count = 0
    identities: set[str] = set()
    for raw_observation in raw_observations:
        raw_record = raw_observation.raw_data_record
        parsed = parse_alpha_vantage_canonical_calendar(bytes(raw_record.payload))
        if parsed.status == AlphaVantageCanonicalParseStatus.PAGE_ERROR:
            raise AlphaVantageCanonicalReplayIntegrityError(
                "Persisted canonical Alpha Vantage payload is no longer parseable."
            )
        expected_identities: set[str] = set()
        for row in parsed.rows:
            resolution = resolve_alpha_vantage_provider_symbol(
                snapshot_reference=snapshot_reference,
                provider_symbol=row.provider_symbol,
            )
            if resolution.outcome is CompanyResolutionOutcome.RESOLVED:
                assert resolution.company is not None
                expected_identities.add(
                    derive_alpha_vantage_source_identity_v2(
                        source_key=current.source.key,
                        provider_key=current.source.provider_adapter,
                        company_id=resolution.company.pk,
                        period_end_date=row.period_end_date,
                    )
                )
                resolved_count += 1
            elif resolution.outcome is CompanyResolutionOutcome.UNMATCHED:
                unresolved_count += 1
            else:
                ambiguous_count += 1
        persisted_observations = tuple(
            EarningsCalendarObservation.objects.filter(
                raw_data_record=raw_record,
                provider_key=current.source.provider_adapter,
                provider_version=ALPHA_VANTAGE_CANONICAL_PROVIDER_VERSION,
                parser_version=ALPHA_VANTAGE_CANONICAL_PARSER_VERSION,
            )
        )
        if any(observation.period_type is not None for observation in persisted_observations):
            raise AlphaVantageCanonicalReplayIntegrityError(
                "Persisted v2 observations must keep period_type NULL."
            )
        persisted_identities = {
            observation.provider_event_id for observation in persisted_observations
        }
        if persisted_identities != expected_identities:
            raise AlphaVantageCanonicalReplayIntegrityError(
                "Replay could not reproduce the persisted v2 source identities."
            )
        identities.update(expected_identities)

    digest_payload = {
        "identities": sorted(identities),
        "raw_record_count": len(raw_observations),
        "resolved_row_count": resolved_count,
        "unresolved_row_count": unresolved_count,
        "ambiguous_row_count": ambiguous_count,
    }
    return AlphaVantageCanonicalReplayResult(
        sync_run=current,
        raw_record_count=len(raw_observations),
        resolved_row_count=resolved_count,
        unresolved_row_count=unresolved_count,
        ambiguous_row_count=ambiguous_count,
        identity_digest=_sha256_json(digest_payload),
    )


def _process_parsed_rows(
    *,
    run: SyncRun,
    source: DataSource,
    raw_record: RawDataRecord,
    snapshot_reference: MonitoringPoolSnapshotReference,
    parsed: AlphaVantageCanonicalParseResult,
    provider: AlphaVantageCanonicalProvider,
) -> AlphaVantageCanonicalSyncResult:
    resolved_groups: dict[str, list[tuple[AlphaVantageCanonicalRow, CompanyResolution]]] = {}
    skipped = 0
    failed = parsed.invalid_row_count
    for row in parsed.rows:
        resolution = resolve_alpha_vantage_provider_symbol(
            snapshot_reference=snapshot_reference,
            provider_symbol=row.provider_symbol,
        )
        if resolution.outcome is CompanyResolutionOutcome.UNMATCHED:
            skipped += 1
            continue
        if resolution.outcome is CompanyResolutionOutcome.AMBIGUOUS:
            failed += 1
            continue
        assert resolution.company is not None
        identity = derive_alpha_vantage_source_identity_v2(
            source_key=source.key,
            provider_key=provider.provider_key,
            company_id=resolution.company.pk,
            period_end_date=row.period_end_date,
        )
        resolved_groups.setdefault(identity, []).append((row, resolution))

    resolved_rows = 0
    candidate_created = 0
    candidate_reused = 0
    for identity, entries in sorted(resolved_groups.items()):
        schedule_facts = {(entry[0].report_date, entry[0].release_session) for entry in entries}
        if len(entries) > 1 and len(schedule_facts) > 1:
            failed += len(entries)
            continue
        row, resolution = min(entries, key=lambda entry: entry[0].raw_position)
        if len(entries) > 1:
            skipped += len(entries) - 1
        resolved_rows += 1
        with transaction.atomic():
            observation = record_earnings_calendar_observation(
                source=source,
                raw_data_record=raw_record,
                provider_key=provider.provider_key,
                provider_version=provider.provider_version,
                parser_version=ALPHA_VANTAGE_CANONICAL_PARSER_VERSION,
                provider_event_id=identity,
                raw_position=row.raw_position,
                provider_symbol=row.provider_symbol,
                company_name=row.company_name,
                period_end_date=row.period_end_date,
                period_type=None,
                fiscal_year=None,
                fiscal_calendar_type=None,
                period_length_weeks=None,
                estimated_release=row.report_date,
                estimated_release_precision=EarningsDatePrecision.DATE_ONLY,
                release_session=row.release_session,
                source_observed_at=raw_record.fetched_at,
                confidence=None,
            ).observation
            write = _create_or_reuse_candidate(
                run=run,
                source=source,
                observation=observation,
                snapshot_reference=snapshot_reference,
                resolution=resolution,
                row=row,
            )
        if write.collision:
            failed += 1
        elif write.candidate_created:
            candidate_created += 1
        elif write.decision_created or write.schedule_changed:
            candidate_reused += 1
        else:
            skipped += 1

    update_sync_run_counts(
        run.pk,
        created_delta=candidate_created,
        updated_delta=candidate_reused,
        skipped_delta=skipped,
        failed_delta=failed,
    )
    if failed:
        finished = mark_sync_run_partial(
            run.pk,
            error_summary=(
                "Alpha Vantage canonical run contains unresolved, ambiguous, or invalid rows."
            ),
        )
    else:
        finished = mark_sync_run_succeeded(run.pk)
    return AlphaVantageCanonicalSyncResult(
        sync_run=finished,
        created=True,
        parse_status=parsed.status.value,
        resolved_row_count=resolved_rows,
        candidate_created_count=candidate_created,
        candidate_reused_count=candidate_reused,
        skipped_count=skipped,
        failed_count=failed,
    )


def _create_or_reuse_candidate(
    *,
    run: SyncRun,
    source: DataSource,
    observation: EarningsCalendarObservation,
    snapshot_reference: MonitoringPoolSnapshotReference,
    resolution: CompanyResolution,
    row: AlphaVantageCanonicalRow,
) -> AlphaVantageCandidateWrite:
    assert resolution.company is not None
    company = resolution.company
    source_identity = observation.provider_event_id
    period_end_date = observation.period_end_date
    if period_end_date is None:
        raise AlphaVantageCanonicalIntegrityError(
            "Canonical Alpha Vantage observation must have a period_end_date."
        )
    expected_identity = derive_alpha_vantage_source_identity_v2(
        source_key=source.key,
        provider_key=observation.provider_key,
        company_id=company.pk,
        period_end_date=period_end_date,
    )
    if source_identity != expected_identity:
        raise AlphaVantageCanonicalIntegrityError(
            "Persisted v2 source identity does not match the frozen resolution."
        )
    resolution_key = derive_alpha_vantage_resolution_key(
        source_key=source.key,
        provider_key=observation.provider_key,
        provider_symbol=resolution.provider_symbol,
        period_end_date=period_end_date,
    )
    family_key = derive_alpha_vantage_candidate_family_key(
        source_identity=source_identity,
        source_key=source.key,
        provider_key=observation.provider_key,
        company_id=company.pk,
        period_end_date=period_end_date,
    )
    candidate_id = derive_alpha_vantage_candidate_uuid(family_key)
    match_factors = _build_match_factors(
        observation=observation,
        snapshot_reference=snapshot_reference,
        resolution=resolution,
        row=row,
        source_identity=source_identity,
        candidate_family_key=family_key,
        resolution_key=resolution_key,
        collision=False,
        reason_code="unique_frozen_symbol_match",
    )

    collision = _find_cross_company_collision(
        resolution_key=resolution_key,
        company_id=company.pk,
    )
    if collision is not None:
        collision_factors = dict(match_factors)
        collision_namespace = dict(cast(dict[str, object], collision_factors[_AV_V2_NAMESPACE]))
        collision_namespace["reason_code"] = "cross_company_resolution_collision"
        collision_namespace["prior_decision_id"] = str(collision.pk)
        collision_factors[_AV_V2_NAMESPACE] = collision_namespace
        decision_result = record_earnings_reconciliation_decision(
            observation=observation,
            decision_type="collision",
            status="open",
            rule_version=ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2,
            match_factors=collision_factors,
            reason="Cross-Company Alpha Vantage symbol resolution requires review.",
            sync_run=run,
        )
        _record_candidate_audit(
            run=run,
            target_type=AuditRecord.TargetType.EARNINGS_RECONCILIATION_DECISION,
            target_id=decision_result.decision.pk,
            action=AuditRecord.Action.CREATE,
            reason="Alpha Vantage cross-Company collision recorded.",
            payload={
                "collision": True,
                "resolution_key": resolution_key,
                "prior_decision_id": str(collision.pk),
            },
        )
        return AlphaVantageCandidateWrite(
            candidate=None,
            decision=decision_result.decision,
            candidate_created=False,
            decision_created=decision_result.created,
            schedule_changed=False,
            collision=True,
        )

    existing = EarningsEvent.objects.filter(pk=candidate_id).first()
    if existing is not None:
        _verify_existing_candidate(
            candidate=existing,
            company=company,
            period_end_date=period_end_date,
        )
        evidence = record_source_evidence(
            raw_data_record=observation.raw_data_record,
            sync_run=run,
            target_type=SourceEvidence.TargetType.EARNINGS_EVENT,
            target_id=existing.pk,
            field_name="company_match",
            raw_value=_resolution_evidence_raw_value(resolution, row),
            normalized_value=match_factors,
            confidence=Decimal("1.0000"),
            normalizer_version=ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2,
        ).evidence
        decision_result = record_earnings_reconciliation_decision(
            observation=observation,
            decision_type="matched_candidate",
            status="resolved",
            rule_version=ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2,
            target_event=existing,
            match_factors=match_factors,
            reason="Exact frozen monitoring-pool symbol match reused an incomplete candidate.",
            sync_run=run,
        )
        schedule_changed = _apply_observation_schedule(
            run=run,
            observation=observation,
            candidate=existing,
        )
        _record_candidate_audit(
            run=run,
            target_type=AuditRecord.TargetType.EARNINGS_EVENT,
            target_id=existing.pk,
            action=AuditRecord.Action.UPDATE,
            reason="Alpha Vantage incomplete candidate reused.",
            payload={
                "decision_id": str(decision_result.decision.pk),
                "source_evidence_id": str(evidence.pk),
                "candidate_family_key": family_key,
            },
        )
        return AlphaVantageCandidateWrite(
            candidate=existing,
            decision=decision_result.decision,
            candidate_created=False,
            decision_created=decision_result.created,
            schedule_changed=schedule_changed,
            collision=False,
        )

    try:
        with transaction.atomic():
            evidence = record_source_evidence(
                raw_data_record=observation.raw_data_record,
                sync_run=run,
                target_type=SourceEvidence.TargetType.EARNINGS_EVENT,
                target_id=candidate_id,
                field_name="company_match",
                raw_value=_resolution_evidence_raw_value(resolution, row),
                normalized_value=match_factors,
                confidence=Decimal("1.0000"),
                normalizer_version=ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2,
            ).evidence
            candidate = EarningsEvent.objects.create(
                id=candidate_id,
                company=company,
                identity_status=IdentityStatus.CANDIDATE,
                identity_key=None,
                identity_rule_version=None,
                period_end_date=period_end_date,
                period_type=None,
                includes_q4=False,
                fiscal_year=None,
                fiscal_calendar_type=FiscalCalendarType.UNKNOWN,
                period_length_weeks=None,
                status=EventStatus.SCHEDULED_ESTIMATED,
                source_evidence=evidence,
            )
            schedule_changed = _apply_observation_schedule(
                run=run,
                observation=observation,
                candidate=candidate,
            )
            decision_result = record_earnings_reconciliation_decision(
                observation=observation,
                decision_type="created_candidate",
                status="resolved",
                rule_version=ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2,
                target_event=candidate,
                match_factors=match_factors,
                reason="Exact frozen monitoring-pool symbol match created an incomplete candidate.",
                sync_run=run,
            )
            _record_candidate_audit(
                run=run,
                target_type=AuditRecord.TargetType.EARNINGS_EVENT,
                target_id=candidate.pk,
                action=AuditRecord.Action.CREATE,
                reason="Alpha Vantage incomplete candidate created.",
                payload={
                    "decision_id": str(decision_result.decision.pk),
                    "source_evidence_id": str(evidence.pk),
                    "candidate_family_key": family_key,
                },
            )
            return AlphaVantageCandidateWrite(
                candidate=candidate,
                decision=decision_result.decision,
                candidate_created=True,
                decision_created=decision_result.created,
                schedule_changed=schedule_changed,
                collision=False,
            )
    except IntegrityError:
        existing = EarningsEvent.objects.filter(pk=candidate_id).first()
        if existing is None:
            raise
        _verify_existing_candidate(
            candidate=existing,
            company=company,
            period_end_date=period_end_date,
        )
        record_source_evidence(
            raw_data_record=observation.raw_data_record,
            sync_run=run,
            target_type=SourceEvidence.TargetType.EARNINGS_EVENT,
            target_id=existing.pk,
            field_name="company_match",
            raw_value=_resolution_evidence_raw_value(resolution, row),
            normalized_value=match_factors,
            confidence=Decimal("1.0000"),
            normalizer_version=ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2,
        )
        decision_result = record_earnings_reconciliation_decision(
            observation=observation,
            decision_type="matched_candidate",
            status="resolved",
            rule_version=ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2,
            target_event=existing,
            match_factors=match_factors,
            reason="Concurrent Alpha Vantage candidate creation reused the winner.",
            sync_run=run,
        )
        schedule_changed = _apply_observation_schedule(
            run=run,
            observation=observation,
            candidate=existing,
        )
        return AlphaVantageCandidateWrite(
            candidate=existing,
            decision=decision_result.decision,
            candidate_created=False,
            decision_created=decision_result.created,
            schedule_changed=schedule_changed,
            collision=False,
        )


def _verify_existing_candidate(
    *,
    candidate: EarningsEvent,
    company: Company,
    period_end_date: date,
) -> None:
    if (
        candidate.identity_status != IdentityStatus.CANDIDATE
        or candidate.identity_key is not None
        or candidate.identity_rule_version is not None
        or candidate.company_id != company.pk
        or candidate.period_end_date != period_end_date
        or candidate.period_type is not None
        or candidate.includes_q4 is not False
        or candidate.fiscal_calendar_type != FiscalCalendarType.UNKNOWN
        or candidate.period_length_weeks is not None
    ):
        raise AlphaVantageCanonicalIntegrityError(
            "Existing Alpha Vantage candidate does not match the ADR-020 contract."
        )


def _find_cross_company_collision(
    *,
    resolution_key: str,
    company_id: UUID,
) -> EarningsReconciliationDecision | None:
    decisions = (
        EarningsReconciliationDecision.objects.filter(
            rule_version=ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2
        )
        .only("id", "match_factors")
        .iterator()
    )
    for decision in decisions:
        factors = decision.match_factors
        namespace = factors.get(_AV_V2_NAMESPACE) if isinstance(factors, dict) else None
        if not isinstance(namespace, dict) or namespace.get("resolution_key") != resolution_key:
            continue
        prior_company = namespace.get("matched_company_id")
        if prior_company != str(company_id) or decision.status == "open":
            return cast(EarningsReconciliationDecision, decision)
    return None


def _build_match_factors(
    *,
    observation: EarningsCalendarObservation,
    snapshot_reference: MonitoringPoolSnapshotReference,
    resolution: CompanyResolution,
    row: AlphaVantageCanonicalRow,
    source_identity: str,
    candidate_family_key: str,
    resolution_key: str,
    collision: bool,
    reason_code: str,
) -> dict[str, object]:
    snapshot = snapshot_reference.snapshot
    return {
        _AV_V2_NAMESPACE: {
            "matcher_version": ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2,
            "resolution_key": resolution_key,
            "source_event_identity": source_identity,
            "candidate_family_key": candidate_family_key,
            "provider_key": observation.provider_key,
            "provider_version": observation.provider_version,
            "parser_version": observation.parser_version,
            "provider_capability_version": ALPHA_VANTAGE_WINDOW_CAPABILITY_VERSION,
            "forward_capability": ALPHA_VANTAGE_FORWARD_CAPABILITY,
            "past_correction_capability": ALPHA_VANTAGE_PAST_CORRECTION_CAPABILITY,
            "coverage_exactness": "nominal",
            "monitoring_pool_as_of": snapshot.as_of_date.isoformat(),
            "monitoring_pool_snapshot_id": str(snapshot.pk),
            "monitoring_pool_hash": snapshot.pool_hash,
            "selector_version": snapshot.selector_version,
            "snapshot_input_revision": snapshot.input_revision,
            "observation_id": str(observation.pk),
            "raw_data_record_id": str(observation.raw_data_record_id),
            "normalized_provider_symbol": resolution.provider_symbol,
            "matched_company_id": str(resolution.company.pk) if resolution.company else None,
            "matched_security_listing_ids": sorted(
                str(listing.pk) for listing in resolution.listings
            ),
            "exact_company_ids": sorted(
                str(company_id) for company_id in resolution.exact_company_ids
            ),
            "raw_report_date": row.report_date.isoformat(),
            "time_of_day": row.release_session,
            "outcome": resolution.outcome.value,
            "reason_code": reason_code,
            "collision": collision,
        }
    }


def _resolution_evidence_raw_value(
    resolution: CompanyResolution,
    row: AlphaVantageCanonicalRow,
) -> dict[str, object]:
    return {
        "provider_symbol": resolution.provider_symbol,
        "company_name": row.company_name,
        "report_date": row.report_date.isoformat(),
        "period_end_date": row.period_end_date.isoformat(),
        "release_session": row.release_session,
    }


def _apply_observation_schedule(
    *,
    run: SyncRun,
    observation: EarningsCalendarObservation,
    candidate: EarningsEvent,
) -> bool:
    changes: dict[str, object] = {}
    if observation.estimated_release_precision == EarningsDatePrecision.DATE_ONLY:
        if observation.estimated_release_date is not None:
            changes["estimated_release"] = observation.estimated_release_date
    elif observation.estimated_release_at is not None:
        changes["estimated_release"] = observation.estimated_release_at
    if observation.release_session != ReleaseSession.UNKNOWN:
        changes["release_session"] = observation.release_session
    if not changes:
        return False
    return update_earnings_schedule(
        earnings_event=candidate,
        changes=changes,
        sync_run=run,
    ).changed


def _record_candidate_audit(
    *,
    run: SyncRun,
    target_type: str,
    target_id: UUID,
    action: str,
    reason: str,
    payload: Mapping[str, object],
) -> None:
    record_system_action(
        sync_run=run,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before={},
        after=dict(payload),
        reason=reason,
        request_id=f"alpha-vantage-canonical:{run.pk}",
    )


def _basis_company_by_listing(
    snapshot_reference: MonitoringPoolSnapshotReference,
) -> dict[UUID, UUID]:
    mapping: dict[UUID, UUID] = {}
    for member in snapshot_reference.members:
        basis_rows = member.basis
        if not isinstance(basis_rows, list) or not basis_rows:
            raise AlphaVantageCanonicalIntegrityError("Frozen snapshot basis is invalid.")
        for basis in basis_rows:
            if not isinstance(basis, dict) or "security_listing_id" not in basis:
                raise AlphaVantageCanonicalIntegrityError("Frozen snapshot basis row is invalid.")
            try:
                listing_id = UUID(str(basis["security_listing_id"]))
            except (TypeError, ValueError) as error:
                raise AlphaVantageCanonicalIntegrityError(
                    "Frozen snapshot basis listing id is invalid."
                ) from error
            existing = mapping.get(listing_id)
            if existing is not None and existing != member.company_id:
                raise AlphaVantageCanonicalIntegrityError(
                    "Frozen snapshot basis listing belongs to multiple Companies."
                )
            mapping[listing_id] = member.company_id
    return mapping


def _normalize_provider_symbol(value: str) -> str:
    if not isinstance(value, str):
        raise AlphaVantageCanonicalIntegrityError("provider_symbol must be text.")
    try:
        return normalize_ticker(value)
    except CompanyServiceError as error:
        raise AlphaVantageCanonicalIntegrityError(
            "provider_symbol is not a valid exact ticker."
        ) from error


def _parse_payload(
    *, raw: RawDataIngestResult, parser_version: str
) -> AlphaVantageCanonicalParseResult:
    try:
        return parse_alpha_vantage_canonical_calendar(bytes(raw.record.payload))
    except Exception:
        mark_raw_data_system_error(
            raw.record.pk,
            parser_version=parser_version,
            parse_error="Alpha Vantage canonical parser system failure.",
            observation=raw.observation,
        )
        raise


def _build_idempotency_key(
    *,
    source: DataSource,
    scope: Mapping[str, object],
    provider_version: str,
    parser_version: str,
    request_id: str | None,
) -> str:
    identity: dict[str, object] = {
        "source_id": str(source.pk),
        "scope": dict(scope),
        "provider_version": provider_version,
        "parser_version": parser_version,
    }
    prefix = _SCHEDULED_IDEMPOTENCY_PREFIX
    if request_id is not None:
        identity["request_id"] = request_id
        prefix = _REQUEST_IDEMPOTENCY_PREFIX
    return f"{prefix}{_sha256_json(identity)}"


def _retire_stale_canonical_runs(*, source_id: UUID) -> None:
    cutoff = timezone.now() - timedelta(seconds=settings.EARNINGS_CALENDAR_STALE_AFTER_SECONDS)
    running = list(
        SyncRun.objects.filter(
            source_id=source_id,
            job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
            provider_version=ALPHA_VANTAGE_CANONICAL_PROVIDER_VERSION,
            status=SyncRun.Status.RUNNING,
        ).order_by("started_at", "pk")
    )
    for run in running:
        if run.heartbeat_at > cutoff:
            raise AlphaVantageCanonicalSyncError(
                "Another canonical Alpha Vantage run is still active."
            )
        mark_sync_run_failed(run.pk, error_summary="Stale canonical Alpha Vantage run retired.")


def _result_from_existing(run: SyncRun) -> AlphaVantageCanonicalSyncResult:
    return AlphaVantageCanonicalSyncResult(
        sync_run=run,
        created=False,
        parse_status=None,
        resolved_row_count=run.created_count + run.updated_count + run.skipped_count,
        candidate_created_count=run.created_count,
        candidate_reused_count=run.updated_count,
        skipped_count=run.skipped_count,
        failed_count=run.failed_count,
    )


def _sha256_json(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()
