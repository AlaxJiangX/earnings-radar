from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from django.db.models import Q, QuerySet

from indexes.models import NORMATIVE_MEMBERSHIP_STATUSES, IndexMembership

if TYPE_CHECKING:
    from indexes.models import MarketIndex


def get_normative_memberships_for_index(
    *,
    index: MarketIndex,
    as_of_date: date,
) -> QuerySet[IndexMembership]:
    return IndexMembership.objects.filter(
        index=index,
        status__in=NORMATIVE_MEMBERSHIP_STATUSES,
        effective_from__lte=as_of_date,
    ).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gt=as_of_date),
    )


def get_normative_memberships_for_listing(
    *,
    security_listing_id: UUID,
    as_of_date: date,
) -> QuerySet[IndexMembership]:
    return IndexMembership.objects.filter(
        security_listing_id=security_listing_id,
        status__in=NORMATIVE_MEMBERSHIP_STATUSES,
        effective_from__lte=as_of_date,
    ).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gt=as_of_date),
    )


def get_normative_memberships_in_period(
    *,
    index: MarketIndex,
    from_date: date,
    to_date: date,
) -> QuerySet[IndexMembership]:
    return IndexMembership.objects.filter(
        index=index,
        status__in=NORMATIVE_MEMBERSHIP_STATUSES,
        effective_from__lt=to_date,
    ).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gt=from_date),
    )
