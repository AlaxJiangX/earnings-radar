"""Digest bucket arithmetic tests (ADR-027 D2): pure functions, no database.

Covers the next-bucket rule (before / after the daily digest time), the
user-timezone fallback, the default digest time, and both US DST switch
dates — bucketing must stay deterministic on 2026-03-08 (spring forward)
and 2026-11-01 (fall back), where 08:00 local exists and is unambiguous.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from notifications.models import DEFAULT_DIGEST_TIME as MODEL_DEFAULT_DIGEST_TIME
from notifications.services.digest import (
    digest_due_at,
    effective_digest_time,
    is_digest_bucket_due,
    next_digest_bucket,
    user_digest_timezone,
)

NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
EIGHT = time(8, 0)


class TestUserTimezone:
    def test_valid_timezone(self) -> None:
        class _User:
            timezone = "Asia/Tokyo"

        assert user_digest_timezone(_User()) is TOKYO

    def test_missing_timezone_falls_back_to_market(self) -> None:
        class _User:
            timezone = None

        assert user_digest_timezone(_User()) is NEW_YORK

    def test_invalid_timezone_falls_back_to_market(self) -> None:
        class _User:
            timezone = "Not/A-Zone"

        assert user_digest_timezone(_User()) is NEW_YORK


class TestEffectiveDigestTime:
    def test_none_preference_uses_default(self) -> None:
        assert effective_digest_time(None) == MODEL_DEFAULT_DIGEST_TIME == EIGHT

    def test_null_column_uses_default(self) -> None:
        class _Preference:
            digest_time_local = None

        assert effective_digest_time(_Preference()) == EIGHT  # type: ignore[arg-type]

    def test_configured_time_wins(self) -> None:
        class _Preference:
            digest_time_local = time(18, 30)

        assert effective_digest_time(_Preference()) == time(18, 30)  # type: ignore[arg-type]


class TestNextBucket:
    def test_before_digest_time_is_today(self) -> None:
        now = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)  # 02:00 EDT
        assert next_digest_bucket(now, digest_time=EIGHT, timezone=NEW_YORK) == date(2026, 10, 26)

    def test_after_digest_time_is_tomorrow(self) -> None:
        now = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)  # 09:00 EDT
        assert next_digest_bucket(now, digest_time=EIGHT, timezone=NEW_YORK) == date(2026, 10, 27)

    def test_exactly_at_digest_time_is_tomorrow(self) -> None:
        now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)  # 08:00 EDT
        assert next_digest_bucket(now, digest_time=EIGHT, timezone=NEW_YORK) == date(2026, 10, 27)

    def test_tokyo_bucket_is_a_local_date(self) -> None:
        # 2026-10-26 20:00 UTC is already 2026-10-27 05:00 in Tokyo.
        now = datetime(2026, 10, 26, 20, 0, tzinfo=UTC)
        assert next_digest_bucket(now, digest_time=EIGHT, timezone=TOKYO) == date(2026, 10, 27)

    def test_tokyo_before_local_digest_time_same_day(self) -> None:
        # 2026-10-26 22:30 UTC is 2026-10-27 07:30 JST, still before 08:00.
        now = datetime(2026, 10, 26, 22, 30, tzinfo=UTC)
        assert next_digest_bucket(now, digest_time=EIGHT, timezone=TOKYO) == date(2026, 10, 27)

    def test_rejects_naive_now(self) -> None:
        try:
            next_digest_bucket(datetime(2026, 10, 26, 6, 0), digest_time=EIGHT, timezone=NEW_YORK)
        except ValueError:
            pass
        else:
            raise AssertionError("expected naive-datetime rejection")


class TestDSTBoundaries:
    def test_spring_forward_due_instant_uses_edt(self) -> None:
        # 2026-03-08 08:00 America/New_York is EDT (UTC-4) = 12:00 UTC.
        due = digest_due_at(date(2026, 3, 8), digest_time=EIGHT, timezone=NEW_YORK)
        assert due == datetime(2026, 3, 8, 12, 0, tzinfo=UTC)

    def test_fall_back_due_instant_uses_est(self) -> None:
        # 2026-11-01 08:00 America/New_York is EST (UTC-5) = 13:00 UTC.
        due = digest_due_at(date(2026, 11, 1), digest_time=EIGHT, timezone=NEW_YORK)
        assert due == datetime(2026, 11, 1, 13, 0, tzinfo=UTC)

    def test_bucket_around_spring_forward(self) -> None:
        before_switch = datetime(2026, 3, 7, 20, 0, tzinfo=UTC)  # 15:00 EST
        after_switch = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)  # 00:00 EST/EDT eve
        assert next_digest_bucket(before_switch, digest_time=EIGHT, timezone=NEW_YORK) == date(
            2026, 3, 8
        )
        assert next_digest_bucket(after_switch, digest_time=EIGHT, timezone=NEW_YORK) == date(
            2026, 3, 8
        )

    def test_due_around_fall_back(self) -> None:
        not_yet = datetime(2026, 11, 1, 12, 59, tzinfo=UTC)
        arrived = datetime(2026, 11, 1, 13, 0, tzinfo=UTC)
        bucket = date(2026, 11, 1)
        assert not is_digest_bucket_due(
            not_yet, bucket=bucket, digest_time=EIGHT, timezone=NEW_YORK
        )
        assert is_digest_bucket_due(arrived, bucket=bucket, digest_time=EIGHT, timezone=NEW_YORK)


class TestIsDue:
    def test_future_bucket_is_not_due(self) -> None:
        now = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)
        assert not is_digest_bucket_due(
            now, bucket=date(2026, 10, 27), digest_time=EIGHT, timezone=NEW_YORK
        )

    def test_past_bucket_stays_due_for_days(self) -> None:
        now = datetime(2026, 10, 30, 13, 0, tzinfo=UTC)
        assert is_digest_bucket_due(
            now, bucket=date(2026, 10, 26), digest_time=EIGHT, timezone=NEW_YORK
        )

    def test_due_at_exact_instant(self) -> None:
        now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)  # exactly 08:00 EDT
        assert is_digest_bucket_due(
            now, bucket=date(2026, 10, 26), digest_time=EIGHT, timezone=NEW_YORK
        )

    def test_one_minute_before(self) -> None:
        now = datetime(2026, 10, 26, 11, 59, tzinfo=UTC)
        assert not is_digest_bucket_due(
            now, bucket=date(2026, 10, 26), digest_time=EIGHT, timezone=NEW_YORK
        )

    def test_custom_digest_time(self) -> None:
        now = datetime(2026, 10, 26, 21, 30, tzinfo=UTC)  # 17:30 EDT
        assert is_digest_bucket_due(
            now, bucket=date(2026, 10, 26), digest_time=time(17, 30), timezone=NEW_YORK
        )

    def test_bucket_and_interval_arithmetic(self) -> None:
        # Buckets are nominal local dates, never fixed 24h offsets: across
        # the fall-back weekend the 08:00 instants are 25 hours apart in UTC.
        bucket = date(2026, 11, 1)
        due = digest_due_at(bucket, digest_time=EIGHT, timezone=NEW_YORK)
        previous = digest_due_at(bucket - timedelta(days=1), digest_time=EIGHT, timezone=NEW_YORK)
        assert due.astimezone(UTC) - previous.astimezone(UTC) == timedelta(hours=25)
