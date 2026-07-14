from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from django.db.models import Q, QuerySet

from indexes.models import NORMATIVE_MEMBERSHIP_STATUSES, IndexMembership

if TYPE_CHECKING:
    from indexes.models import MarketIndex


_NORMATIVE_DATE_FILTER = (
    Q(status__in=NORMATIVE_MEMBERSHIP_STATUSES)
    & Q(effective_from__lte=date.today())
    & (Q(effective_to__isnull=True) | Q(effective_to__gt=date.today()))
)


def _normative_as_of(
    queryset: QuerySet[IndexMembership],
    as_of_date: date,
) -> QuerySet[IndexMembership]:
    return queryset.filter(
        status__in=NORMATIVE_MEMBERSHIP_STATUSES,
        effective_from__lte=as_of_date,
    ).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gt=as_of_date),
    )


def get_normative_memberships_for_index(
    *,
    index: MarketIndex,
    as_of_date: date,
) -> QuerySet[IndexMembership]:
    return _normative_as_of(
        IndexMembership.objects.filter(index=index),
        as_of_date,
    )


def get_normative_memberships_for_listing(
    *,
    security_listing_id: UUID,
    as_of_date: date,
) -> QuerySet[IndexMembership]:
    return _normative_as_of(
        IndexMembership.objects.filter(security_listing_id=security_listing_id),
        as_of_date,
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


def current_listing_indexes_as_of(
    *,
    security_listing_id: UUID,
    as_of_date: date,
    is_enabled: bool | None = True,
) -> QuerySet[IndexMembership]:
    """Return normative IndexMembership records for a listing as of a date.

    Returns one membership row per (index, security_listing) pair.

    When *is_enabled* is ``True``, only memberships whose index
    ``is_enabled=True`` are included.  When ``False``, only disabled-index
    memberships.  When ``None``, all normative memberships are returned
    regardless of index status.
    """
    qs = IndexMembership.objects.select_related("index").filter(
        security_listing_id=security_listing_id,
    )
    qs = _normative_as_of(qs, as_of_date)
    if is_enabled is True:
        qs = qs.filter(index__is_enabled=True)
    elif is_enabled is False:
        qs = qs.filter(index__is_enabled=False)
    return qs


def company_indexes_as_of(
    *,
    company_id: UUID,
    as_of_date: date,
    is_enabled: bool | None = True,
) -> QuerySet[IndexMembership]:
    """Return normative IndexMembership for all listings of a company as of a date.

    Only one membership row per (index, security_listing) pair is returned.
    """
    from companies.models import SecurityListing

    listing_ids = SecurityListing.objects.filter(company_id=company_id).values("id")
    qs = IndexMembership.objects.select_related("index").filter(
        security_listing_id__in=listing_ids,
    )
    qs = _normative_as_of(qs, as_of_date)
    if is_enabled is True:
        qs = qs.filter(index__is_enabled=True)
    elif is_enabled is False:
        qs = qs.filter(index__is_enabled=False)
    return qs.distinct()


def memberships_as_of(
    *,
    as_of_date: date,
    index_code: str | None = None,
) -> QuerySet[IndexMembership]:
    """Return all normative memberships as of a date, optionally filtered by index code.

    Returns one membership row per (index, security_listing) pair.
    """
    qs = IndexMembership.objects.select_related("index", "security_listing")
    qs = _normative_as_of(qs, as_of_date)
    if index_code is not None:
        qs = qs.filter(index__code=index_code.upper())
    return qs.order_by("index__code", "security_listing__ticker")


def current_memberships(
    *,
    as_of_date: date,
    is_enabled: bool | None = True,
) -> QuerySet[IndexMembership]:
    """Return all currently normative memberships as of a date.

    When *is_enabled* is ``True``, only memberships whose index
    ``is_enabled=True`` are included.  When ``False``, only disabled-index
    memberships.  When ``None``, all normative memberships are returned.
    """
    qs = IndexMembership.objects.select_related("index", "security_listing")
    qs = _normative_as_of(qs, as_of_date)
    if is_enabled is True:
        qs = qs.filter(index__is_enabled=True)
    elif is_enabled is False:
        qs = qs.filter(index__is_enabled=False)
    return qs.order_by("index__code", "security_listing__ticker")


def listing_indexes_as_of(
    *,
    security_listing_id: UUID,
    as_of_date: date,
    is_enabled: bool | None = True,
) -> QuerySet[MarketIndex]:
    """Return distinct MarketIndex objects for a listing as of a date.

    When *is_enabled* is ``True`` (default), only enabled indexes are
    returned.  When ``False``, only disabled indexes.  When ``None``, all
    indexes are returned regardless of enabled status.
    """
    from indexes.models import MarketIndex as MI

    membership_ids = _normative_as_of(
        IndexMembership.objects.filter(security_listing_id=security_listing_id),
        as_of_date,
    ).values_list("index_id", flat=True)
    qs = MI.objects.filter(id__in=membership_ids)
    if is_enabled is True:
        qs = qs.filter(is_enabled=True)
    elif is_enabled is False:
        qs = qs.filter(is_enabled=False)
    return qs.distinct()
