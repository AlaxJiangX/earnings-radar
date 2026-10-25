"""Read-only notification queries for pages and other modules."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.db.models import Count, Min, Q, QuerySet

from audit.selectors import (
    DEFAULT_FRESHNESS_LOOKBACK_HOURS,
    FreshnessMeasurement,
    measure_latencies,
)
from notifications.models import Notification

if TYPE_CHECKING:
    from accounts.models import User

NOTIFICATION_PAGE_LIMIT = 100


def notifications_for_user(
    user: User, *, limit: int = NOTIFICATION_PAGE_LIMIT
) -> QuerySet[Notification]:
    """Return the user's notifications, newest first; never other users'."""

    return Notification.objects.filter(user=user).order_by("-created_at", "-id")[:limit]


def resolve_company_names(notifications: Iterable[Notification]) -> dict[uuid.UUID, str]:
    """Batch-resolve the subject company name for each notification.

    One query per related_object_type; notifications whose subject row no
    longer resolves simply get no entry.
    """

    from earnings.models import EarningsDateChange, EarningsEvent
    from filings.models import Filing
    from indexes.models import IndexChangeEvent, IndexChangeLeg

    names: dict[uuid.UUID, str] = {}

    def _collect(related_type: str) -> list[uuid.UUID]:
        return [
            notification.related_object_id
            for notification in notifications
            if notification.related_object_type == related_type
        ]

    event_ids = _collect(Notification.RelatedObjectType.EARNINGS_EVENT)
    if event_ids:
        event_rows = EarningsEvent.objects.filter(pk__in=event_ids).values_list(
            "pk", "company__display_name"
        )
        names.update(dict(event_rows))

    change_ids = _collect(Notification.RelatedObjectType.EARNINGS_DATE_CHANGE)
    if change_ids:
        change_rows = EarningsDateChange.objects.filter(pk__in=change_ids).values_list(
            "pk", "earnings_event__company__display_name"
        )
        names.update(dict(change_rows))

    filing_ids = _collect(Notification.RelatedObjectType.FILING)
    if filing_ids:
        filing_rows = Filing.objects.filter(pk__in=filing_ids).values_list(
            "pk", "company__display_name"
        )
        names.update(dict(filing_rows))

    leg_ids = _collect(Notification.RelatedObjectType.INDEX_CHANGE_LEG)
    if leg_ids:
        leg_rows = IndexChangeLeg.objects.filter(pk__in=leg_ids).values_list(
            "pk", "security_listing__company__display_name"
        )
        names.update(dict(leg_rows))

    change_event_ids = _collect(Notification.RelatedObjectType.INDEX_CHANGE_EVENT)
    if change_event_ids:
        change_event_rows = IndexChangeEvent.objects.filter(pk__in=change_event_ids).values_list(
            "pk", "company__display_name"
        )
        names.update(dict(change_event_rows))

    return names


# ---------------------------------------------------------------------------
# Stage 7.3 data-health signals (roadmap 7.3, ADR-027)
# ---------------------------------------------------------------------------

P0P1_SEND_TARGET_SECONDS = 5 * 60
P0P1_PRIORITIES = (Notification.Priority.P0, Notification.Priority.P1)
# Residue rows: ``processing`` claims that were never finalized.  MVP does
# not auto-reclaim them (ADR-027 section 7); they must stay visible to the
# operator instead.
BACKLOG_STATUSES = (
    Notification.Status.PENDING,
    Notification.Status.PROCESSING,
    Notification.Status.FAILED,
)


@dataclass(frozen=True, slots=True)
class NotificationBacklogSnapshot:
    """Queued-notification state for the data-health page.

    Every count is a whole-table aggregate (no per-user filtering): this
    is an operator signal, not a user page.  Ages are in seconds and
    measured against ``now``; ``None`` means no rows in that bucket.
    """

    pending_total: int
    pending_p0p1: int
    oldest_pending_seconds: float | None
    oldest_pending_p0p1_seconds: float | None
    processing_total: int
    oldest_processing_seconds: float | None
    failed_total: int
    failed_p0p1: int
    oldest_failed_seconds: float | None


def notification_backlog_snapshot(*, now: datetime) -> NotificationBacklogSnapshot:
    """Aggregate pending / processing-residue / failed notification counts."""

    aggregate_rows = (
        Notification.objects.filter(status__in=BACKLOG_STATUSES)
        .values("status")
        .annotate(
            total=Count("id"),
            p0p1=Count("id", filter=Q(priority__in=P0P1_PRIORITIES)),
        )
    )
    aggregates = {row["status"]: (row["total"], row["p0p1"]) for row in aggregate_rows}
    oldest = dict(
        Notification.objects.filter(status__in=BACKLOG_STATUSES)
        .values("status")
        .annotate(oldest=Min("created_at"))
        .values_list("status", "oldest")
    )
    oldest_p0p1 = Notification.objects.filter(
        status=Notification.Status.PENDING, priority__in=P0P1_PRIORITIES
    ).aggregate(oldest=Min("created_at"))["oldest"]

    def age(value: datetime | None) -> float | None:
        if value is None:
            return None
        return (now - value).total_seconds()

    pending = aggregates.get(Notification.Status.PENDING, (0, 0))
    processing = aggregates.get(Notification.Status.PROCESSING, (0, 0))
    failed = aggregates.get(Notification.Status.FAILED, (0, 0))
    return NotificationBacklogSnapshot(
        pending_total=pending[0],
        pending_p0p1=pending[1],
        oldest_pending_seconds=age(oldest.get(Notification.Status.PENDING)),
        oldest_pending_p0p1_seconds=age(oldest_p0p1),
        processing_total=processing[0],
        oldest_processing_seconds=age(oldest.get(Notification.Status.PROCESSING)),
        failed_total=failed[0],
        failed_p0p1=failed[1],
        oldest_failed_seconds=age(oldest.get(Notification.Status.FAILED)),
    )


def measure_p0p1_send_freshness(
    *,
    now: datetime,
    lookback_hours: int = DEFAULT_FRESHNESS_LOOKBACK_HOURS,
    target_seconds: float = P0P1_SEND_TARGET_SECONDS,
) -> FreshnessMeasurement:
    """Measure P0/P1 notification send freshness (data-sources.md section 6).

    The unified formula is
    ``provider_accepted_at / in_app_created_at - domain_change_committed_at``.
    Notifications are enqueued inside the triggering domain transaction
    (ADR-025), so ``created_at`` proxies the domain-change commit time and
    the in-app visibility moment; ``sent_at`` is the accepted delivery
    (email) or the immediate close-out (in-app channel, ADR-027).  Rows
    still pending / processing / failed have no end endpoint yet and are
    counted as unmeasured; they are also visible in the backlog snapshot.
    """

    window_start = now - timedelta(hours=lookback_hours)
    in_window = Notification.objects.filter(
        priority__in=P0P1_PRIORITIES, created_at__gte=window_start
    )
    latencies: list[float] = []
    unmeasured = 0
    latest_sample_at: datetime | None = None
    for created_at, status, sent_at in in_window.values_list("created_at", "status", "sent_at"):
        if latest_sample_at is None or created_at > latest_sample_at:
            latest_sample_at = created_at
        if status != Notification.Status.SENT or sent_at is None:
            unmeasured += 1
            continue
        latencies.append((sent_at - created_at).total_seconds())
    return measure_latencies(
        latencies=latencies,
        unmeasured_count=unmeasured,
        target_seconds=target_seconds,
        latest_sample_at=latest_sample_at,
    )
