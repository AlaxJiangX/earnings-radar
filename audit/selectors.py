"""Read-only observability selectors for the data-health surface (Stage 7.3).

The freshness measurement type is shared by every pipeline selector; the
SyncRun snapshots feed the task-overdue and source-failure signals defined
in ``docs/architecture.md`` section 11.  None of these queries write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Count, Max
from django.utils import timezone

from audit.models import DataSource, SyncRun

INGESTION_RUN_MODE = "ingestion"
FAILURE_STATUSES = ("failed", "partial")
ACTIVE_FAILURE_WINDOW = timedelta(hours=24)
# Shared default lookback window for the five freshness routes (one week;
# long enough to cover the daily pipelines' samples in normal operation).
DEFAULT_FRESHNESS_LOOKBACK_HOURS = 168


@dataclass(frozen=True, slots=True)
class FreshnessMeasurement:
    """One pipeline's freshness measurement over a lookback window.

    Implements the unified start/end formulas of ``docs/data-sources.md``
    section 6.  ``measured_count`` counts in-window samples where both
    formula endpoints exist.  ``unmeasured_count`` counts in-window rows
    that lack the start endpoint (upstream time not provided); absence is
    reported honestly, never as a zero delay.  ``anomaly_count`` counts
    samples whose end precedes the start (upstream time anomaly per the
    SEC row of section 6); anomalies are excluded from the within/over
    counts.  All datetimes are timezone-aware UTC.
    """

    measured_count: int
    within_target_count: int
    over_target_count: int
    unmeasured_count: int
    anomaly_count: int
    max_latency_seconds: float | None
    latest_sample_at: datetime | None


def measure_latencies(
    *,
    latencies: list[float],
    unmeasured_count: int,
    target_seconds: float,
    latest_sample_at: datetime | None,
) -> FreshnessMeasurement:
    """Reduce raw latency samples into a :class:`FreshnessMeasurement`.

    Negative latencies (end before start) are upstream time anomalies:
    they are counted separately and excluded from the target comparison.
    """

    normal = [value for value in latencies if value >= 0]
    anomalies = len(latencies) - len(normal)
    within = sum(1 for value in normal if value <= target_seconds)
    return FreshnessMeasurement(
        measured_count=len(latencies),
        within_target_count=within,
        over_target_count=len(normal) - within,
        unmeasured_count=unmeasured_count,
        anomaly_count=anomalies,
        max_latency_seconds=max(normal) if normal else None,
        latest_sample_at=latest_sample_at,
    )


@dataclass(frozen=True, slots=True)
class SyncRunTaskSnapshot:
    """Last ingestion state of one SyncRun job type."""

    job_type: str
    last_run_started_at: datetime | None
    last_run_finished_at: datetime | None
    last_run_status: str | None
    last_run_source_key: str | None
    last_run_error_summary: str
    running_count: int
    failed_partial_count_24h: int


@dataclass(frozen=True, slots=True)
class SyncRunSourceSnapshot:
    """Last ingestion state of one (job type, source) pair."""

    job_type: str
    source_key: str
    last_run_started_at: datetime | None
    last_run_status: str | None
    last_run_error_summary: str
    failed_partial_count_24h: int


def ingestion_job_types() -> list[str]:
    """Return every job type that has at least one ingestion SyncRun."""

    return list(
        SyncRun.objects.filter(run_mode=INGESTION_RUN_MODE)
        .values_list("job_type", flat=True)
        .distinct()
    )


def _recent_failure_counts(job_type: str) -> int:
    return SyncRun.objects.filter(
        job_type=job_type,
        run_mode=INGESTION_RUN_MODE,
        status__in=FAILURE_STATUSES,
        started_at__gte=timezone.now() - ACTIVE_FAILURE_WINDOW,
    ).count()


def ingestion_task_snapshots() -> list[SyncRunTaskSnapshot]:
    """Summarize the last ingestion run per job type for the health page."""

    snapshots: list[SyncRunTaskSnapshot] = []
    for job_type in sorted(ingestion_job_types()):
        last = (
            SyncRun.objects.filter(job_type=job_type, run_mode=INGESTION_RUN_MODE)
            .select_related("source")
            .order_by("-started_at", "-heartbeat_at")
            .first()
        )
        running_count = SyncRun.objects.filter(
            job_type=job_type, run_mode=INGESTION_RUN_MODE, status=SyncRun.Status.RUNNING
        ).count()
        snapshots.append(
            SyncRunTaskSnapshot(
                job_type=job_type,
                last_run_started_at=last.started_at if last else None,
                last_run_finished_at=last.finished_at if last else None,
                last_run_status=last.status if last else None,
                last_run_source_key=last.source.key if last else None,
                last_run_error_summary=last.error_summary if last else "",
                running_count=running_count,
                failed_partial_count_24h=_recent_failure_counts(job_type),
            )
        )
    return snapshots


def ingestion_source_snapshots() -> list[SyncRunSourceSnapshot]:
    """Summarize the last ingestion run per (job type, source) pair."""

    pairs = list(
        SyncRun.objects.filter(run_mode=INGESTION_RUN_MODE)
        .values("job_type", "source_id")
        .annotate(last_started_at=Max("started_at"))
        .order_by("job_type", "source_id")
    )
    recent_failure_rows = (
        SyncRun.objects.filter(
            run_mode=INGESTION_RUN_MODE,
            status__in=FAILURE_STATUSES,
            started_at__gte=timezone.now() - ACTIVE_FAILURE_WINDOW,
        )
        .values("job_type", "source_id")
        .annotate(failures=Count("id"))
    )
    recent_failures = {
        (row["job_type"], row["source_id"]): row["failures"] for row in recent_failure_rows
    }
    sources = {
        source.pk: source
        for source in DataSource.objects.filter(pk__in={pair["source_id"] for pair in pairs})
    }
    snapshots: list[SyncRunSourceSnapshot] = []
    for pair in pairs:
        last = (
            SyncRun.objects.filter(
                job_type=pair["job_type"],
                source_id=pair["source_id"],
                run_mode=INGESTION_RUN_MODE,
                started_at=pair["last_started_at"],
            )
            .order_by("-heartbeat_at")
            .first()
        )
        source = sources.get(pair["source_id"])
        snapshots.append(
            SyncRunSourceSnapshot(
                job_type=pair["job_type"],
                source_key=source.key if source else str(pair["source_id"]),
                last_run_started_at=last.started_at if last else None,
                last_run_status=last.status if last else None,
                last_run_error_summary=last.error_summary if last else "",
                failed_partial_count_24h=recent_failures.get(
                    (pair["job_type"], pair["source_id"]), 0
                ),
            )
        )
    return snapshots


def failing_runs_since(*, since: datetime) -> int:
    """Count ingestion runs that failed or partially failed since ``since``."""

    return SyncRun.objects.filter(
        run_mode=INGESTION_RUN_MODE,
        status__in=FAILURE_STATUSES,
        started_at__gte=since,
    ).count()
