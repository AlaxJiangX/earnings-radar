"""Timezone and DST boundary tests for the advance-reminder anchor.

ADR-024 anchors "one day before" to the America/New_York natural day.
These tests pin the anchor across both 2026 DST switches (spring forward
2026-03-08, fall back 2026-11-01), against UTC instants, and demonstrate
that a user's local date never shifts the anchor.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from notifications.services.lead_time import (
    is_advance_reminder_due,
    lead_time_in_days,
    market_date_from_utc,
    reminder_date_for_lead,
)

USER_TZ = ZoneInfo("Asia/Tokyo")


class TestReminderDateForLead:
    def test_one_day_before_release(self) -> None:
        assert reminder_date_for_lead(date(2026, 11, 5), 1440) == date(2026, 11, 4)

    def test_multi_day_leads(self) -> None:
        release = date(2026, 10, 30)
        assert reminder_date_for_lead(release, 2880) == date(2026, 10, 28)
        assert reminder_date_for_lead(release, 4320) == date(2026, 10, 27)

    def test_release_day_after_spring_forward_switch(self) -> None:
        # Release on the Monday after the 2026-03-08 switch; the one-day
        # reminder is the switch day itself (a nominal 23-hour day in ET).
        assert reminder_date_for_lead(date(2026, 3, 9), 1440) == date(2026, 3, 8)

    def test_release_day_after_fall_back_switch(self) -> None:
        # 2026-11-01 is the 25-hour day; a 11-02 release reminds on 11-01.
        assert reminder_date_for_lead(date(2026, 11, 2), 1440) == date(2026, 11, 1)

    @pytest.mark.parametrize("lead", [0, -1440, 720, 1441, 2160])
    def test_rejects_non_positive_or_fractional_day_leads(self, lead: int) -> None:
        with pytest.raises(ValueError):
            lead_time_in_days(lead)


class TestIsAdvanceReminderDue:
    def test_due_exactly_one_market_day_before(self) -> None:
        assert (
            is_advance_reminder_due(market_date=date(2026, 3, 8), release_date=date(2026, 3, 9))
            is True
        )

    def test_not_due_on_other_days(self) -> None:
        assert (
            is_advance_reminder_due(market_date=date(2026, 3, 7), release_date=date(2026, 3, 9))
            is False
        )
        assert (
            is_advance_reminder_due(market_date=date(2026, 3, 9), release_date=date(2026, 3, 9))
            is False
        )

    def test_multi_day_lead_due(self) -> None:
        assert (
            is_advance_reminder_due(
                market_date=date(2026, 11, 3),
                release_date=date(2026, 11, 5),
                lead_time_minutes=2880,
            )
            is True
        )


class TestMarketDateFromUtc:
    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            market_date_from_utc(datetime(2026, 3, 8, 4, 30))

    def test_spring_forward_2026_03_08(self) -> None:
        # ET midnight before the switch is 05:00 UTC (EST, UTC-5).
        assert market_date_from_utc(datetime(2026, 3, 8, 4, 59, tzinfo=UTC)) == date(2026, 3, 7)
        assert market_date_from_utc(datetime(2026, 3, 8, 5, 0, tzinfo=UTC)) == date(2026, 3, 8)
        # The 02:00->03:00 local jump happens at 07:00 UTC; the date is
        # 03-08 on both sides of the jump.
        assert market_date_from_utc(datetime(2026, 3, 8, 6, 59, tzinfo=UTC)) == date(2026, 3, 8)
        assert market_date_from_utc(datetime(2026, 3, 8, 7, 0, tzinfo=UTC)) == date(2026, 3, 8)

    def test_fall_back_2026_11_01(self) -> None:
        # ET midnight on 11-01 is 04:00 UTC (EDT, UTC-4) — the day before
        # Halloween ends at 03:59 UTC.
        assert market_date_from_utc(datetime(2026, 11, 1, 3, 59, tzinfo=UTC)) == date(2026, 10, 31)
        assert market_date_from_utc(datetime(2026, 11, 1, 4, 0, tzinfo=UTC)) == date(2026, 11, 1)
        # The 02:00->01:00 local repeat happens at 06:00 UTC; the same
        # local wall time exists twice with the same market date.
        assert market_date_from_utc(datetime(2026, 11, 1, 5, 30, tzinfo=UTC)) == date(2026, 11, 1)
        assert market_date_from_utc(datetime(2026, 11, 1, 6, 30, tzinfo=UTC)) == date(2026, 11, 1)

    def test_user_local_date_does_not_shift_the_anchor(self) -> None:
        # 2026-11-05 01:00 UTC is 2026-11-04 20:00 in New York (the evening
        # before an after-market release on 11-05) but already 2026-11-05
        # 10:00 for a Tokyo user.  The reminder anchor stays on market date.
        instant = datetime(2026, 11, 5, 1, 0, tzinfo=UTC)

        assert instant.astimezone(USER_TZ).date() == date(2026, 11, 5)
        market_date = market_date_from_utc(instant)
        assert market_date == date(2026, 11, 4)
        assert is_advance_reminder_due(market_date=market_date, release_date=date(2026, 11, 5))
