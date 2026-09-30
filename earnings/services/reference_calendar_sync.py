"""Owned, raw-first synchronization for the private reference calendar."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.models import DataSource, SyncRun
from audit.services import (
    mark_raw_data_parse_failed,
    mark_raw_data_parsed,
    mark_raw_data_system_error,
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    record_raw_data_observation,
    start_sync_run_with_result,
    update_sync_run_counts,
)
from earnings.reference_calendar_parser import (
    REFERENCE_CALENDAR_PARSER_VERSION,
    ReferenceParseStatus,
    parse_reference_calendar,
)
from earnings.services.calendar_run_ownership import calendar_run_ownership
from earnings.services.monitoring_pool import (
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
    select_monitoring_pool,
)
from earnings.services.reference_calendar_projection import (
    REFERENCE_CALENDAR_JOB_TYPE,
    REFERENCE_PROJECTION_VERSION,
)
from providers.alpha_vantage_reference import (
    ALPHA_VANTAGE_REFERENCE_URL,
    AlphaVantageReferenceProvider,
)
from providers.exceptions import ProviderError
from providers.types import ProviderCapability, ProviderRequest


class ReferenceCalendarSyncError(RuntimeError):
    """The reference run cannot safely start or complete."""


@dataclass(frozen=True, slots=True)
class ReferenceSyncResult:
    sync_run: SyncRun
    created: bool
    parse_status: str | None


def execute_reference_calendar_sync(
    *,
    source: DataSource,
    as_of: date,
    enabled_index_codes: Iterable[str],
    provider: AlphaVantageReferenceProvider | None = None,
    forward_horizon_days: int = 90,
    request_id: str | None = None,
) -> ReferenceSyncResult:
    """One market-wide fetch; no canonical earnings writes or runtime scheduling."""

    if not transaction.get_autocommit():
        raise ReferenceCalendarSyncError("Reference synchronization requires autocommit.")
    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        raise ValueError("as_of must be a date.")
    if (
        isinstance(forward_horizon_days, bool)
        or not isinstance(forward_horizon_days, int)
        or not 1 <= forward_horizon_days <= 90
    ):
        raise ValueError("forward_horizon_days must be between 1 and 90.")
    if request_id is not None and (not request_id.strip() or len(request_id) > 100):
        raise ValueError("request_id must contain 1 to 100 characters.")
    active_provider = provider or AlphaVantageReferenceProvider.from_environment()
    if not isinstance(active_provider, AlphaVantageReferenceProvider):
        raise ReferenceCalendarSyncError("Only the approved Mode A reference provider is allowed.")
    if source.pk is None or source._state.adding:
        raise ReferenceCalendarSyncError("DataSource must be persisted.")
    current_source = DataSource.objects.get(pk=source.pk)
    if (
        current_source.source_type != DataSource.SourceType.EARNINGS_CALENDAR
        or not current_source.is_enabled
        or current_source.provider_adapter != active_provider.provider_key
        or current_source.base_url.rstrip("/") != "https://www.alphavantage.co"
    ):
        raise ReferenceCalendarSyncError("DataSource is not an enabled Alpha Vantage calendar.")

    with calendar_run_ownership(source_id=current_source.pk, job_type=REFERENCE_CALENDAR_JOB_TYPE):
        _retire_stale_reference_runs(source_id=current_source.pk)
        pool = select_monitoring_pool(
            as_of=as_of,
            selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
            enabled_index_codes=enabled_index_codes,
        )
        window_end = as_of + timedelta(days=forward_horizon_days - 1)
        scope = {
            "capability": ProviderCapability.EARNINGS_CALENDAR.value,
            "provider_key": active_provider.provider_key,
            "provider_version": active_provider.provider_version,
            "window_kind": "retry" if request_id is not None else "scheduled",
            "window_start": as_of.isoformat(),
            "window_end": window_end.isoformat(),
            "monitoring_pool_as_of": as_of.isoformat(),
            "monitoring_pool_hash": pool.monitoring_pool_hash,
            "selector_version": pool.selector_version,
            "parser_version": REFERENCE_CALENDAR_PARSER_VERSION,
            "projection_version": REFERENCE_PROJECTION_VERSION,
        }
        identity = {"source_id": str(current_source.pk), "scope": scope}
        if request_id is not None:
            identity["request_id"] = request_id
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        started = start_sync_run_with_result(
            job_type=REFERENCE_CALENDAR_JOB_TYPE,
            source=current_source,
            scope=scope,
            idempotency_key=f"earnings-reference:v1:{digest}",
            parser_version=REFERENCE_CALENDAR_PARSER_VERSION,
            provider_version=active_provider.provider_version,
            require_provider_version=True,
        )
        if not started.created:
            if started.sync_run.status == SyncRun.Status.SUCCEEDED:
                return ReferenceSyncResult(started.sync_run, False, None)
            raise ReferenceCalendarSyncError(
                "Existing reference run is not successful; use a new request_id for retry."
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
            try:
                parsed = parse_reference_calendar(result.raw_content)
            except Exception:
                mark_raw_data_system_error(
                    raw.record.pk,
                    parser_version=REFERENCE_CALENDAR_PARSER_VERSION,
                    parse_error="Reference parser system failure.",
                    observation=raw.observation,
                )
                raise
            if parsed.status == ReferenceParseStatus.PAGE_ERROR:
                mark_raw_data_parse_failed(
                    raw.record.pk,
                    parser_version=REFERENCE_CALENDAR_PARSER_VERSION,
                    parse_error=f"Reference CSV {parsed.error_code}.",
                    observation=raw.observation,
                )
                update_sync_run_counts(run.pk, failed_delta=1)
                return ReferenceSyncResult(
                    mark_sync_run_failed(run.pk, error_summary="Reference CSV is invalid."),
                    True,
                    parsed.status.value,
                )
            mark_raw_data_parsed(
                raw.record.pk,
                parser_version=REFERENCE_CALENDAR_PARSER_VERSION,
                observation=raw.observation,
            )
            if parsed.status == ReferenceParseStatus.PARTIAL:
                update_sync_run_counts(run.pk, failed_delta=parsed.invalid_row_count)
                finished = mark_sync_run_partial(
                    run.pk, error_summary="Reference CSV contains invalid rows."
                )
            else:
                finished = mark_sync_run_succeeded(run.pk)
            return ReferenceSyncResult(finished, True, parsed.status.value)
        except ProviderError:
            mark_sync_run_failed(run.pk, error_summary="Reference provider fetch failed.")
            raise
        except Exception:
            if SyncRun.objects.get(pk=run.pk).status == SyncRun.Status.RUNNING:
                mark_sync_run_failed(run.pk, error_summary="Reference synchronization failed.")
            raise


def _retire_stale_reference_runs(*, source_id: UUID) -> None:
    cutoff = timezone.now() - timedelta(seconds=settings.EARNINGS_CALENDAR_STALE_AFTER_SECONDS)
    running = list(
        SyncRun.objects.filter(
            source_id=source_id,
            job_type=REFERENCE_CALENDAR_JOB_TYPE,
            status=SyncRun.Status.RUNNING,
        ).order_by("started_at", "pk")
    )
    for run in running:
        if run.heartbeat_at > cutoff:
            raise ReferenceCalendarSyncError("Another reference run is still active.")
        mark_sync_run_failed(run.pk, error_summary="Stale reference run retired.")
