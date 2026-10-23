from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from accounts.models import User
from companies.models import Company
from notifications.models import ReminderRule, UserNotificationPreference

pytestmark = pytest.mark.django_db


def _company(suffix: str) -> Company:
    return Company.objects.create(
        legal_name=f"Notifications {suffix} Inc",
        display_name=f"Notifications {suffix}",
    )


def _rule(
    user: User,
    event_type: str,
    channel: str,
    *,
    company: Company | None = None,
    lead_time_minutes: int = 1440,
    is_enabled: bool = True,
) -> ReminderRule:
    return ReminderRule.objects.create(
        user=user,
        company=company,
        event_type=event_type,
        channel=channel,
        lead_time_minutes=lead_time_minutes,
        is_enabled=is_enabled,
    )


def test_preference_defaults(user: User) -> None:
    preference = UserNotificationPreference.objects.create(user=user)

    assert preference.email_enabled is True
    assert preference.in_app_enabled is True
    assert preference.default_lead_time_minutes == 1440


def test_preference_is_one_per_user(user: User) -> None:
    UserNotificationPreference.objects.create(user=user)

    with pytest.raises(IntegrityError), transaction.atomic():
        UserNotificationPreference.objects.create(user=user)


def test_preference_rejects_non_positive_default_lead(user: User) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        UserNotificationPreference.objects.create(user=user, default_lead_time_minutes=0)


def test_rule_defaults_to_enabled_one_day_lead(user: User) -> None:
    rule = ReminderRule.objects.create(
        user=user,
        event_type=ReminderRule.EventType.EARNINGS_UPCOMING,
        channel=ReminderRule.Channel.EMAIL,
    )

    assert rule.is_enabled is True
    assert rule.lead_time_minutes == 1440
    assert rule.company_id is None


def test_rule_enforces_null_safe_unique_key(user: User) -> None:
    _rule(user, "earnings_upcoming", "email", lead_time_minutes=1440)

    # NULL-company rows compare equal under nulls_distinct=False.
    with pytest.raises(IntegrityError), transaction.atomic():
        _rule(user, "earnings_upcoming", "email", lead_time_minutes=1440)

    # A different lead or channel is a different rule and may coexist.
    _rule(user, "earnings_upcoming", "email", lead_time_minutes=2880)
    _rule(user, "earnings_upcoming", "in_app", lead_time_minutes=1440)


def test_rule_unique_key_is_scoped_per_user(user: User) -> None:
    other = User.objects.create_user(
        email="notifications-model-other@example.com",
        password="fixture-only",
    )
    _rule(user, "filing", "in_app", lead_time_minutes=0)
    _rule(other, "filing", "in_app", lead_time_minutes=0)

    with pytest.raises(IntegrityError), transaction.atomic():
        _rule(other, "filing", "in_app", lead_time_minutes=0)


def test_rule_user_default_and_company_rows_coexist(user: User) -> None:
    company = _company("coexist")

    _rule(user, "date_changed", "email", company=None, lead_time_minutes=0)
    _rule(user, "date_changed", "email", company=company, lead_time_minutes=0)

    with pytest.raises(IntegrityError), transaction.atomic():
        _rule(user, "date_changed", "email", company=company, lead_time_minutes=0)


def test_rule_lead_time_only_for_upcoming(user: User) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        _rule(user, "filing", "email", lead_time_minutes=1440)
    with pytest.raises(IntegrityError), transaction.atomic():
        _rule(user, "index_added", "email", lead_time_minutes=60)

    _rule(user, "filing", "email", lead_time_minutes=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        _rule(user, "earnings_upcoming", "email", lead_time_minutes=0)


def test_rule_rejects_unknown_event_type_or_channel(user: User) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        _rule(user, "market_trending", "email", lead_time_minutes=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        _rule(user, "filing", "push", lead_time_minutes=0)
