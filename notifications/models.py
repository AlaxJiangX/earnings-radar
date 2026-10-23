from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

# Advance-reminder lead times are expressed in whole market-timezone calendar
# days (ADR-024); minutes keep the storage unit explicit and DST-neutral.
LEAD_TIME_ONE_DAY_MINUTES = 1440
LEAD_TIME_NOT_APPLICABLE = 0

ALLOWED_EVENT_TYPES = frozenset(
    {
        "earnings_upcoming",
        "date_changed",
        "earnings_released",
        "filing",
        "index_added",
        "index_removed",
        "index_transferred",
        "full_exit",
    }
)
ALLOWED_CHANNELS = frozenset({"email", "in_app"})


class UserNotificationPreference(models.Model):
    """User-level channel master switches and the default upcoming lead time.

    A missing row means every built-in default applies (all channels enabled,
    one-day advance lead); per-event-type and per-company behaviour lives in
    :class:`ReminderRule`, not here (ADR-024).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="notification_preference",
    )
    email_enabled = models.BooleanField(default=True)
    in_app_enabled = models.BooleanField(default=True)
    default_lead_time_minutes = models.PositiveIntegerField(
        default=LEAD_TIME_ONE_DAY_MINUTES,
        help_text="User-level default lead for earnings_upcoming, whole days only.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user_id", "id")
        constraints = [
            models.CheckConstraint(
                condition=Q(default_lead_time_minutes__gt=0),
                name="notifications_preference_lead_time_positive",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:email={self.email_enabled},in_app={self.in_app_enabled}"


class ReminderRule(models.Model):
    """One reminder rule at the user-default or per-company override level.

    ``company IS NULL`` marks a user-default rule.  Company-level rules fully
    replace the user-default set for the same event type and channel
    (replace semantics, ADR-024); suppression gates live outside this table.
    """

    class EventType(models.TextChoices):
        EARNINGS_UPCOMING = "earnings_upcoming", "Earnings upcoming"
        DATE_CHANGED = "date_changed", "Earnings date changed"
        EARNINGS_RELEASED = "earnings_released", "Earnings released"
        FILING = "filing", "SEC filing"
        INDEX_ADDED = "index_added", "Index added"
        INDEX_REMOVED = "index_removed", "Index removed"
        INDEX_TRANSFERRED = "index_transferred", "Index transferred"
        FULL_EXIT = "full_exit", "Exited all base indexes"

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        IN_APP = "in_app", "In-app"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reminder_rules",
    )
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reminder_rules",
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    lead_time_minutes = models.PositiveIntegerField(
        default=LEAD_TIME_ONE_DAY_MINUTES,
        help_text="Whole-day lead for earnings_upcoming; must be 0 otherwise.",
    )
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user_id", "company_id", "event_type", "channel", "lead_time_minutes")
        indexes = [
            models.Index(
                fields=("user", "company", "event_type", "channel"),
                name="notifications_rule_resolve_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("user", "company", "event_type", "channel", "lead_time_minutes"),
                nulls_distinct=False,
                name="notifications_rule_user_company_event_channel_lead_unique",
            ),
            models.CheckConstraint(
                condition=Q(event_type__in=ALLOWED_EVENT_TYPES),
                name="notifications_rule_event_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(channel__in=ALLOWED_CHANNELS),
                name="notifications_rule_channel_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(event_type="earnings_upcoming") & Q(lead_time_minutes__gt=0))
                    | (
                        ~Q(event_type="earnings_upcoming")
                        & Q(lead_time_minutes=LEAD_TIME_NOT_APPLICABLE)
                    )
                ),
                name="notifications_rule_lead_time_valid",
            ),
        ]

    def __str__(self) -> str:
        company_label = "default" if self.company_id is None else str(self.company_id)
        return (
            f"{self.user_id}:{company_label}:{self.event_type}:"
            f"{self.channel}:{self.lead_time_minutes}m"
        )
