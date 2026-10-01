"""Read-only Company and SecurityListing selectors for Stage 4.3 pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from django.db.models import Q, QuerySet

from companies.models import Company, SecurityListing


@dataclass(frozen=True, slots=True)
class CompanyTickerResolution:
    company: Company | None
    listings: tuple[SecurityListing, ...]
    ambiguous: bool
    company_count: int


def get_companies(
    *,
    query: str | None = None,
    exchange: str | None = None,
    company_ids: tuple[UUID, ...] | None = None,
) -> QuerySet[Company]:
    queryset = Company.objects.all()
    if query:
        normalized_query = query.strip()
        if normalized_query:
            queryset = queryset.filter(
                Q(display_name__icontains=normalized_query)
                | Q(legal_name__icontains=normalized_query)
                | Q(security_listings__ticker__icontains=normalized_query)
            ).distinct()
    if exchange:
        normalized_exchange = exchange.strip().upper()
        if normalized_exchange:
            queryset = queryset.filter(security_listings__exchange=normalized_exchange).distinct()
    if company_ids is not None:
        queryset = queryset.filter(id__in=company_ids)
    return queryset.order_by("display_name", "id")


def company_listings(
    *,
    company_ids: tuple[UUID, ...],
    as_of: date,
) -> dict[UUID, tuple[SecurityListing, ...]]:
    """Return as-of effective listings grouped by Company in one query."""

    if not company_ids:
        return {}
    rows = (
        SecurityListing.objects.filter(
            company_id__in=company_ids,
            effective_from__lte=as_of,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=as_of))
        .order_by("company_id", "-is_primary", "-effective_from", "ticker", "id")
    )
    grouped: dict[UUID, list[SecurityListing]] = {}
    for listing in rows:
        grouped.setdefault(listing.company_id, []).append(listing)
    return {company_id: tuple(items) for company_id, items in grouped.items()}


def primary_listing(listings: tuple[SecurityListing, ...]) -> SecurityListing | None:
    for listing in listings:
        if listing.is_primary:
            return listing
    return listings[0] if listings else None


def resolve_company_by_ticker(
    *,
    ticker: str,
    as_of: date,
) -> CompanyTickerResolution:
    """Resolve a ticker only among listings effective at the requested date."""

    normalized = ticker.strip()
    if not normalized:
        return CompanyTickerResolution(
            company=None,
            listings=(),
            ambiguous=False,
            company_count=0,
        )
    listings = tuple(
        SecurityListing.objects.select_related("company")
        .filter(
            ticker__iexact=normalized,
            effective_from__lte=as_of,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=as_of))
        .order_by("company_id", "-is_primary", "-effective_from", "id")
    )
    company_ids = tuple(sorted({listing.company_id for listing in listings}, key=str))
    if not company_ids:
        return CompanyTickerResolution(
            company=None,
            listings=(),
            ambiguous=False,
            company_count=0,
        )
    if len(company_ids) > 1:
        return CompanyTickerResolution(
            company=None,
            listings=listings,
            ambiguous=True,
            company_count=len(company_ids),
        )
    company = Company.objects.get(pk=company_ids[0])
    return CompanyTickerResolution(
        company=company,
        listings=listings,
        ambiguous=False,
        company_count=1,
    )
