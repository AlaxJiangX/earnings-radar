"""Read-only Filing selectors for cross-module page composition."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from django.db.models import QuerySet

from audit.selectors import (
    DEFAULT_FRESHNESS_LOOKBACK_HOURS,
    FreshnessMeasurement,
    measure_latencies,
)
from filings.models import Filing


def recent_filings_for_companies(
    *,
    company_ids: Sequence[UUID],
    limit: int,
) -> QuerySet[Filing]:
    """Return the most recently accepted SEC filings for the given Companies."""

    return (
        Filing.objects.filter(company_id__in=company_ids)
        .select_related("company")
        .order_by("-accepted_at", "id")[:limit]
    )


def recent_filings(*, limit: int) -> QuerySet[Filing]:
    """Return the most recently accepted SEC filings across all Companies."""

    return Filing.objects.select_related("company").order_by("-accepted_at", "id")[:limit]


SEC_DISCOVERY_TARGET_SECONDS = 15 * 60


def measure_sec_discovery_freshness(
    *,
    now: datetime,
    lookback_hours: int = DEFAULT_FRESHNESS_LOOKBACK_HOURS,
    target_seconds: float = SEC_DISCOVERY_TARGET_SECONDS,
) -> FreshnessMeasurement:
    """Measure SEC filing-discovery freshness (data-sources.md section 6).

    Latency per sample is ``Filing.created_at - Filing.accepted_at`` over
    filings created inside the lookback window.  A negative latency means
    SEC ``accepted_at`` is in the future relative to our write time and is
    counted as an upstream time anomaly instead of a delay.
    """

    window_start = now - timedelta(hours=lookback_hours)
    latencies: list[float] = []
    latest_sample_at: datetime | None = None
    for created_at, accepted_at in Filing.objects.filter(created_at__gte=window_start).values_list(
        "created_at", "accepted_at"
    ):
        latencies.append((created_at - accepted_at).total_seconds())
        if latest_sample_at is None or created_at > latest_sample_at:
            latest_sample_at = created_at
    return measure_latencies(
        latencies=latencies,
        unmeasured_count=0,
        target_seconds=target_seconds,
        latest_sample_at=latest_sample_at,
    )
