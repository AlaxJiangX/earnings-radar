"""Data-health composition layer and staff-only view (roadmap 7.3).

Cross-app composition lives here, mirroring ``config.pages``: the view
only orchestrates HTTP and rendering, every read goes through the owning
app's public selectors, and the status vocabulary below is the single
normalization point.

Status vocabulary (data-health routes):

- ``blocked``        — the route has a known non-live gate (license or
  fixture-first); any measured data is fixture/dev data and is labeled.
- ``not_configured`` — no enabled DataSource exists for the route yet
  (e.g. the earnings calendar without a production provider source).
- ``no_data``        — an enabled source exists but the lookback window
  has no measurable samples.
- ``stale``          — measured samples exist but at least one missed the
  freshness target of ``docs/data-sources.md`` section 6.
- ``fresh``          — measured samples exist and all met the target.

Task-overdue statuses (expected frequencies from ``docs/data-sources.md``
section 7): ``never_run`` / ``overdue`` / ``ok``; job types with no
documented frequency are shown as ``unmapped`` and are deliberately never
judged overdue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from audit.models import DataSource
from audit.selectors import (
    FreshnessMeasurement,
    ingestion_source_snapshots,
    ingestion_task_snapshots,
)
from earnings.selectors import (
    CALENDAR_FRESHNESS_TARGET_SECONDS,
    IR_FRESHNESS_TARGET_SECONDS,
    measure_earnings_calendar_freshness,
    measure_investor_relations_freshness,
)
from filings.selectors import (
    SEC_DISCOVERY_TARGET_SECONDS,
    measure_sec_discovery_freshness,
)
from indexes.selectors import (
    INDEX_ANNOUNCEMENT_TARGET_SECONDS,
    measure_index_announcement_freshness,
)
from notifications.selectors import (
    P0P1_SEND_TARGET_SECONDS,
    measure_p0p1_send_freshness,
    notification_backlog_snapshot,
)

DEFAULT_LOOKBACK_HOURS = 168
# A job type is overdue when its last ingestion run started more than one
# full missed cycle ago (one grace cycle on top of the documented
# frequency).  Cron registration itself is a Stage 8 deployment concern,
# so ``never_run`` is reported honestly without an overdue judgment.
OVERDUE_GRACE_MULTIPLIER = 2

ROUTE_STATUS_BLOCKED = "blocked"
ROUTE_STATUS_NOT_CONFIGURED = "not_configured"
ROUTE_STATUS_NO_DATA = "no_data"
ROUTE_STATUS_STALE = "stale"
ROUTE_STATUS_FRESH = "fresh"

GATE_LIVE = "live"
GATE_LICENSE_BLOCKED = "license_blocked"
GATE_FIXTURE_FIRST = "fixture_first"

TASK_STATUS_OK = "ok"
TASK_STATUS_OVERDUE = "overdue"
TASK_STATUS_NEVER_RUN = "never_run"
TASK_STATUS_UNMAPPED = "unmapped"


@dataclass(frozen=True, slots=True)
class FreshnessRoute:
    """One freshness pipeline of ``docs/data-sources.md`` section 6."""

    key: str
    label: str
    target_seconds: int
    gate: str
    gate_doc_ref: str
    source_type_filter: str | None  # None: internal pipeline, no external source

    @property
    def target_label(self) -> str:
        return format_duration(self.target_seconds)


FRESHNESS_ROUTES: tuple[FreshnessRoute, ...] = (
    FreshnessRoute(
        key="sec_filing_discovery",
        label="SEC filing discovery",
        target_seconds=SEC_DISCOVERY_TARGET_SECONDS,
        gate=GATE_LIVE,
        gate_doc_ref="data-sources.md §6/§7",
        source_type_filter=DataSource.SourceType.SEC,
    ),
    FreshnessRoute(
        key="earnings_calendar",
        label="Earnings calendar update",
        target_seconds=CALENDAR_FRESHNESS_TARGET_SECONDS,
        gate=GATE_LIVE,
        gate_doc_ref="ADR-019 / ADR-020 (personal-use license)",
        source_type_filter=DataSource.SourceType.EARNINGS_CALENDAR,
    ),
    FreshnessRoute(
        key="investor_relations",
        label="IR confirmation update",
        target_seconds=IR_FRESHNESS_TARGET_SECONDS,
        gate=GATE_LICENSE_BLOCKED,
        gate_doc_ref="ADR-022; data-sources.md §8 (no approved live source)",
        source_type_filter=DataSource.SourceType.INVESTOR_RELATIONS,
    ),
    FreshnessRoute(
        key="index_announcements",
        label="Index announcement update",
        target_seconds=INDEX_ANNOUNCEMENT_TARGET_SECONDS,
        gate=GATE_FIXTURE_FIRST,
        gate_doc_ref="ADR-005; data-sources.md §10 (index sources unconfirmed)",
        source_type_filter=DataSource.SourceType.INDEX,
    ),
    FreshnessRoute(
        key="p0p1_notifications",
        label="P0/P1 notification send",
        target_seconds=P0P1_SEND_TARGET_SECONDS,
        gate=GATE_LIVE,
        gate_doc_ref="data-sources.md §6; ADR-025/ADR-027",
        source_type_filter=None,
    ),
)

# Expected ingestion frequencies (seconds) from data-sources.md §7.  Job
# types absent from this table are reported as ``unmapped`` and never
# judged overdue; new job types must be added here with a documentation
# basis or surfaced to the product owner.
EXPECTED_JOB_FREQUENCIES: dict[str, int] = {
    "filings.sec_edgar": 10 * 60,
    "earnings.calendar_window": 24 * 60 * 60,
    "earnings.calendar_reference_window": 24 * 60 * 60,
    "earnings.ir_confirmation": 6 * 60 * 60,
    "indexes.constituent_snapshot": 24 * 60 * 60,
}

EXPECTED_JOB_LABELS: dict[str, str] = {
    "filings.sec_edgar": "every 5–15 minutes (10-minute budget)",
    "earnings.calendar_window": "daily",
    "earnings.calendar_reference_window": "daily",
    "earnings.ir_confirmation": "every 6 hours",
    "indexes.constituent_snapshot": "daily",
}


def format_duration(seconds: float) -> str:
    """Render a duration for the health page (e.g. ``15m`` / ``24h``)."""

    total_minutes = round(seconds / 60)
    if total_minutes >= 60 and total_minutes % 60 == 0:
        return f"{total_minutes // 60}h"
    return f"{total_minutes}m"


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{round(seconds)}s"
    return format_duration(seconds)


def _route_is_configured(route: FreshnessRoute) -> bool:
    if route.source_type_filter is None:
        return True
    return DataSource.objects.filter(source_type=route.source_type_filter, is_enabled=True).exists()


def route_status(route: FreshnessRoute, measurement: FreshnessMeasurement) -> str:
    """Normalize one route to the documented status vocabulary."""

    if route.gate != GATE_LIVE:
        return ROUTE_STATUS_BLOCKED
    if not _route_is_configured(route):
        return ROUTE_STATUS_NOT_CONFIGURED
    if measurement.measured_count == 0:
        return ROUTE_STATUS_NO_DATA
    if measurement.over_target_count > 0:
        return ROUTE_STATUS_STALE
    return ROUTE_STATUS_FRESH


def task_overdue_status(
    *,
    job_type: str,
    last_run_started_at: datetime | None,
    now: datetime,
) -> str:
    expected = EXPECTED_JOB_FREQUENCIES.get(job_type)
    if expected is None:
        return TASK_STATUS_UNMAPPED
    if last_run_started_at is None:
        return TASK_STATUS_NEVER_RUN
    if now - last_run_started_at > timedelta(seconds=expected * OVERDUE_GRACE_MULTIPLIER):
        return TASK_STATUS_OVERDUE
    return TASK_STATUS_OK


def build_data_health_report(*, now: datetime | None = None) -> dict[str, Any]:
    """Assemble the full data-health report from the app selectors."""

    resolved_now = now or timezone.now()
    routes: list[dict[str, Any]] = []
    for route in FRESHNESS_ROUTES:
        measurement = _measure_route(route.key, now=resolved_now)
        status = route_status(route, measurement)
        routes.append(
            {
                "key": route.key,
                "label": route.label,
                "target": route.target_label,
                "gate": route.gate,
                "gate_doc_ref": route.gate_doc_ref,
                "configured": _route_is_configured(route),
                "status": status,
                "measured_count": measurement.measured_count,
                "within_target_count": measurement.within_target_count,
                "over_target_count": measurement.over_target_count,
                "unmeasured_count": measurement.unmeasured_count,
                "anomaly_count": measurement.anomaly_count,
                "max_latency": format_age(measurement.max_latency_seconds),
                "latest_sample_at": measurement.latest_sample_at,
            }
        )

    tasks: list[dict[str, Any]] = []
    for snapshot in ingestion_task_snapshots():
        tasks.append(
            {
                "job_type": snapshot.job_type,
                "expected": EXPECTED_JOB_LABELS.get(snapshot.job_type, "—"),
                "status": task_overdue_status(
                    job_type=snapshot.job_type,
                    last_run_started_at=snapshot.last_run_started_at,
                    now=resolved_now,
                ),
                "last_run_started_at": snapshot.last_run_started_at,
                "last_run_status": snapshot.last_run_status,
                "last_run_source_key": snapshot.last_run_source_key,
                "last_run_error_summary": snapshot.last_run_error_summary,
                "running_count": snapshot.running_count,
                "failed_partial_count_24h": snapshot.failed_partial_count_24h,
            }
        )

    sources: list[dict[str, Any]] = []
    for source_snapshot in ingestion_source_snapshots():
        sources.append(
            {
                "job_type": source_snapshot.job_type,
                "source_key": source_snapshot.source_key,
                "last_run_started_at": source_snapshot.last_run_started_at,
                "last_run_status": source_snapshot.last_run_status,
                "last_run_error_summary": source_snapshot.last_run_error_summary,
                "failed_partial_count_24h": source_snapshot.failed_partial_count_24h,
            }
        )

    backlog = notification_backlog_snapshot(now=resolved_now)
    return {
        "generated_at": resolved_now,
        "lookback_hours": DEFAULT_LOOKBACK_HOURS,
        "routes": routes,
        "tasks": tasks,
        "sources": sources,
        "backlog": backlog,
    }


def _measure_route(route_key: str, *, now: datetime) -> FreshnessMeasurement:
    if route_key == "sec_filing_discovery":
        return measure_sec_discovery_freshness(now=now)
    if route_key == "earnings_calendar":
        return measure_earnings_calendar_freshness(now=now)
    if route_key == "investor_relations":
        return measure_investor_relations_freshness(now=now)
    if route_key == "index_announcements":
        return measure_index_announcement_freshness(now=now)
    if route_key == "p0p1_notifications":
        return measure_p0p1_send_freshness(now=now)
    raise ValueError(f"Unknown freshness route: {route_key}")


@staff_member_required
def data_health_view(request: HttpRequest) -> HttpResponse:
    """Staff-only operator page; architecture.md section 11 (admin checks)."""

    report = build_data_health_report()
    backlog = report["backlog"]
    report["backlog_rows"] = [
        {
            "label": "Pending",
            "total": backlog.pending_total,
            "p0p1": backlog.pending_p0p1,
            "oldest": format_age(backlog.oldest_pending_seconds),
            "oldest_note": (
                f"P0/P1 oldest: {format_age(backlog.oldest_pending_p0p1_seconds)}"
                if backlog.oldest_pending_p0p1_seconds is not None
                else ""
            ),
        },
        {
            "label": "Processing (crash residue, ADR-027; not auto-reclaimed)",
            "total": backlog.processing_total,
            "p0p1": "—",
            "oldest": format_age(backlog.oldest_processing_seconds),
            "oldest_note": "",
        },
        {
            "label": "Failed (terminal, ADR-027 retry ladder)",
            "total": backlog.failed_total,
            "p0p1": backlog.failed_p0p1,
            "oldest": format_age(backlog.oldest_failed_seconds),
            "oldest_note": "",
        },
    ]
    return render(request, "pages/data_health.html", {"report": report})
