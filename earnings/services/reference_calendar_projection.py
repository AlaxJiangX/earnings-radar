"""Read-only reference projection from one persisted Alpha Vantage SyncRun."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.utils import timezone

from audit.models import RawDataObservation, RawDataParseAttempt, SyncRun
from companies.models import SecurityListing
from companies.services import normalize_ticker
from earnings.models import MonitoringPoolMember
from earnings.reference_calendar_parser import (
    REFERENCE_CALENDAR_PARSER_VERSION,
    ReferenceParsedRow,
    ReferenceParseStatus,
    parse_reference_calendar,
)
from earnings.services.monitoring_pool import resolve_monitoring_pool_snapshot_contract
from providers.alpha_vantage_reference import AlphaVantageReferenceProvider

REFERENCE_CALENDAR_JOB_TYPE = "earnings.calendar_reference_window"
REFERENCE_PROJECTION_VERSION = "earnings-reference-projection-v1"
_REFERENCE_SCOPE_KEYS = frozenset(
    {
        "capability",
        "provider_key",
        "provider_version",
        "window_kind",
        "window_start",
        "window_end",
        "monitoring_pool_as_of",
        "monitoring_pool_hash",
        "selector_version",
        "parser_version",
        "projection_version",
    }
)
_SESSION_ORDER = {"pre_market": 0, "post_market": 1, "unknown": 2}


class ReferenceProjectionError(RuntimeError):
    """Persisted reference lineage or scope is inconsistent."""


@dataclass(frozen=True, slots=True)
class ReferenceProjectionRow:
    row_key: tuple[str, str, int]
    projection_row_key: tuple[str, str, tuple[str, str, int]]
    company_id: UUID
    provider_symbol: str
    display_symbol: str
    company_name: str
    estimated_report_date: date
    session: str
    provider_key: str
    data_as_of: datetime
    projected_at: datetime
    freshness: str
    label: str = "estimated / third-party reference"


@dataclass(frozen=True, slots=True)
class ReferenceDiagnostics:
    out_of_pool: int = 0
    ambiguous: int = 0
    invalid_row: int = 0
    duplicate: int = 0
    invalid_basis: int = 0


@dataclass(frozen=True, slots=True)
class ReferenceProjection:
    sync_run_id: UUID
    status: str
    rows: tuple[ReferenceProjectionRow, ...]
    diagnostics: ReferenceDiagnostics
    requested_window_start: date
    requested_window_end: date
    observed_report_date_min: date | None
    observed_report_date_max: date | None
    provider_coverage_end: None
    data_as_of: datetime | None
    projected_at: datetime
    freshness: str


@dataclass(frozen=True, slots=True)
class ReferenceCalendarView:
    projection: ReferenceProjection | None
    latest_run_id: UUID | None
    update_state: str


def validate_reference_scope(scope: object) -> dict[str, str]:
    if not isinstance(scope, dict) or set(scope) != _REFERENCE_SCOPE_KEYS:
        raise ReferenceProjectionError("Reference SyncRun scope shape is invalid.")
    if any(not isinstance(value, str) or not value for value in scope.values()):
        raise ReferenceProjectionError("Reference SyncRun scope values are invalid.")
    if (
        scope["capability"] != "earnings_calendar"
        or scope["provider_key"] != AlphaVantageReferenceProvider.provider_key
        or scope["provider_version"] != AlphaVantageReferenceProvider.provider_version
        or scope["parser_version"] != REFERENCE_CALENDAR_PARSER_VERSION
        or scope["projection_version"] != REFERENCE_PROJECTION_VERSION
        or scope["window_kind"] not in {"scheduled", "retry"}
    ):
        raise ReferenceProjectionError("Reference SyncRun scope contract is unsupported.")
    try:
        start = date.fromisoformat(scope["window_start"])
        end = date.fromisoformat(scope["window_end"])
        as_of = date.fromisoformat(scope["monitoring_pool_as_of"])
    except ValueError:
        raise ReferenceProjectionError("Reference SyncRun dates are invalid.") from None
    if start != as_of or not 1 <= (end - start).days + 1 <= 90:
        raise ReferenceProjectionError("Reference SyncRun window is invalid.")
    if len(scope["monitoring_pool_hash"]) != 64:
        raise ReferenceProjectionError("Reference SyncRun pool hash is invalid.")
    return scope


def project_reference_calendar(
    sync_run_id: UUID, *, now: datetime | None = None
) -> ReferenceProjection:
    """Reparse persisted raw and resolve only the run's frozen snapshot; no writes."""

    projected_at = now or timezone.now()
    if timezone.is_naive(projected_at):
        raise ValueError("Projection time must be timezone-aware.")
    run = SyncRun.objects.select_related("source").get(pk=sync_run_id)
    if run.job_type != REFERENCE_CALENDAR_JOB_TYPE or run.run_mode != SyncRun.RunMode.INGESTION:
        raise ReferenceProjectionError("SyncRun is not a reference ingestion run.")
    scope = validate_reference_scope(run.scope)
    if (
        run.provider_version != scope["provider_version"]
        or run.parser_version != scope["parser_version"]
        or run.source.provider_adapter != scope["provider_key"]
    ):
        raise ReferenceProjectionError("Reference SyncRun version lineage is inconsistent.")
    start = date.fromisoformat(scope["window_start"])
    end = date.fromisoformat(scope["window_end"])
    frozen = resolve_monitoring_pool_snapshot_contract(
        as_of=date.fromisoformat(scope["monitoring_pool_as_of"]),
        selector_version=scope["selector_version"],
        pool_hash=scope["monitoring_pool_hash"],
    )
    observations = list(
        RawDataObservation.objects.filter(sync_run=run)
        .select_related("raw_data_record")
        .order_by("observed_at", "pk")
    )
    if len(observations) > 1:
        raise ReferenceProjectionError("Reference run has more than one raw response.")
    if not observations:
        if run.status != SyncRun.Status.FAILED:
            raise ReferenceProjectionError("Reference run has no persisted raw response.")
        return ReferenceProjection(
            run.pk,
            "FAILED",
            (),
            ReferenceDiagnostics(),
            start,
            end,
            None,
            None,
            None,
            None,
            projected_at,
            "unavailable",
        )

    observation = observations[0]
    raw = observation.raw_data_record
    if raw.source_id != run.source_id:
        raise ReferenceProjectionError("Reference raw response belongs to another source.")
    parsed = parse_reference_calendar(bytes(raw.payload))
    expected_attempt = (
        RawDataParseAttempt.Status.DATA_ERROR
        if parsed.status == ReferenceParseStatus.PAGE_ERROR
        else RawDataParseAttempt.Status.SUCCEEDED
    )
    attempt = RawDataParseAttempt.objects.filter(
        observation=observation, parser_version=REFERENCE_CALENDAR_PARSER_VERSION
    ).first()
    if attempt is None or attempt.status not in {
        expected_attempt,
        RawDataParseAttempt.Status.SYSTEM_ERROR,
    }:
        raise ReferenceProjectionError("Reference parse-attempt lineage is inconsistent.")
    if attempt.status == RawDataParseAttempt.Status.SYSTEM_ERROR:
        status = "FAILED"
    else:
        status = parsed.status.value
    if (
        (status in {"COMPLETE", "EMPTY"} and run.status != SyncRun.Status.SUCCEEDED)
        or (status == "PARTIAL" and run.status != SyncRun.Status.PARTIAL)
        or (status in {"PAGE_ERROR", "FAILED"} and run.status != SyncRun.Status.FAILED)
    ):
        raise ReferenceProjectionError("Reference run status disagrees with its parser result.")

    observed_dates = [row.report_date for row in parsed.rows]
    observed_min = min(observed_dates) if observed_dates else None
    observed_max = max(observed_dates) if observed_dates else None
    freshness = (
        "fresh"
        if projected_at - raw.fetched_at
        <= timedelta(hours=settings.REFERENCE_CALENDAR_STALE_AFTER_HOURS)
        else "stale"
    )
    if status not in {"COMPLETE", "EMPTY"}:
        freshness = "unavailable"
    rows: tuple[ReferenceProjectionRow, ...] = ()
    diagnostics = ReferenceDiagnostics(invalid_row=parsed.invalid_row_count)
    if status == "COMPLETE":
        rows, diagnostics = _match_rows(
            parsed.rows,
            frozen.members,
            snapshot_id=str(frozen.snapshot.pk),
            as_of=frozen.snapshot.as_of_date,
            start=start,
            end=end,
            provider_key=scope["provider_key"],
            data_as_of=raw.fetched_at,
            projected_at=projected_at,
            freshness=freshness,
            raw_record_id=str(raw.pk),
            invalid_row_count=parsed.invalid_row_count,
        )
    return ReferenceProjection(
        run.pk,
        status,
        rows,
        diagnostics,
        start,
        end,
        observed_min,
        observed_max,
        None,
        raw.fetched_at,
        projected_at,
        freshness,
    )


