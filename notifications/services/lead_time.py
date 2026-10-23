"""Advance-reminder lead-time arithmetic anchored to the market timezone.

ADR-024 fixes the "one day before" semantics: the reminder date is the
calendar day immediately before the earnings business date, where the
business date is the natural day in ``America/New_York`` — the same anchor
as ``earnings.presentation.business_date``.  "One day" is a nominal
calendar day (it may be 23 or 25 elapsed hours across a DST switch, as in
RFC 5545 duration triggers), never a fixed ``timedelta(hours=24)`` offset
on absolute timestamps.

User timezones never enter this module: they only affect display and the
delivery window, which is a Stage 6.4 concern.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from earnings.presentation import MARKET_TIMEZONE
from notifications.models import LEAD_TIME_ONE_DAY_MINUTES

_MINUTES_PER_DAY = 1440


def market_date_from_utc(now_utc: datetime) -> date:
    """Return the current market-timezone natural date for a UTC instant.

    The input must be timezone-aware; DST translation is handled entirely by
    ``zoneinfo``.
    """

    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware.")
    return now_utc.astimezone(MARKET_TIMEZONE).date()


def reminder_date_for_lead(release_date: date, lead_time_minutes: int) -> date:
    """Return the reminder date for a whole-day lead before *release_date*."""

    days = lead_time_in_days(lead_time_minutes)
    return release_date - timedelta(days=days)


def is_advance_reminder_due(
    *,
    market_date: date,
    release_date: date,
    lead_time_minutes: int = LEAD_TIME_ONE_DAY_MINUTES,
) -> bool:
    """Return True when the advance reminder falls on *market_date*.

    *market_date* must already be expressed in the market timezone (use
    :func:`market_date_from_utc`); this function is pure date arithmetic so
    the DST boundaries of the anchor cannot leak in.
    """

    return reminder_date_for_lead(release_date, lead_time_minutes) == market_date


def lead_time_in_days(lead_time_minutes: int) -> int:
    """Validate a whole-day lead and return its day count.

    MVP leads are whole market-calendar days only; fractional-day leads are
    rejected rather than silently rounded (ADR-024).
    """

    if lead_time_minutes <= 0:
        raise ValueError("lead_time_minutes must be positive.")
    if lead_time_minutes % _MINUTES_PER_DAY != 0:
        raise ValueError("lead_time_minutes must be a whole number of days.")
    return lead_time_minutes // _MINUTES_PER_DAY
