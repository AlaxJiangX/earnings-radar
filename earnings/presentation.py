"""Read-only presentation helpers for earnings pages.

The helpers never mutate data and never call providers.  Storage remains
UTC-aware; Eastern Time is always shown as the market reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from earnings.models import EarningsDatePrecision, EarningsEvent

MARKET_TIMEZONE = ZoneInfo("America/New_York")

WINDOW_CHOICES = (
    ("today", "Today"),
    ("week", "This week"),
    ("next30", "Next 30 days"),
)

STATUS_CHOICES = (
    ("scheduled_estimated", "Estimated"),
    ("scheduled_confirmed", "Confirmed"),
    ("released", "Released"),
    ("cancelled", "Cancelled"),
)

SESSION_CHOICES = (
    ("pre_market", "Pre-market"),
    ("after_market", "After-market"),
    ("during_market", "During market"),
    ("unknown", "Unknown"),
)


@dataclass(frozen=True, slots=True)
class EventTimingDisplay:
    label: str
    date_label: str
    time_label: str
    utc_label: str
    local_label: str
    data_utc: str
    precision: str
    session_label: str


@dataclass(frozen=True, slots=True)
class EventDisplay:
    event: EarningsEvent
    timing: EventTimingDisplay
    status_label: str
    identity_label: str
    source_label: str
    is_candidate: bool
    is_cancelled: bool


def business_date() -> date:
    """Return the current business date in the market timezone."""

    return timezone.localdate(timezone=MARKET_TIMEZONE)


def resolve_user_timezone(request: Any) -> ZoneInfo | None:
    """Return a validated user timezone when the account model provides one.

    The current User model has no persistent timezone field.  ``getattr`` keeps
    this forward-compatible without adding schema, and browser-local time is
    progressively rendered by the base template when no server-side value is
    available.
    """

    if not getattr(request.user, "is_authenticated", False):
        return None
    value = getattr(request.user, "timezone", None)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return ZoneInfo(value.strip())
    except ZoneInfoNotFoundError:
        return None


def window_bounds(as_of: date, window: str) -> tuple[date, date]:
    """Return deterministic inclusive bounds for a supported window."""

    if window == "today":
        return as_of, as_of
    if window == "week":
        monday = as_of - timedelta(days=as_of.weekday())
        return monday, monday + timedelta(days=6)
    if window == "next30":
        return as_of, as_of + timedelta(days=29)
    raise ValueError("Unsupported earnings window.")


def window_label(window: str, as_of: date) -> str:
    start, end = window_bounds(as_of, window)
    if window == "today":
        return f"Today ({start.isoformat()})"
    if window == "week":
        return f"This week ({start.isoformat()} to {end.isoformat()})"
    return f"Next 30 days ({start.isoformat()} to {end.isoformat()})"


def build_event_display(
    event: EarningsEvent,
    *,
    user_timezone: ZoneInfo | None = None,
) -> EventDisplay:
    return EventDisplay(
        event=event,
        timing=_event_timing(event, user_timezone=user_timezone),
        status_label=event.get_status_display(),
        identity_label=(
            "Candidate / Incomplete" if event.identity_status == "candidate" else "Canonical"
        ),
        source_label=_source_label(event),
        is_candidate=event.identity_status == "candidate",
        is_cancelled=event.status == "cancelled",
    )


def _event_timing(
    event: EarningsEvent,
    *,
    user_timezone: ZoneInfo | None,
) -> EventTimingDisplay:
    session_label = event.get_release_session_display()
    if event.status == "released" and event.earnings_release_precision != "unknown":
        prefix = "earnings_release"
        label = "Released"
    elif event.confirmed_release_precision != "unknown":
        prefix = "confirmed_release"
        label = "Confirmed"
    else:
        prefix = "estimated_release"
        label = "Estimated"

    precision = getattr(event, f"{prefix}_precision")
    if precision == EarningsDatePrecision.EXACT_DATETIME:
        value = getattr(event, f"{prefix}_at")
        if value is not None:
            return _exact_timing(
                value,
                label=label,
                session_label=session_label,
                user_timezone=user_timezone,
            )
    if precision == EarningsDatePrecision.DATE_ONLY:
        value_date = getattr(event, f"{prefix}_date")
        if value_date is not None:
            return EventTimingDisplay(
                label=label,
                date_label=value_date.strftime("%b %d, %Y"),
                time_label="",
                utc_label="",
                local_label="",
                data_utc="",
                precision="date_only",
                session_label=session_label,
            )
    if event.release_session != "unknown":
        return EventTimingDisplay(
            label=label,
            date_label="",
            time_label="",
            utc_label="",
            local_label="",
            data_utc="",
            precision="session_only",
            session_label=session_label,
        )
    return EventTimingDisplay(
        label=label,
        date_label="",
        time_label="",
        utc_label="",
        local_label="",
        data_utc="",
        precision="unknown",
        session_label=session_label,
    )


def _exact_timing(
    value: datetime,
    *,
    label: str,
    session_label: str,
    user_timezone: ZoneInfo | None,
) -> EventTimingDisplay:
    market_value = timezone.localtime(value, MARKET_TIMEZONE)
    utc_value = value.astimezone(UTC)
    local_label = ""
    if user_timezone is not None:
        local_value = timezone.localtime(value, user_timezone)
        local_label = local_value.strftime("%b %d, %Y %I:%M %p %Z").replace(" 0", " ")
    return EventTimingDisplay(
        label=label,
        date_label=market_value.strftime("%b %d, %Y"),
        time_label=market_value.strftime("%I:%M %p %Z").lstrip("0"),
        utc_label=utc_value.strftime("%H:%M UTC"),
        local_label=local_label,
        data_utc=value.isoformat(),
        precision="exact_datetime",
        session_label=session_label,
    )


def _source_label(event: EarningsEvent) -> str:
    evidence = event.source_evidence
    if evidence is None:
        return "No source evidence"
    source = evidence.raw_data_record.source
    official_label = "official" if source.is_official else "third-party"
    prefix = "Candidate source" if event.identity_status == "candidate" else "Source"
    return f"{prefix}: {source.name} ({official_label})"
