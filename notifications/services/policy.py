"""Effective reminder-policy resolution (ADR-024).

Resolution combines three independent layers:

1. **Channel master switch** — ``UserNotificationPreference`` per channel;
   a missing preference row means all channels enabled.
2. **Company master switch** — the active ``WatchlistItem.alerts_enabled``
   (PRD §10.6 single-company toggle, owned by the watchlists app).  No
   active item means no gate: non-watchlist companies simply follow their
   rules.
3. **Event-type rule set** — company-level ``ReminderRule`` rows fully
   replace the user-default set for the same event type and channel
   (replace semantics, following the Google Calendar ``defaultReminders``
   vs ``overrides`` model); with no rows at all a built-in default applies:
   enabled, with the preference's lead for ``earnings_upcoming``.

The two switches are suppression gates AND-ed on top of the rule chain,
not links in it (following the Grafana lesson that suppression windows
must not be inherited through the content-override chain).  Decisions
carry the suppression reason so Stage 6.2 can record why a notification
was suppressed; what happens to already-queued notifications when a
watchlist item is removed stays open as ADR-023 PD-4 for Stage 6.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from notifications.models import (
    LEAD_TIME_NOT_APPLICABLE,
    LEAD_TIME_ONE_DAY_MINUTES,
    ReminderRule,
    UserNotificationPreference,
)
from watchlists.selectors import get_watchlist_items_for_user

if TYPE_CHECKING:
    from accounts.models import User
    from companies.models import Company


class PolicyOrigin(StrEnum):
    BUILTIN_DEFAULT = "builtin_default"
    USER_DEFAULT_RULE = "user_default_rule"
    COMPANY_OVERRIDE_RULE = "company_override_rule"


class SuppressionReason(StrEnum):
    CHANNEL_DISABLED = "channel_disabled"
    COMPANY_ALERTS_DISABLED = "company_alerts_disabled"
    RULE_DISABLED = "rule_disabled"


@dataclass(frozen=True, slots=True)
class ReminderPolicyDecision:
    """One resolved (event type, channel, lead) decision for Stage 6.2."""

    event_type: str
    channel: str
    lead_time_minutes: int
    origin: PolicyOrigin
    is_enabled: bool
    suppression_reason: SuppressionReason | None

    @property
    def should_notify(self) -> bool:
        return self.is_enabled and self.suppression_reason is None


def resolve_reminder_policy(
    *,
    user: User,
    company: Company,
    event_type: str,
    channels: tuple[str, ...] = (
        ReminderRule.Channel.EMAIL,
        ReminderRule.Channel.IN_APP,
    ),
) -> tuple[ReminderPolicyDecision, ...]:
    """Resolve the effective reminder decisions for one user + company.

    Returns one decision per matching rule and channel (advance reminders
    may legitimately have several whole-day leads).  Read-only and
    deterministic: identical inputs and database state yield identical
    output.
    """

    if event_type not in ReminderRule.EventType.values:
        raise ValueError(f"Unknown event_type: {event_type!r}.")
    unknown_channels = [
        channel for channel in channels if channel not in ReminderRule.Channel.values
    ]
    if unknown_channels:
        raise ValueError(f"Unknown channels: {unknown_channels}.")

    preference = UserNotificationPreference.objects.filter(user=user).first()
    default_lead = (
        preference.default_lead_time_minutes
        if preference is not None
        else LEAD_TIME_ONE_DAY_MINUTES
    )
    channel_enabled: dict[str, bool] = {
        ReminderRule.Channel.EMAIL: True if preference is None else preference.email_enabled,
        ReminderRule.Channel.IN_APP: True if preference is None else preference.in_app_enabled,
    }
    company_gate_open = _company_alerts_gate_open(user=user, company=company)

    decisions: list[ReminderPolicyDecision] = []
    for channel in channels:
        gate_reason: SuppressionReason | None = None
        if not channel_enabled[channel]:
            gate_reason = SuppressionReason.CHANNEL_DISABLED
        elif not company_gate_open:
            gate_reason = SuppressionReason.COMPANY_ALERTS_DISABLED

        for rule in _effective_rules(
            user=user,
            company=company,
            event_type=event_type,
            channel=channel,
            default_lead_time_minutes=default_lead,
        ):
            decisions.append(
                ReminderPolicyDecision(
                    event_type=event_type,
                    channel=channel,
                    lead_time_minutes=rule.lead_time_minutes,
                    origin=rule.origin,
                    is_enabled=rule.is_enabled,
                    suppression_reason=gate_reason
                    if gate_reason is not None
                    else (SuppressionReason.RULE_DISABLED if not rule.is_enabled else None),
                )
            )
    return tuple(decisions)


@dataclass(frozen=True, slots=True)
class _EffectiveRule:
    lead_time_minutes: int
    is_enabled: bool
    origin: PolicyOrigin


def _company_alerts_gate_open(*, user: User, company: Company) -> bool:
    alerts_enabled = (
        get_watchlist_items_for_user(user)
        .filter(company=company, is_active=True)
        .values_list("alerts_enabled", flat=True)
        .first()
    )
    if alerts_enabled is None:
        return True
    return alerts_enabled


def _effective_rules(
    *,
    user: User,
    company: Company,
    event_type: str,
    channel: str,
    default_lead_time_minutes: int,
) -> tuple[_EffectiveRule, ...]:
    company_rules = ReminderRule.objects.filter(
        user=user,
        company=company,
        event_type=event_type,
        channel=channel,
    )
    if company_rules.exists():
        return tuple(
            _EffectiveRule(
                lead_time_minutes=rule.lead_time_minutes,
                is_enabled=rule.is_enabled,
                origin=PolicyOrigin.COMPANY_OVERRIDE_RULE,
            )
            for rule in company_rules
        )

    user_default_rules = ReminderRule.objects.filter(
        user=user,
        company__isnull=True,
        event_type=event_type,
        channel=channel,
    )
    if user_default_rules.exists():
        return tuple(
            _EffectiveRule(
                lead_time_minutes=rule.lead_time_minutes,
                is_enabled=rule.is_enabled,
                origin=PolicyOrigin.USER_DEFAULT_RULE,
            )
            for rule in user_default_rules
        )

    builtin_lead = (
        default_lead_time_minutes
        if event_type == ReminderRule.EventType.EARNINGS_UPCOMING
        else LEAD_TIME_NOT_APPLICABLE
    )
    return (
        _EffectiveRule(
            lead_time_minutes=builtin_lead,
            is_enabled=True,
            origin=PolicyOrigin.BUILTIN_DEFAULT,
        ),
    )
