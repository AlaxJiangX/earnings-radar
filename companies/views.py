"""Read-only public Company list and detail pages for Stage 4.3."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render

from companies.models import Company, SecurityListing
from companies.selectors import (
    company_listings,
    get_companies,
    primary_listing,
    resolve_company_by_ticker,
)
from earnings.presentation import (
    EventDisplay,
    build_event_display,
    business_date,
    resolve_user_timezone,
)
from earnings.selectors import (
    earnings_history,
    next_earnings_event,
    next_earnings_events_for_companies,
)
from indexes.models import ALLOWED_CODES, IndexMembership
from indexes.selectors import memberships_as_of, memberships_for_companies

DEFAULT_PAGE_SIZE = 25


@dataclass(frozen=True, slots=True)
class CompanyListRow:
    company: Company
    listing: SecurityListing | None
    index_codes: tuple[str, ...]
    next_display: EventDisplay | None


def company_list(request: HttpRequest) -> HttpResponse:
    as_of = business_date()
    user_timezone = resolve_user_timezone(request)
    params = _parse_company_list_params(request)

    company_ids: tuple[Any, ...] | None = None
    if params["index"] is not None:
        company_ids = tuple(
            memberships_as_of(
                as_of_date=as_of,
                index_code=params["index"],
            )
            .values_list("security_listing__company_id", flat=True)
            .distinct()
        )

    queryset = get_companies(
        query=params["q"],
        exchange=params["exchange"],
        company_ids=company_ids,
    )
    paginator = Paginator(queryset, DEFAULT_PAGE_SIZE, orphans=2)
    page = paginator.get_page(params["page"])
    page_company_ids = tuple(company.pk for company in page)

    listings_by_company = company_listings(company_ids=page_company_ids, as_of=as_of)
    memberships_by_company = _memberships_by_company(
        company_ids=page_company_ids,
        as_of=as_of,
    )
    next_events = next_earnings_events_for_companies(
        company_ids=page_company_ids,
        as_of=as_of,
        include_candidates=request.user.is_authenticated,
    )
    rows = [
        CompanyListRow(
            company=company,
            listing=primary_listing(listings_by_company.get(company.pk, ())),
            index_codes=tuple(
                sorted(
                    {
                        membership.index.code
                        for membership in memberships_by_company.get(company.pk, ())
                    }
                )
            ),
            next_display=(
                build_event_display(
                    next_events[company.pk],
                    user_timezone=user_timezone,
                )
                if company.pk in next_events
                else None
            ),
        )
        for company in page
    ]

    context: dict[str, Any] = {
        "rows": rows,
        "page_obj": page,
        "q": params["q"],
        "exchange": params["exchange"],
        "index": params["index"],
        "index_choices": sorted(ALLOWED_CODES),
        "has_results": page.paginator.count > 0,
        "filters_active": bool(params["q"] or params["exchange"] or params["index"]),
        "query_string": _company_list_query_string(params),
        "include_candidates": request.user.is_authenticated,
    }
    if request.headers.get("HX-Request"):
        return render(request, "companies/_company_list.html", context)
    return render(request, "companies/company_list.html", context)


def company_detail(request: HttpRequest, ticker: str) -> HttpResponse:
    as_of = business_date()
    user_timezone = resolve_user_timezone(request)
    resolution = resolve_company_by_ticker(ticker=ticker, as_of=as_of)
    if resolution.company is None:
        if resolution.ambiguous:
            raise Http404("This ticker is ambiguous for the requested date.")
        raise Http404("Company not found for the requested ticker.")

    company = resolution.company
    listings = company_listings(company_ids=(company.pk,), as_of=as_of).get(
        company.pk,
        (),
    )
    memberships = tuple(
        memberships_for_companies(
            company_ids=(company.pk,),
            as_of_date=as_of,
            is_enabled=True,
        )
    )
    next_event = next_earnings_event(
        company_id=company.pk,
        as_of=as_of,
        include_candidates=request.user.is_authenticated,
    )
    history_queryset = earnings_history(
        company_id=company.pk,
        include_candidates=request.user.is_authenticated,
    )
    history_paginator = Paginator(history_queryset, DEFAULT_PAGE_SIZE, orphans=2)
    history_page = history_paginator.get_page(_parse_page(request.GET.get("history_page", "1")))
    history_rows = [
        build_event_display(event, user_timezone=user_timezone) for event in history_page
    ]

    context: dict[str, Any] = {
        "company": company,
        "listings": listings,
        "primary_listing": primary_listing(listings),
        "memberships": memberships,
        "next_display": (
            build_event_display(next_event, user_timezone=user_timezone)
            if next_event is not None
            else None
        ),
        "history_rows": history_rows,
        "history_page_obj": history_page,
        "history_query_string": "",
        "include_candidates": request.user.is_authenticated,
        "market_timezone_label": "America/New_York (ET)",
    }
    return render(request, "companies/company_detail.html", context)


def _parse_company_list_params(request: HttpRequest) -> dict[str, Any]:
    query = request.GET
    q = query.get("q", "").strip()
    exchange = query.get("exchange", "").strip().upper()
    index = query.get("index", "").strip().upper()
    return {
        "q": q,
        "exchange": exchange or None,
        "index": index if index in ALLOWED_CODES else None,
        "page": _parse_page(query.get("page", "1")),
    }


def _parse_page(value: str) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return 1
    return page if page >= 1 else 1


def _company_list_query_string(params: dict[str, Any]) -> str:
    values = {
        "q": params["q"],
        "exchange": params["exchange"] or "",
        "index": params["index"] or "",
    }
    return urlencode({key: value for key, value in values.items() if value})


def _memberships_by_company(
    *,
    company_ids: tuple[Any, ...],
    as_of: date,
) -> dict[Any, tuple[IndexMembership, ...]]:
    grouped: dict[Any, list[IndexMembership]] = {}
    for membership in memberships_for_companies(
        company_ids=company_ids,
        as_of_date=as_of,
        is_enabled=True,
    ):
        grouped.setdefault(membership.security_listing.company_id, []).append(membership)
    return {company_id: tuple(items) for company_id, items in grouped.items()}
