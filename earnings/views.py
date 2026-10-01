"""Read-only public Stage 4.3 earnings pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from companies.selectors import company_listings, primary_listing
from earnings.presentation import (
    SESSION_CHOICES,
    STATUS_CHOICES,
    WINDOW_CHOICES,
    EventDisplay,
    build_event_display,
    business_date,
    resolve_user_timezone,
    window_label,
)
from earnings.selectors import get_earnings_events
from indexes.models import ALLOWED_CODES

DEFAULT_PAGE_SIZE = 25
DEFAULT_WINDOW = "next30"
VALID_WINDOWS = frozenset(value for value, _label in WINDOW_CHOICES)
VALID_STATUSES = frozenset(value for value, _label in STATUS_CHOICES)
VALID_SESSIONS = frozenset(value for value, _label in SESSION_CHOICES)


@dataclass(frozen=True, slots=True)
class EarningsListRow:
    display: EventDisplay
    ticker: str
    exchange: str


def earnings_list(request: HttpRequest) -> HttpResponse:
    as_of = business_date()
    params = _parse_query_params(request)
    user_timezone = resolve_user_timezone(request)
    queryset = get_earnings_events(
        as_of=as_of,
        window=params["window"],
        status=params["status"],
        session=params["session"],
        index_code=params["index"],
        include_candidates=request.user.is_authenticated,
    )
    paginator = Paginator(queryset, DEFAULT_PAGE_SIZE, orphans=2)
    page = paginator.get_page(params["page"])

    company_ids = tuple(event.company_id for event in page)
    listings_by_company = company_listings(company_ids=company_ids, as_of=as_of)
    rows: list[EarningsListRow] = []
    for event in page:
        listing = primary_listing(listings_by_company.get(event.company_id, ()))
        rows.append(
            EarningsListRow(
                display=build_event_display(event, user_timezone=user_timezone),
                ticker=listing.ticker if listing is not None else "",
                exchange=listing.exchange if listing is not None else "",
            )
        )

    context: dict[str, Any] = {
        "rows": rows,
        "page_obj": page,
        "window": params["window"],
        "window_label": window_label(params["window"], as_of),
        "status": params["status"],
        "session": params["session"],
        "index": params["index"],
        "window_choices": WINDOW_CHOICES,
        "status_choices": STATUS_CHOICES,
        "session_choices": SESSION_CHOICES,
        "index_choices": sorted(ALLOWED_CODES),
        "has_results": page.paginator.count > 0,
        "filters_active": bool(params["status"] or params["session"] or params["index"]),
        "query_string": _query_string(params),
        "include_candidates": request.user.is_authenticated,
        "market_timezone_label": "America/New_York (ET)",
    }
    if request.headers.get("HX-Request"):
        return render(request, "earnings/_earnings_list.html", context)
    return render(request, "earnings/earnings_list.html", context)


def _parse_query_params(request: HttpRequest) -> dict[str, Any]:
    query = request.GET
    window = query.get("window", DEFAULT_WINDOW)
    if window not in VALID_WINDOWS:
        window = DEFAULT_WINDOW
    status = query.get("status", "")
    normalized_status = status if status in VALID_STATUSES else None
    session = query.get("session", "")
    normalized_session = session if session in VALID_SESSIONS else None
    index = query.get("index", "").strip().upper()
    normalized_index = index if index in ALLOWED_CODES else None
    page = _parse_page(query.get("page", "1"))
    return {
        "window": window,
        "status": normalized_status,
        "session": normalized_session,
        "index": normalized_index,
        "page": page,
    }


def _parse_page(value: str) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return 1
    return page if page >= 1 else 1


def _query_string(params: dict[str, Any]) -> str:
    values = {
        "window": params["window"],
        "status": params["status"] or "",
        "session": params["session"] or "",
        "index": params["index"] or "",
    }
    return urlencode({key: value for key, value in values.items() if value})
