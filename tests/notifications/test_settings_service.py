"""Stage 6.3 service-level boundary tests."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from accounts.models import User
from accounts.services import AccountServiceError, set_user_timezone
from notifications.models import Notification, ReminderRule, UserNotificationPreference
from notifications.services.settings_service import (
    NotificationNotFound,
    NotificationSettingsError,
    mark_notification_read,
    set_event_type_enabled,
    update_notification_preference,
)

pytestmark = pytest.mark.django_db

RELATED = Notification.RelatedObjectType.FILING


def _notification(user: User, **overrides: object) -> Notification:
    values: dict[str, object] = {
        "user": user,
        "event_type": ReminderRule.EventType.FILING,
        "related_object_type": RELATED,
        "related_object_id": uuid.uuid4(),
        "event_version": "v",
        "priority": "P1",
        "channel": ReminderRule.Channel.IN_APP,
        "idempotency_key": uuid.uuid4().hex,
    }
    values.update(overrides)
    return Notification.objects.create(**values)


class TestMarkNotificationRead:
    def test_rejects_naive_read_at(self, user: User) -> None:
        notification = _notification(user)

        with pytest.raises(NotificationSettingsError, match="timezone-aware"):
            mark_notification_read(
                user=user,
                notification_id=notification.pk,
                read_at=datetime(2026, 10, 24, 12, 0),
            )

        notification.refresh_from_db()
        assert notification.read_at is None

    def test_missing_notification_raises_not_found(self, user: User) -> None:
        with pytest.raises(NotificationNotFound):
            mark_notification_read(user=user, notification_id=uuid.uuid4())


class TestEventTypeToggle:
    def test_upcoming_toggle_uses_preference_lead(self, user: User) -> None:
        update_notification_preference(user, default_lead_time_days=3)

        set_event_type_enabled(user, ReminderRule.EventType.EARNINGS_UPCOMING, False)

        rules = ReminderRule.objects.filter(
            user=user, event_type=ReminderRule.EventType.EARNINGS_UPCOMING
        )
        assert {(rule.channel, rule.lead_time_minutes) for rule in rules} == {
            ("email", 4320),
            ("in_app", 4320),
        }
        assert all(rule.is_enabled is False for rule in rules)

    def test_non_upcoming_rules_have_zero_lead(self, user: User) -> None:
        set_event_type_enabled(user, ReminderRule.EventType.INDEX_REMOVED, True)

        rules = ReminderRule.objects.filter(
            user=user, event_type=ReminderRule.EventType.INDEX_REMOVED
        )
        assert {(rule.channel, rule.lead_time_minutes) for rule in rules} == {
            ("email", 0),
            ("in_app", 0),
        }
        assert all(rule.is_enabled is True for rule in rules)

    def test_company_rules_are_untouched(self, user: User) -> None:
        from companies.models import Company

        company = Company.objects.create(legal_name="Toggle Co", display_name="Toggle Co")
        ReminderRule.objects.create(
            user=user,
            company=company,
            event_type=ReminderRule.EventType.FILING,
            channel=ReminderRule.Channel.EMAIL,
            lead_time_minutes=0,
            is_enabled=True,
        )

        set_event_type_enabled(user, ReminderRule.EventType.FILING, False)

        company_rule = ReminderRule.objects.get(user=user, company=company)
        assert company_rule.is_enabled is True

    def test_lead_change_removes_stale_enabled_row(self, user: User) -> None:
        # P2: save with default lead 1, then change the preference to 2
        # days and save again; the old 1440 row must not survive.
        update_notification_preference(user, default_lead_time_days=1)
        set_event_type_enabled(user, ReminderRule.EventType.EARNINGS_UPCOMING, True)
        update_notification_preference(user, default_lead_time_days=2)
        set_event_type_enabled(user, ReminderRule.EventType.EARNINGS_UPCOMING, True)

        rules = ReminderRule.objects.filter(
            user=user, event_type=ReminderRule.EventType.EARNINGS_UPCOMING
        )
        assert {(rule.channel, rule.lead_time_minutes) for rule in rules} == {
            ("email", 2880),
            ("in_app", 2880),
        }
        assert all(rule.is_enabled is True for rule in rules)

    def test_disable_normalizes_preexisting_dirty_rows(self, user: User) -> None:
        update_notification_preference(user, default_lead_time_days=2)
        for lead in (1440, 2880, 4320):
            ReminderRule.objects.create(
                user=user,
                event_type=ReminderRule.EventType.EARNINGS_UPCOMING,
                channel=ReminderRule.Channel.EMAIL,
                lead_time_minutes=lead,
                is_enabled=True,
            )

        set_event_type_enabled(user, ReminderRule.EventType.EARNINGS_UPCOMING, False)

        rules = ReminderRule.objects.filter(
            user=user, event_type=ReminderRule.EventType.EARNINGS_UPCOMING
        )
        assert sorted(
            (rule.channel, rule.lead_time_minutes, rule.is_enabled) for rule in rules
        ) == [
            ("email", 2880, False),
            ("in_app", 2880, False),
        ]

    def test_company_override_rows_survive_normalization(self, user: User) -> None:
        from companies.models import Company

        company = Company.objects.create(legal_name="Norm Co", display_name="Norm Co")
        for lead in (1440, 2880):
            ReminderRule.objects.create(
                user=user,
                company=company,
                event_type=ReminderRule.EventType.EARNINGS_UPCOMING,
                channel=ReminderRule.Channel.EMAIL,
                lead_time_minutes=lead,
                is_enabled=True,
            )

        update_notification_preference(user, default_lead_time_days=1)
        set_event_type_enabled(user, ReminderRule.EventType.EARNINGS_UPCOMING, True)

        overrides = ReminderRule.objects.filter(
            user=user, company=company, event_type=ReminderRule.EventType.EARNINGS_UPCOMING
        )
        assert {(rule.channel, rule.lead_time_minutes) for rule in overrides} == {
            ("email", 1440),
            ("email", 2880),
        }

    def test_repeated_toggling_is_idempotent(self, user: User) -> None:
        update_notification_preference(user, default_lead_time_days=1)
        for _round in range(3):
            set_event_type_enabled(user, ReminderRule.EventType.FILING, False)
            set_event_type_enabled(user, ReminderRule.EventType.FILING, True)

        rules = ReminderRule.objects.filter(user=user, event_type=ReminderRule.EventType.FILING)
        assert {(rule.channel, rule.lead_time_minutes, rule.is_enabled) for rule in rules} == {
            ("email", 0, True),
            ("in_app", 0, True),
        }

    def test_unknown_event_type_rejected(self, user: User) -> None:
        with pytest.raises(NotificationSettingsError, match="Unknown event_type"):
            set_event_type_enabled(user, "market_trending", True)


class TestUpdatePreference:
    def test_partial_update_keeps_other_fields(self, user: User) -> None:
        update_notification_preference(user, email_enabled=False)
        update_notification_preference(user, default_lead_time_days=5)

        # Query directly: the reverse one-to-one cache on the user instance
        # predates the second update.
        preference = UserNotificationPreference.objects.get(user=user)
        assert preference.email_enabled is False
        assert preference.in_app_enabled is True
        assert preference.default_lead_time_minutes == 7200

    def test_invalid_lead_days_rejected(self, user: User) -> None:
        with pytest.raises(NotificationSettingsError, match="positive whole"):
            update_notification_preference(user, default_lead_time_days=0)


class TestSetUserTimezone:
    def test_set_clear_and_idempotent(self, user: User) -> None:
        set_user_timezone(user, "Asia/Tokyo")
        user.refresh_from_db()
        assert user.timezone == "Asia/Tokyo"

        set_user_timezone(user, "Asia/Tokyo")
        user.refresh_from_db()
        assert user.timezone == "Asia/Tokyo"

        set_user_timezone(user, "")
        user.refresh_from_db()
        assert user.timezone is None

    def test_invalid_zone_rejected(self, user: User) -> None:
        with pytest.raises(AccountServiceError, match="Unknown IANA timezone"):
            set_user_timezone(user, "Not/AZone")
        user.refresh_from_db()
        assert user.timezone is None
