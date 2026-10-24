"""Stage 5.2 login-required composition pages: /dashboard and /watchlist.

This module is the project-level composition root.  The views only handle
HTTP, permissions, input parsing and rendering; every domain read goes
through public selectors and every mutation through ``watchlists.services``.
No cross-app dependency rule is relaxed here: ``config`` is not a domain app
and may import every app's public interface.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from accounts.models import User
from accounts.services import AccountServiceError, set_user_timezone
from companies.models import SecurityListing
from companies.selectors import (
    company_listings,
    primary_listing,
    resolve_company_by_ticker,
)
from earnings.models import EarningsDateChange, EarningsEvent
from earnings.presentation import (
    EventDisplay,
    build_event_display,
    business_date,
    resolve_user_timezone,
)
from earnings.selectors import (
    next_earnings_events_for_companies,
    recent_earnings_date_changes_for_companies,
    recent_released_earnings_for_companies,
    upcoming_earnings_events_for_companies,
)
from filings.models import Filing
from filings.selectors import recent_filings_for_companies
from indexes.models import IndexChangeEvent
from indexes.selectors import recent_index_changes_for_companies
from notifications.models import ReminderRule
from notifications.selectors import (
    notifications_for_user,
    resolve_company_names,
)
from notifications.services.settings_service import (
    NotificationSettingsError,
    get_or_create_notification_preference,
    mark_notification_read,
    set_digest_time_local,
    set_event_type_enabled,
    update_notification_preference,
)
from watchlists.models import WatchlistItem
from watchlists.selectors import get_watchlist_items_for_user
from watchlists.services import (
    WatchlistCompanyNotEligible,
    WatchlistServiceError,
    add_watchlist_item,
    remove_watchlist_item,
    set_watchlist_alerts,
    set_watchlist_priority,
)

SORT_CHOICES = (
    ("next_earnings", "Next earnings"),
    ("priority", "Reminder priority (S before A)"),
)
VALID_SORTS = frozenset(value for value, _label in SORT_CHOICES)
DEFAULT_SORT = "next_earnings"

DASHBOARD_SECTION_LIMIT = 10
UPCOMING_DAYS = 7

ERROR_MESSAGES = {
    "invalid_ticker": "Enter a non-empty ticker.",
    "company_not_found": "No company with an effective listing matches that ticker.",
    "ambiguous_ticker": "That ticker matches multiple companies and cannot be added by ticker.",
    "not_eligible": "The company has no effective listing today and cannot be added.",
    "item_not_found": "Watchlist item was not found.",
    "invalid_input": "The request was invalid.",
}
VALID_ERROR_CODES = frozenset(ERROR_MESSAGES)

# Sentinel later than any real earnings date; keeps companies without a next
# event at the bottom of both sort orders.
_FUTURE_SENTINEL = date.max


@dataclass(frozen=True, slots=True)
class WatchlistRow:
    item: WatchlistItem
    ticker: str
    exchange: str
    next_display: EventDisplay | None
    next_sort_date: date


@dataclass(frozen=True, slots=True)
class EventRow:
    display: EventDisplay
    ticker: str
    exchange: str


@dataclass(frozen=True, slots=True)
class DateChangeRow:
    change: EarningsDateChange
    summary: str


@dataclass(frozen=True, slots=True)
class FilingRow:
    filing: Filing
    summary: str


@dataclass(frozen=True, slots=True)
class IndexChangeRow:
    event: IndexChangeEvent
    summary: str


def _authenticated_user(request: HttpRequest) -> User:
    """Narrow ``request.user`` for mypy; ``@login_required`` guarantees a User."""

    user = request.user
    assert isinstance(user, User)
    return user


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    user = _authenticated_user(request)
    as_of = business_date()
    user_timezone = resolve_user_timezone(request)
    items = list(get_watchlist_items_for_user(user).filter(is_active=True))
    company_ids = tuple(item.company_id for item in items)
    next_events = next_earnings_events_for_companies(company_ids=company_ids, as_of=as_of)
    listings_by_company = company_listings(company_ids=company_ids, as_of=as_of)
    rows_by_priority: dict[str, list[WatchlistRow]] = {"important": [], "normal": []}
    for item in items:
        event = next_events.get(item.company_id)
        rows_by_priority[item.priority_level].append(
            _watchlist_row(item, event, listings_by_company, user_timezone)
        )

    upcoming = list(
        upcoming_earnings_events_for_companies(
            company_ids=company_ids, as_of=as_of, days=UPCOMING_DAYS
        )
    )
    date_changes = recent_earnings_date_changes_for_companies(
        company_ids=company_ids, limit=DASHBOARD_SECTION_LIMIT
    )
    releases = list(
        recent_released_earnings_for_companies(
            company_ids=company_ids, limit=DASHBOARD_SECTION_LIMIT
        )
    )
    filings = recent_filings_for_companies(company_ids=company_ids, limit=DASHBOARD_SECTION_LIMIT)
    index_changes = recent_index_changes_for_companies(
        company_ids=company_ids, limit=DASHBOARD_SECTION_LIMIT
    )

    window_end = as_of + timedelta(days=UPCOMING_DAYS - 1)
    context: dict[str, Any] = {
        "important_rows": _sorted_rows(rows_by_priority["important"]),
        "normal_rows": _sorted_rows(rows_by_priority["normal"]),
        "upcoming_rows": _event_rows(upcoming, listings_by_company, user_timezone),
        "date_change_rows": [
            DateChangeRow(change=change, summary=_date_change_summary(change))
            for change in date_changes
        ],
        "release_rows": _event_rows(releases, listings_by_company, user_timezone),
        "filing_rows": [
            FilingRow(filing=filing, summary=_filing_summary(filing)) for filing in filings
        ],
        "index_change_rows": [
            IndexChangeRow(event=event, summary=_index_change_summary(event))
            for event in index_changes
        ],
        "upcoming_window_label": f"{as_of.isoformat()} to {window_end.isoformat()}",
        "section_limit": DASHBOARD_SECTION_LIMIT,
        "market_timezone_label": "America/New_York (ET)",
    }
    return render(request, "pages/dashboard.html", context)


@login_required
def watchlist_page(request: HttpRequest) -> HttpResponse:
    sort = _parse_sort(request.GET.get("sort"))
    error_code = request.GET.get("error", "")
    context = _watchlist_context(request, sort=sort, error_code=error_code)
    if request.headers.get("HX-Request"):
        return render(request, "pages/_watchlist_results.html", context)
    return render(request, "pages/watchlist.html", context)


@login_required
def watchlist_add(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("watchlist")
    ticker = (request.POST.get("ticker") or "").strip()
    priority = (request.POST.get("priority_level") or "").strip().lower()
    sort = _parse_sort(request.POST.get("sort"))
    error_code = _add_company(request, ticker=ticker, priority=priority)
    if error_code is not None:
        return _action_failure(request, sort=sort, error_code=error_code)
    return _action_success(request, sort=sort)


@login_required
def watchlist_remove(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("watchlist")
    sort = _parse_sort(request.POST.get("sort"))
    item_id = _parse_uuid(request.POST.get("item_id"))
    if item_id is None:
        return _action_failure(request, sort=sort, error_code="invalid_input")
    try:
        remove_watchlist_item(
            user=_authenticated_user(request),
            item_id=item_id,
            as_of=business_date(),
            reason="watchlist-page-remove",
            request_id=uuid.uuid4().hex,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except WatchlistServiceError:
        # Foreign items must behave exactly like missing ones (ADR-023 §5.1).
        return _action_failure(request, sort=sort, error_code="item_not_found")
    return _action_success(request, sort=sort)


@login_required
def watchlist_priority(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("watchlist")
    sort = _parse_sort(request.POST.get("sort"))
    item_id = _parse_uuid(request.POST.get("item_id"))
    priority = (request.POST.get("priority_level") or "").strip().lower()
    if item_id is None or priority not in _VALID_PRIORITIES:
        return _action_failure(request, sort=sort, error_code="invalid_input")
    try:
        set_watchlist_priority(
            user=_authenticated_user(request),
            item_id=item_id,
            priority_level=priority,
            reason="watchlist-page-priority",
            request_id=uuid.uuid4().hex,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except WatchlistServiceError:
        return _action_failure(request, sort=sort, error_code="item_not_found")
    return _action_success(request, sort=sort)


@login_required
def watchlist_alerts(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("watchlist")
    sort = _parse_sort(request.POST.get("sort"))
    item_id = _parse_uuid(request.POST.get("item_id"))
    alerts_enabled = request.POST.get("alerts_enabled") == "on"
    if item_id is None:
        return _action_failure(request, sort=sort, error_code="invalid_input")
    try:
        set_watchlist_alerts(
            user=_authenticated_user(request),
            item_id=item_id,
            alerts_enabled=alerts_enabled,
            reason="watchlist-page-alerts",
            request_id=uuid.uuid4().hex,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except WatchlistServiceError:
        return _action_failure(request, sort=sort, error_code="item_not_found")
    return _action_success(request, sort=sort)


_VALID_PRIORITIES = frozenset(
    {WatchlistItem.PriorityLevel.NORMAL, WatchlistItem.PriorityLevel.IMPORTANT}
)


def _add_company(request: HttpRequest, *, ticker: str, priority: str) -> str | None:
    if not ticker:
        return "invalid_ticker"
    if priority not in _VALID_PRIORITIES:
        return "invalid_input"
    as_of = business_date()
    resolution = resolve_company_by_ticker(ticker=ticker, as_of=as_of)
    if resolution.ambiguous:
        return "ambiguous_ticker"
    if resolution.company is None:
        return "company_not_found"
    try:
        add_watchlist_item(
            user=_authenticated_user(request),
            company_id=resolution.company.pk,
            as_of=as_of,
            priority_level=priority,
            reason="watchlist-page-add",
            request_id=uuid.uuid4().hex,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except WatchlistCompanyNotEligible:
        return "not_eligible"
    except WatchlistServiceError:
        return "invalid_input"
    return None


def _watchlist_context(request: HttpRequest, *, sort: str, error_code: str = "") -> dict[str, Any]:
    user = _authenticated_user(request)
    as_of = business_date()
    user_timezone = resolve_user_timezone(request)
    items = list(get_watchlist_items_for_user(user).filter(is_active=True))
    company_ids = tuple(item.company_id for item in items)
    next_events = next_earnings_events_for_companies(company_ids=company_ids, as_of=as_of)
    listings_by_company = company_listings(company_ids=company_ids, as_of=as_of)
    rows = [
        _watchlist_row(item, next_events.get(item.company_id), listings_by_company, user_timezone)
        for item in items
    ]
    rows = _sorted_rows(rows, sort=sort)
    return {
        "rows": rows,
        "sort": sort,
        "sort_choices": SORT_CHOICES,
        "has_items": bool(rows),
        "error_code": error_code if error_code in VALID_ERROR_CODES else "",
        "error_message": ERROR_MESSAGES.get(error_code)
        if error_code in VALID_ERROR_CODES
        else None,
        "market_timezone_label": "America/New_York (ET)",
    }


def _watchlist_row(
    item: WatchlistItem,
    event: EarningsEvent | None,
    listings_by_company: dict[Any, tuple[SecurityListing, ...]],
    user_timezone: ZoneInfo | None,
) -> WatchlistRow:
    listing = primary_listing(listings_by_company.get(item.company_id, ()))
    return WatchlistRow(
        item=item,
        ticker=listing.ticker if listing is not None else "",
        exchange=listing.exchange if listing is not None else "",
        next_display=build_event_display(event, user_timezone=user_timezone)
        if event is not None
        else None,
        next_sort_date=_event_sort_date(event) if event is not None else _FUTURE_SENTINEL,
    )


def _sorted_rows(rows: list[WatchlistRow], *, sort: str = DEFAULT_SORT) -> list[WatchlistRow]:
    def next_earnings_key(row: WatchlistRow) -> tuple[int, date, str, str]:
        return (
            0 if row.next_sort_date != _FUTURE_SENTINEL else 1,
            row.next_sort_date,
            row.item.company.display_name,
            str(row.item.id),
        )

    def priority_key(row: WatchlistRow) -> tuple[int, int, date, str, str]:
        return (
            0 if row.item.priority_level == WatchlistItem.PriorityLevel.IMPORTANT else 1,
            *next_earnings_key(row),
        )

    if sort == "priority":
        return sorted(rows, key=priority_key)
    return sorted(rows, key=next_earnings_key)


def _event_rows(
    events: Sequence[EarningsEvent],
    listings_by_company: dict[Any, tuple[SecurityListing, ...]],
    user_timezone: ZoneInfo | None,
) -> list[EventRow]:
    rows: list[EventRow] = []
    for event in events:
        listing = primary_listing(listings_by_company.get(event.company_id, ()))
        rows.append(
            EventRow(
                display=build_event_display(event, user_timezone=user_timezone),
                ticker=listing.ticker if listing is not None else "",
                exchange=listing.exchange if listing is not None else "",
            )
        )
    return rows


def _event_sort_date(event: EarningsEvent) -> date:
    return (
        event.confirmed_release_date
        or event.earnings_release_date
        or event.estimated_release_date
        or _FUTURE_SENTINEL
    )


def _parse_sort(value: str | None) -> str:
    return value if value in VALID_SORTS else DEFAULT_SORT


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value or "")
    except ValueError:
        return None


def _watchlist_url(sort: str, error_code: str | None = None) -> str:
    query = {"sort": sort}
    if error_code is not None:
        query["error"] = error_code
    return f"{reverse('watchlist')}?{urlencode(query)}"


def _action_success(request: HttpRequest, *, sort: str) -> HttpResponse:
    if request.headers.get("HX-Request"):
        context = _watchlist_context(request, sort=sort)
        return render(request, "pages/_watchlist_results.html", context)
    return redirect(_watchlist_url(sort))


def _action_failure(request: HttpRequest, *, sort: str, error_code: str) -> HttpResponse:
    if request.headers.get("HX-Request"):
        context = _watchlist_context(request, sort=sort, error_code=error_code)
        return render(
            request,
            "pages/_watchlist_results.html",
            context,
            status=400,
        )
    return redirect(_watchlist_url(sort, error_code))


def _date_change_summary(change: EarningsDateChange) -> str:
    """Compact old -> new summary for an EarningsDateChange row."""

    def value(prefix: str) -> str:
        value_date: date | None = getattr(change, f"{prefix}_date")
        value_datetime: datetime | None = getattr(change, f"{prefix}_datetime")
        value_session: str | None = getattr(change, f"{prefix}_session")
        if value_date is not None:
            return value_date.isoformat()
        if value_datetime is not None:
            return value_datetime.strftime("%Y-%m-%d %H:%M UTC")
        if value_session:
            return value_session.replace("_", " ")
        return "unknown"

    return f"{value('old')} \N{RIGHTWARDS ARROW} {value('new')}"


def _filing_summary(filing: Filing) -> str:
    accepted = filing.accepted_at.strftime("%Y-%m-%d %H:%M UTC")
    return f"{filing.form_type} accepted {accepted}"


def _index_change_summary(event: IndexChangeEvent) -> str:
    legs = ", ".join(f"{leg.action} {leg.index.code}" for leg in event.legs.all())
    displacement = event.get_displacement_display()
    return f"{legs} ({displacement})" if legs else displacement


# ---------------------------------------------------------------------------
# Stage 6.3: in-app notifications and notification settings
# ---------------------------------------------------------------------------

NOTIFICATIONS_ERROR_MESSAGES = {
    "invalid_input": "The request was invalid.",
    "notification_not_found": "Notification was not found.",
    "invalid_settings": "The settings could not be saved; check the values.",
}
NOTIFICATIONS_VALID_ERRORS = frozenset(NOTIFICATIONS_ERROR_MESSAGES)

_SETTINGS_SAVED_NOTE = "Settings saved."

# Sent later than any unread row; keeps unread notifications on top.
_READ_SENTINEL = datetime.max.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class NotificationRow:
    notification: Any
    company_name: str
    event_type_label: str
    utc_label: str
    utc_iso: str
    local_label: str
    local_set: bool


def _notification_time_fields(user: User, value: datetime) -> tuple[str, str, str, bool]:
    utc_label = value.strftime("%Y-%m-%d %H:%M UTC")
    utc_iso = value.isoformat()
    timezone_value = (getattr(user, "timezone", None) or "").strip()
    if timezone_value:
        try:
            zone = ZoneInfo(timezone_value)
        except Exception:
            return utc_label, utc_iso, "", False
        local = value.astimezone(zone)
        return utc_label, utc_iso, local.strftime("%b %d, %Y %H:%M %Z"), True
    return utc_label, utc_iso, "", False


def _notifications_context(request: HttpRequest, *, error_code: str = "") -> dict[str, Any]:
    user = _authenticated_user(request)
    notifications = list(notifications_for_user(user))
    company_names = resolve_company_names(notifications)
    event_labels = dict(ReminderRule.EventType.choices)
    rows = []
    for notification in notifications:
        utc_label, utc_iso, local_label, local_set = _notification_time_fields(
            user, notification.created_at
        )
        rows.append(
            NotificationRow(
                notification=notification,
                company_name=company_names.get(notification.related_object_id, ""),
                event_type_label=event_labels.get(notification.event_type, notification.event_type),
                utc_label=utc_label,
                utc_iso=utc_iso,
                local_label=local_label,
                local_set=local_set,
            )
        )
    return {
        "rows": rows,
        "error_message": NOTIFICATIONS_ERROR_MESSAGES.get(error_code, ""),
        "saved_note": _SETTINGS_SAVED_NOTE if request.GET.get("saved") == "1" else "",
    }


@login_required
def notifications_page(request: HttpRequest) -> HttpResponse:
    return render(request, "pages/notifications.html", _notifications_context(request))


@login_required
def notification_read(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("notifications")
    notification_id = _parse_uuid(request.POST.get("notification_id"))
    if notification_id is None:
        return _notifications_action_failure(request, error_code="invalid_input")
    try:
        mark_notification_read(user=_authenticated_user(request), notification_id=notification_id)
    except NotificationSettingsError:
        # Foreign rows must behave exactly like missing ones.
        return _notifications_action_failure(request, error_code="notification_not_found")
    return _notifications_action_success(request)


def _notifications_action_success(request: HttpRequest) -> HttpResponse:
    if request.headers.get("HX-Request"):
        return render(request, "pages/_notification_rows.html", _notifications_context(request))
    return redirect("notifications")


def _notifications_action_failure(request: HttpRequest, *, error_code: str) -> HttpResponse:
    if request.headers.get("HX-Request"):
        return render(
            request,
            "pages/_notification_rows.html",
            _notifications_context(request, error_code=error_code),
            status=400,
        )
    return redirect(f"{reverse('notifications')}?error={error_code}")


def _event_type_enabled_state(user: User, event_type: str) -> bool:
    rules = ReminderRule.objects.filter(user=user, company__isnull=True, event_type=event_type)
    if not rules.exists():
        return True  # built-in default is enabled (ADR-024)
    return any(rule.is_enabled for rule in rules)


def _notification_settings_context(request: HttpRequest, *, error_code: str = "") -> dict[str, Any]:
    user = _authenticated_user(request)
    preference = get_or_create_notification_preference(user)
    event_types = [
        (event_type, label, _event_type_enabled_state(user, event_type))
        for event_type, label in ReminderRule.EventType.choices
    ]
    return {
        "preference": preference,
        "preference_lead_days": preference.default_lead_time_minutes // 1440,
        "digest_time_value": (
            preference.digest_time_local.strftime("%H:%M")
            if preference.digest_time_local is not None
            else ""
        ),
        "event_types": event_types,
        "timezone_value": (getattr(user, "timezone", None) or ""),
        "error_message": NOTIFICATIONS_ERROR_MESSAGES.get(error_code, ""),
        "saved_note": _SETTINGS_SAVED_NOTE if request.GET.get("saved") == "1" else "",
    }


@login_required
def notification_settings(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return render(
            request, "pages/notification_settings.html", _notification_settings_context(request)
        )
    user = _authenticated_user(request)
    try:
        lead_raw = (request.POST.get("default_lead_time_days") or "").strip()
        lead_days = int(lead_raw) if lead_raw else None
        update_notification_preference(
            user,
            email_enabled=request.POST.get("email_enabled") == "on",
            in_app_enabled=request.POST.get("in_app_enabled") == "on",
            default_lead_time_days=lead_days,
        )
        digest_raw = (request.POST.get("digest_time_local") or "").strip()
        digest_time = time.fromisoformat(digest_raw) if digest_raw else None
        set_digest_time_local(user, digest_time)
        for event_type, _label in ReminderRule.EventType.choices:
            enabled = request.POST.get(f"event_{event_type}") == "on"
            set_event_type_enabled(user, event_type, enabled)
        set_user_timezone(user, request.POST.get("timezone") or "")
    except (NotificationSettingsError, AccountServiceError, ValueError):
        return redirect(f"{reverse('notification_settings')}?error=invalid_settings")
    return redirect(f"{reverse('notification_settings')}?saved=1")
