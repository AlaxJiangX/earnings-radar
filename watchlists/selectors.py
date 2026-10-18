from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from django.db.models import QuerySet

from accounts.models import User
from watchlists.models import WatchlistItem


def get_watchlist_items_for_user(user: User) -> QuerySet[WatchlistItem]:
    """Return only the requesting user's watchlist rows."""

    return WatchlistItem.objects.filter(user=user).select_related("company")


def get_active_watchlist_company_ids(
    *, company_ids: Iterable[UUID] | None = None
) -> tuple[UUID, ...]:
    """Return the canonical global Company set without exposing user ownership."""

    queryset = WatchlistItem.objects.filter(is_active=True)
    if company_ids is not None:
        normalized_ids = tuple(sorted({_require_uuid(value) for value in company_ids}, key=str))
        if not normalized_ids:
            return ()
        queryset = queryset.filter(company_id__in=normalized_ids)
    values = queryset.values_list("company_id", flat=True).distinct()
    return tuple(sorted(set(values), key=str))


def _require_uuid(value: UUID) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError("company_ids entries must be UUID values.")
    return value
