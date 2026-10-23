"""Effective reminder-policy resolution tests (ADR-024).

Covers the acceptance criteria of Stage 6.1: every MVP reminder type has a
testable rule, the single-company master switch works, user defaults and
company overrides compose with replace semantics, and repeated resolution
is stable.
"""

from __future__ import annotations

from datetime import date

import pytest

from accounts.models import User
from companies.models import Company, SecurityListing
from notifications.models import ReminderRule, UserNotificationPreference
from notifications.services.policy import (
    PolicyOrigin,
    ReminderPolicyDecision,
    SuppressionReason,
    resolve_reminder_policy,
)
from watchlists.services import (
    WatchlistMutationResult,
    add_watchlist_item,
    remove_watchlist_item,
    set_watchlist_alerts,
)

pytestmark = pytest.mark.django_db

UPCOMING = ReminderRule.EventType.EARNINGS_UPCOMING
EMAIL = ReminderRule.Channel.EMAIL
IN_APP = ReminderRule.Channel.IN_APP
AS_OF = date(2026, 10, 18)


def _company(suffix: str) -> Company:
    return Company.objects.create(
        legal_name=f"Notifications {suffix} Inc",
        display_name=f"Notifications {suffix}",
    )


def _listing(company: Company, suffix: str) -> SecurityListing:
    return SecurityListing.objects.create(
        company=company,
        ticker=f"N{suffix[:5].upper()}",
        exchange="NYSE",
        security_name=f"{company.display_name} Common",
        security_type="common_stock",
        effective_from=date(2026, 1, 1),
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


def _add_watchlist(user: User, company: Company, request_id: str) -> WatchlistMutationResult:
    return add_watchlist_item(
        user=user,
        company_id=company.pk,
        as_of=AS_OF,
        reason="notifications policy test",
        request_id=request_id,
    )


def _by_channel(
    decisions: tuple[ReminderPolicyDecision, ...],
) -> dict[str, list[ReminderPolicyDecision]]:
    grouped: dict[str, list[ReminderPolicyDecision]] = {EMAIL: [], IN_APP: []}
    for decision in decisions:
        grouped[decision.channel].append(decision)
    return grouped


class TestBuiltinDefaults:
    def test_no_preference_no_rules_means_enabled_builtin_defaults(self, user: User) -> None:
        company = _company("builtin")

        decisions = resolve_reminder_policy(user=user, company=company, event_type=UPCOMING)

        grouped = _by_channel(decisions)
        for channel in (EMAIL, IN_APP):
            decision = grouped[channel][0]
            assert decision.origin is PolicyOrigin.BUILTIN_DEFAULT
            assert decision.lead_time_minutes == 1440
            assert decision.is_enabled is True
            assert decision.suppression_reason is None
            assert decision.should_notify is True

    def test_non_upcoming_builtin_lead_is_zero(self, user: User) -> None:
        company = _company("builtin-filing")

        decisions = resolve_reminder_policy(
            user=user, company=company, event_type=ReminderRule.EventType.FILING
        )

        assert all(decision.lead_time_minutes == 0 for decision in decisions)

    def test_preference_default_lead_drives_builtin_upcoming_lead(self, user: User) -> None:
        company = _company("builtin-lead")
        UserNotificationPreference.objects.create(user=user, default_lead_time_minutes=2880)

        decisions = resolve_reminder_policy(user=user, company=company, event_type=UPCOMING)

        assert all(decision.lead_time_minutes == 2880 for decision in decisions)


@pytest.mark.parametrize("event_type", list(ReminderRule.EventType.values))
def test_every_mvp_reminder_type_has_a_testable_rule(user: User, event_type: str) -> None:
    # Roadmap 6.1: "PRD 中每种 MVP 提醒有可测试规则".
    company = _company(f"type-{event_type}")
    lead = 1440 if event_type == UPCOMING else 0
    _rule(user, event_type, EMAIL, lead_time_minutes=lead)

    decisions = resolve_reminder_policy(
        user=user, company=company, event_type=event_type, channels=(EMAIL,)
    )

    assert len(decisions) == 1
    assert decisions[0].origin is PolicyOrigin.USER_DEFAULT_RULE
    assert decisions[0].event_type == event_type
    assert decisions[0].should_notify is True


class TestChannelGate:
    def test_disabled_email_channel_suppresses_only_email(self, user: User) -> None:
        company = _company("channel-gate")
        UserNotificationPreference.objects.create(user=user, email_enabled=False)

        decisions = resolve_reminder_policy(user=user, company=company, event_type=UPCOMING)
        grouped = _by_channel(decisions)

        email_decision = grouped[EMAIL][0]
        assert email_decision.suppression_reason is SuppressionReason.CHANNEL_DISABLED
        assert email_decision.should_notify is False
        # The rule itself stays enabled so 6.2 can record why it was muted.
        assert email_decision.is_enabled is True

        in_app_decision = grouped[IN_APP][0]
        assert in_app_decision.suppression_reason is None
        assert in_app_decision.should_notify is True


class TestCompanyMasterSwitch:
    def test_company_alerts_toggle_suppresses_and_reopens(self, user: User) -> None:
        company = _company("master-switch")
        _listing(company, "master")
        result = _add_watchlist(user, company, "notifications-policy-add-master")

        decisions = resolve_reminder_policy(user=user, company=company, event_type=UPCOMING)
        assert all(decision.should_notify for decision in decisions)

        set_watchlist_alerts(
            user=user,
            item_id=result.item.pk,
            alerts_enabled=False,
            reason="notifications policy test",
            request_id="notifications-policy-mute-master",
        )
        decisions = resolve_reminder_policy(user=user, company=company, event_type=UPCOMING)
        assert decisions
        assert all(
            decision.suppression_reason is SuppressionReason.COMPANY_ALERTS_DISABLED
            for decision in decisions
        )
        assert all(not decision.should_notify for decision in decisions)

        set_watchlist_alerts(
            user=user,
            item_id=result.item.pk,
            alerts_enabled=True,
            reason="notifications policy test",
            request_id="notifications-policy-unmute-master",
        )
        decisions = resolve_reminder_policy(user=user, company=company, event_type=UPCOMING)
        assert all(decision.should_notify for decision in decisions)

    def test_removed_watchlist_item_does_not_gate(self, user: User) -> None:
        company = _company("removed-item")
        _listing(company, "removed")
        result = _add_watchlist(user, company, "notifications-policy-add-removed")
        set_watchlist_alerts(
            user=user,
            item_id=result.item.pk,
            alerts_enabled=False,
            reason="notifications policy test",
            request_id="notifications-policy-mute-removed",
        )
        remove_watchlist_item(
            user=user,
            item_id=result.item.pk,
            as_of=AS_OF,
            reason="notifications policy test",
            request_id="notifications-policy-remove-removed",
        )

        decisions = resolve_reminder_policy(user=user, company=company, event_type=UPCOMING)

        # PD-4 (cancelling already-queued notifications) stays open for 6.2;
        # after removal there is simply no gate anymore.
        assert all(decision.should_notify for decision in decisions)

    def test_gate_applies_even_with_company_override_rule(self, user: User) -> None:
        company = _company("gate-override")
        _listing(company, "gate")
        result = _add_watchlist(user, company, "notifications-policy-add-gate")
        _rule(user, UPCOMING, EMAIL, company=company, lead_time_minutes=1440)
        set_watchlist_alerts(
            user=user,
            item_id=result.item.pk,
            alerts_enabled=False,
            reason="notifications policy test",
            request_id="notifications-policy-mute-gate",
        )

        decisions = resolve_reminder_policy(
            user=user, company=company, event_type=UPCOMING, channels=(EMAIL,)
        )

        decision = decisions[0]
        assert decision.origin is PolicyOrigin.COMPANY_OVERRIDE_RULE
        assert decision.suppression_reason is SuppressionReason.COMPANY_ALERTS_DISABLED


class TestRuleLayers:
    def test_user_default_disabled_rule_is_reported(self, user: User) -> None:
        company = _company("rule-disabled")
        _rule(user, UPCOMING, EMAIL, lead_time_minutes=1440, is_enabled=False)

        decisions = resolve_reminder_policy(
            user=user, company=company, event_type=UPCOMING, channels=(EMAIL,)
        )

        decision = decisions[0]
        assert decision.origin is PolicyOrigin.USER_DEFAULT_RULE
        assert decision.is_enabled is False
        assert decision.suppression_reason is SuppressionReason.RULE_DISABLED
        assert decision.should_notify is False

    def test_company_rules_replace_user_default_set(self, user: User) -> None:
        company = _company("replace-semantics")
        _rule(user, UPCOMING, EMAIL, lead_time_minutes=1440)
        _rule(user, UPCOMING, EMAIL, lead_time_minutes=2880)
        _rule(
            user,
            UPCOMING,
            EMAIL,
            company=company,
            lead_time_minutes=1440,
            is_enabled=False,
        )

        decisions = resolve_reminder_policy(
            user=user, company=company, event_type=UPCOMING, channels=(EMAIL,)
        )

        # The whole user-default set (two enabled leads) is replaced by the
        # single disabled company override — no merging, no stacking.
        assert len(decisions) == 1
        decision = decisions[0]
        assert decision.origin is PolicyOrigin.COMPANY_OVERRIDE_RULE
        assert decision.lead_time_minutes == 1440
        assert decision.should_notify is False

    def test_company_rule_only_applies_to_its_company(self, user: User) -> None:
        watched = _company("watched")
        other = _company("other")
        _rule(user, UPCOMING, EMAIL, company=watched, lead_time_minutes=2880)

        decisions = resolve_reminder_policy(
            user=user, company=other, event_type=UPCOMING, channels=(EMAIL,)
        )

        decision = decisions[0]
        assert decision.origin is PolicyOrigin.BUILTIN_DEFAULT
        assert decision.lead_time_minutes == 1440

    def test_multiple_user_default_leads_coexist(self, user: User) -> None:
        company = _company("multi-lead")
        _rule(user, UPCOMING, EMAIL, lead_time_minutes=1440)
        _rule(user, UPCOMING, EMAIL, lead_time_minutes=2880)

        decisions = resolve_reminder_policy(
            user=user, company=company, event_type=UPCOMING, channels=(EMAIL,)
        )

        assert sorted(decision.lead_time_minutes for decision in decisions) == [1440, 2880]
        assert all(decision.should_notify for decision in decisions)

    def test_other_users_rules_do_not_leak(self, user: User) -> None:
        company = _company("isolation")
        other_user = User.objects.create_user(
            email="notifications-policy-other@example.com",
            password="fixture-only",
        )
        _rule(other_user, UPCOMING, EMAIL, lead_time_minutes=1440, is_enabled=False)

        decisions = resolve_reminder_policy(
            user=user, company=company, event_type=UPCOMING, channels=(EMAIL,)
        )

        decision = decisions[0]
        assert decision.origin is PolicyOrigin.BUILTIN_DEFAULT
        assert decision.should_notify is True


def test_resolution_is_stable_across_repeated_calls(user: User) -> None:
    # AGENTS.md §8: identical inputs and unchanged state must resolve
    # identically on every run.
    company = _company("stable")
    _listing(company, "stable")
    _add_watchlist(user, company, "notifications-policy-add-stable")
    _rule(user, UPCOMING, EMAIL, lead_time_minutes=1440)
    _rule(user, UPCOMING, EMAIL, company=company, lead_time_minutes=2880)

    first = resolve_reminder_policy(user=user, company=company, event_type=UPCOMING)
    second = resolve_reminder_policy(user=user, company=company, event_type=UPCOMING)

    assert first == second
