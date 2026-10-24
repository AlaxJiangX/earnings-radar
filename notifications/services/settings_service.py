"""Public write services for notification settings and read state.

These are the only write paths the settings / notifications pages use
(AGENTS.md section 6: views never mutate models directly).  Everything is
idempotent: submitting the same settings twice leaves the same rows.

Toggle semantics (PRD section 10.9, one toggle per event type):

- disabling a type sets ``is_enabled=False`` on **all** of the user's
  default rules of that type (both channels, every lead);
- enabling a type upserts one enabled default rule per channel at the
  preference's default lead (1 day for ``earnings_upcoming``, 0 for every
  other type).  Per-company override rules are never touched here.

Read state is fully independent of the send status (roadmap 6.3): any
notification in any status can be marked read, and marking twice keeps
the original ``read_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone as django_timezone

from notifications.models import (
    LEAD_TIME_NOT_APPLICABLE,
    Notification,
    ReminderRule,
    UserNotificationPreference,
)
from notifications.services.lead_time import lead_time_in_days

if TYPE_CHECKING:
    from accounts.models import User

CHANNELS = (ReminderRule.Channel.EMAIL, ReminderRule.Channel.IN_APP)
SETTING_EVENT_TYPES = tuple(ReminderRule.EventType.values)


class NotificationSettingsError(ValueError):
    pass


class NotificationNotFound(NotificationSettingsError):
    pass


def get_or_create_notification_preference(user: User) -> UserNotificationPreference:
    preference, _created = UserNotificationPreference.objects.get_or_create(user=user)
    return preference


def update_notification_preference(
    user: User,
    *,
    email_enabled: bool | None = None,
    in_app_enabled: bool | None = None,
    default_lead_time_days: int | None = None,
) -> UserNotificationPreference:
    """Update only the fields that are passed; missing rows are created.

    ``default_lead_time_days`` must be a positive whole number of days.
    """

    if default_lead_time_days is not None:
        try:
            lead_time_in_days(default_lead_time_days * 1440)
        except ValueError as error:
            raise NotificationSettingsError(
                "default_lead_time_days must be a positive whole number of days."
            ) from error
    with transaction.atomic():
        preference = UserNotificationPreference.objects.select_for_update().get_or_create(
            user=user
        )[0]
        if email_enabled is not None:
            preference.email_enabled = email_enabled
        if in_app_enabled is not None:
            preference.in_app_enabled = in_app_enabled
        if default_lead_time_days is not None:
            preference.default_lead_time_minutes = default_lead_time_days * 1440
        preference.save()
    return preference


def set_event_type_enabled(user: User, event_type: str, enabled: bool) -> None:
    """Enable / disable one reminder type at the user-default level.

    Both branches upsert one explicit rule per channel: with no rules at
    all the built-in default (enabled, ADR-024) would apply, so disabling
    must materialize ``is_enabled=False`` rows for the toggle to bite.
    """

    if event_type not in SETTING_EVENT_TYPES:
        raise NotificationSettingsError(f"Unknown event_type: {event_type!r}.")
    preference = get_or_create_notification_preference(user)
    lead = (
        preference.default_lead_time_minutes
        if event_type == ReminderRule.EventType.EARNINGS_UPCOMING
        else LEAD_TIME_NOT_APPLICABLE
    )
    with transaction.atomic():
        for channel in CHANNELS:
            rule = (
                ReminderRule.objects.select_for_update()
                .filter(
                    user=user,
                    company__isnull=True,
                    event_type=event_type,
                    channel=channel,
                    lead_time_minutes=lead,
                )
                .first()
            )
            if rule is None:
                ReminderRule.objects.create(
                    user=user,
                    company=None,
                    event_type=event_type,
                    channel=channel,
                    lead_time_minutes=lead,
                    is_enabled=enabled,
                )
            elif rule.is_enabled != enabled:
                rule.is_enabled = enabled
                rule.save(update_fields=("is_enabled", "updated_at"))


def mark_notification_read(
    user: User, notification_id: uuid.UUID, *, read_at: datetime | None = None
) -> Notification:
    """Mark one of the user's notifications as read (idempotent).

    Foreign or missing notifications raise :class:`NotificationNotFound`
    so callers can render them exactly like missing rows.
    """

    resolved_read_at = read_at or django_timezone.now()
    if resolved_read_at.tzinfo is None:
        raise NotificationSettingsError("read_at must be timezone-aware.")
    with transaction.atomic():
        notification = (
            Notification.objects.select_for_update().filter(pk=notification_id, user=user).first()
        )
        if notification is None:
            raise NotificationNotFound("Notification was not found.")
        if notification.read_at is None:
            notification.read_at = resolved_read_at
            notification.save(update_fields=("read_at", "updated_at"))
        return notification
