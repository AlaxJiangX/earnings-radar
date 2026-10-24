"""Read-only notification queries for pages and other modules."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import TYPE_CHECKING

from django.db.models import QuerySet

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
