from __future__ import annotations

from datetime import date
from typing import Any

from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from indexes.models import ALLOWED_CHANGE_LEG_ACTIONS, ALLOWED_DISPLACEMENTS
from indexes.selectors import get_change_events

DEFAULT_PAGE_SIZE = 25
VALID_INDEX_CODES = frozenset({"SP500", "NASDAQ100", "DJIA", "RUSSELL2000"})


def index_changes(request: HttpRequest) -> HttpResponse:
    """Public page: index change events with filtering, pagination, and HTMX support."""
    params = _parse_query_params(request)
    today = date.today()

    qs = get_change_events(
        displacement=params["displacement"],
        monitoring_impact=params["monitoring_impact"],
        index_code=params["index"],
        action=params["action"],
        effective_from=params["from_date"],
        effective_to=params["to_date"],
        include_cancelled_corrected=params["include_cancelled_corrected"],
    )

    paginator = Paginator(qs, DEFAULT_PAGE_SIZE, orphans=2)
    page = paginator.get_page(params["page"])

    context = {
        "events": page,
        "today": today,
        "filters": params,
        "index_choices": sorted(VALID_INDEX_CODES),
        "displacement_choices": sorted(ALLOWED_DISPLACEMENTS),
        "action_choices": sorted(ALLOWED_CHANGE_LEG_ACTIONS),
        "has_results": page.paginator.count > 0,
    }

    if request.headers.get("HX-Request"):
        return render(request, "indexes/_index_changes_list.html", context)

    return render(request, "indexes/index_changes.html", context)


def _parse_query_params(request: HttpRequest) -> dict[str, Any]:
    """Parse and validate GET query parameters for the index changes page."""
    q = request.GET

    displacement = q.get("displacement", "")
    displacement_val = displacement if displacement in ALLOWED_DISPLACEMENTS else None

    monitoring_impact = q.get("impact", "")
    impact_val = monitoring_impact if monitoring_impact else None

    index_code = q.get("index", "")
    index_val = index_code.upper() if index_code.upper() in VALID_INDEX_CODES else None

    action = q.get("action", "")
    action_val = action if action in ALLOWED_CHANGE_LEG_ACTIONS else None

    include_cancelled_corrected = q.get("history", "").lower() in {"1", "true", "yes", "all"}

    from_date = _parse_date(q.get("from", ""))
    to_date = _parse_date(q.get("to", ""))

    page = _parse_page(q.get("page", "1"))

    return {
        "displacement": displacement_val,
        "monitoring_impact": impact_val,
        "index": index_val,
        "action": action_val,
        "from_date": from_date,
        "to_date": to_date,
        "include_cancelled_corrected": include_cancelled_corrected,
        "page": page,
    }


def _parse_date(value: str) -> date | None:
    """Parse an ISO date string, returning None on invalid input."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, TypeError):
        return None


def _parse_page(value: str) -> int:
    """Parse a page number, returning 1 on invalid input."""
    try:
        page = int(value)
        return page if page >= 1 else 1
    except (ValueError, TypeError):
        return 1