def latest_reference_calendar(
    *, source_id: UUID, now: datetime | None = None
) -> ReferenceCalendarView:
    """Show the latest complete/empty projection, preserving failure diagnostics."""

    runs = (
        SyncRun.objects.filter(source_id=source_id, job_type=REFERENCE_CALENDAR_JOB_TYPE)
        .exclude(status=SyncRun.Status.RUNNING)
        .order_by("-started_at", "-pk")
    )
    latest_id: UUID | None = None
    latest_state = "unavailable"
    for run in runs:
        if latest_id is None:
            latest_id = run.pk
            latest_state = (
                "update_incomplete"
                if run.status == SyncRun.Status.PARTIAL
                else "latest_failed"
                if run.status == SyncRun.Status.FAILED
                else "current"
            )
        if run.status != SyncRun.Status.SUCCEEDED:
            continue
        projection = project_reference_calendar(run.pk, now=now)
        if projection.status in {"COMPLETE", "EMPTY"}:
            return ReferenceCalendarView(projection, latest_id, latest_state)
    return ReferenceCalendarView(None, latest_id, "unavailable")


def _match_rows(
    parsed_rows: tuple[ReferenceParsedRow, ...],
    members: tuple[MonitoringPoolMember, ...],
    *,
    snapshot_id: str,
    as_of: date,
    start: date,
    end: date,
    provider_key: str,
    data_as_of: datetime,
    projected_at: datetime,
    freshness: str,
    raw_record_id: str,
    invalid_row_count: int,
) -> tuple[tuple[ReferenceProjectionRow, ...], ReferenceDiagnostics]:
    listing_ids: set[UUID] = set()
    for member in members:
        for basis in member.basis:
            try:
                listing_ids.add(UUID(basis["security_listing_id"]))
            except (TypeError, ValueError, KeyError):
                raise ReferenceProjectionError("Frozen pool basis listing is invalid.") from None
    listings = {
        listing.pk: listing
        for listing in SecurityListing.objects.filter(pk__in=listing_ids).select_related("company")
    }
    symbols: dict[str, dict[UUID, SecurityListing]] = {}
    invalid_basis = 0
    for member in members:
        valid: list[SecurityListing] = []
        for basis in member.basis:
            listing = listings.get(UUID(basis["security_listing_id"]))
            if (
                listing is None
                or listing.company_id != member.company_id
                or not listing.effective_from <= as_of
                or (listing.effective_to is not None and as_of >= listing.effective_to)
                or date.fromisoformat(basis["effective_from"]) > as_of
                or (
                    basis["effective_to"] is not None
                    and as_of >= date.fromisoformat(basis["effective_to"])
                )
            ):
                valid = []
                invalid_basis += 1
                break
            valid.append(listing)
        for listing in valid:
            symbols.setdefault(normalize_ticker(listing.ticker), {})[listing.company_id] = listing
    projected: list[ReferenceProjectionRow] = []
    out_of_pool = ambiguous = duplicate = 0
    seen: set[tuple[UUID, date, str]] = set()
    for row in parsed_rows:
        if not start <= row.report_date <= end:
            continue
        try:
            key = normalize_ticker(row.provider_symbol)
        except ValueError:
            out_of_pool += 1
            continue
        matches = symbols.get(key, {})
        if not matches:
            out_of_pool += 1
            continue
        if len(matches) > 1:
            ambiguous += 1
            continue
        company_id, listing = next(iter(matches.items()))
        event_key = (company_id, row.report_date, row.session)
        if event_key in seen:
            duplicate += 1
            continue
        seen.add(event_key)
        row_key = (raw_record_id, REFERENCE_CALENDAR_PARSER_VERSION, row.raw_position)
        projected.append(
            ReferenceProjectionRow(
                row_key=row_key,
                projection_row_key=(REFERENCE_PROJECTION_VERSION, snapshot_id, row_key),
                company_id=company_id,
                provider_symbol=row.provider_symbol,
                display_symbol=listing.ticker,
                company_name=listing.company.display_name,
                estimated_report_date=row.report_date,
                session=row.session,
                provider_key=provider_key,
                data_as_of=data_as_of,
                projected_at=projected_at,
                freshness=freshness,
            )
        )
    projected.sort(
        key=lambda row: (
            row.estimated_report_date,
            _SESSION_ORDER[row.session],
            row.provider_symbol,
            row.row_key[0],
            row.row_key[2],
        )
    )
    return tuple(projected), ReferenceDiagnostics(
        out_of_pool, ambiguous, invalid_row_count, duplicate, invalid_basis
    )
