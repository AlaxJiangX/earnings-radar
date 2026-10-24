"""Daily digest bucket arithmetic in the user's local time (ADR-027 D2).

P3 (digest-priority) notifications are bucketed per user and local date.
The bucket recorded at enqueue time is the local date of the *next* digest
moment that has not yet arrived, so replays that cross local midnight within
the same pending window still converge on one row (idempotency key v2).

The user timezone comes from ``User.timezone``; when it is missing or not a
valid IANA name the digest falls back to the market timezone
(America/New_York) so bucketing stays deterministic — the same
single-deterministic-anchor philosophy as ADR-024.  All comparisons happen
on timezone-aware values; DST translation is delegated entirely to
``zoneinfo``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from earnings.presentation import MARKET_TIMEZONE
from notifications.models import DEFAULT_DIGEST_TIME, UserNotificationPreference

DIGEST_TIMEZONE_FALLBACK = MARKET_TIMEZONE


def user_digest_timezone(user: object) -> ZoneInfo:
    """Return the timezone a user's digest is bucketed in.

    An unset or invalid ``User.timezone`` falls back to the market timezone;
    invalid values never crash delivery (ADR-027 D2).
    """

    value = (getattr(user, "timezone", None) or "").strip()
    if value:
        try:
            return ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            return DIGEST_TIMEZONE_FALLBACK
    return DIGEST_TIMEZONE_FALLBACK


def effective_digest_time(
    preference: UserNotificationPreference | None,
) -> time:
    """Return the digest time a user's buckets use; NULL preference = 08:00."""

    if preference is None or preference.digest_time_local is None:
        return DEFAULT_DIGEST_TIME
    return preference.digest_time_local


def digest_due_at(bucket: date, *, digest_time: time, timezone: ZoneInfo) -> datetime:
    """Return the aware instant at which *bucket*'s digest becomes due."""

    return datetime.combine(bucket, digest_time, tzinfo=timezone)


def next_digest_bucket(now_utc: datetime, *, digest_time: time, timezone: ZoneInfo) -> date:
    """Return the local date of the next digest moment not yet arrived.

    Before the daily digest time that is today; at or after it (until
    midnight) that is tomorrow.  ``now_utc`` must be timezone-aware.
    """

    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware.")
    local_now = now_utc.astimezone(timezone)
    today = local_now.date()
    if local_now < digest_due_at(today, digest_time=digest_time, timezone=timezone):
        return today
    return today + timedelta(days=1)


def is_digest_bucket_due(
    now_utc: datetime, *, bucket: date, digest_time: time, timezone: ZoneInfo
) -> bool:
    """Return True when the bucket's digest moment has arrived."""

    return now_utc >= digest_due_at(bucket, digest_time=digest_time, timezone=timezone)
