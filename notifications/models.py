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

# Stage 6.2 writes only pending / suppressed; processing / sent / failed /
# cancelled belong to the Stage 6.4 delivery state machine and to admin
# correction, but the vocabulary is fixed once by data-model section 9.1 so
# later stages do not need a migration to widen the check.
ALLOWED_NOTIFICATION_STATUSES = frozenset(
    {"pending", "processing", "sent", "failed", "cancelled", "suppressed"}
)
ALLOWED_NOTIFICATION_PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})
ALLOWED_RELATED_OBJECT_TYPES = frozenset(
    {
        "earnings_event",
        "earnings_date_change",
        "filing",
        "index_change_event",
        "index_change_leg",
    }
)
ALLOWED_SUPPRESSION_REASONS = frozenset(
    {"channel_disabled", "company_alerts_disabled", "rule_disabled"}
)


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


class Notification(models.Model):
    """One user-facing notification on one channel; the persistent queue.

    Written by the Stage 6.2 enqueue services inside the triggering
    transaction (transactional-outbox semantics, ADR-025).  ``pending``
    rows wait for the Stage 6.4 sender; ``suppressed`` rows record that a
    notification was deliberately not queued and why.  Delivery state
    (processing / sent / failed / cancelled), attempts and snapshots are
    Stage 6.3 / 6.4 additions.

    ``event_version`` identifies the triggering occurrence: the DataChange
    behind a release transition, the accession number of a filing, the
    reminded business date for advance reminders, or the id of the
    append-only fact row itself.  Together with user / event type /
    channel / related object / lead it feeds the deterministic
    ``idempotency_key`` so reruns and concurrent enqueues cannot create a
    second row for the same occurrence (AGENTS.md section 8).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        SUPPRESSED = "suppressed", "Suppressed"

    class Priority(models.TextChoices):
        P0 = "P0", "P0 - immediate"
        P1 = "P1", "P1 - high"
        P2 = "P2", "P2 - normal"
        P3 = "P3", "P3 - digest"

    class RelatedObjectType(models.TextChoices):
        EARNINGS_EVENT = "earnings_event", "Earnings event"
        EARNINGS_DATE_CHANGE = "earnings_date_change", "Earnings date change"
        FILING = "filing", "SEC filing"
        INDEX_CHANGE_EVENT = "index_change_event", "Index change event"
        INDEX_CHANGE_LEG = "index_change_leg", "Index change leg"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="notifications",
    )
    reminder_rule = models.ForeignKey(
        ReminderRule,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="notifications",
    )
    event_type = models.CharField(max_length=32, choices=ReminderRule.EventType.choices)
    related_object_type = models.CharField(
        max_length=32,
        choices=RelatedObjectType.choices,
    )
    related_object_id = models.UUIDField()
    event_version = models.CharField(max_length=128)
    priority = models.CharField(max_length=8, choices=Priority.choices)
    channel = models.CharField(max_length=16, choices=ReminderRule.Channel.choices)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    idempotency_key = models.CharField(max_length=64, unique=True, editable=False)
    suppressed_reason = models.CharField(  # noqa: DJ001
        max_length=32,
        null=True,
        blank=True,
        help_text="Required iff status=suppressed; ADR-024 suppression reasons.",
    )
    source_url = models.URLField(null=True, blank=True)  # noqa: DJ001
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user_id", "created_at", "id")
        indexes = [
            models.Index(
                fields=("user", "status"),
                name="notifications_user_status_idx",
            ),
            models.Index(
                fields=("event_type", "related_object_id"),
                name="notifications_rel_obj_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("idempotency_key",),
                name="notifications_idempotency_key_unique",
            ),
            models.CheckConstraint(
                condition=Q(event_type__in=ALLOWED_EVENT_TYPES),
                name="notifications_event_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(channel__in=ALLOWED_CHANNELS),
                name="notifications_channel_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=ALLOWED_NOTIFICATION_STATUSES),
                name="notifications_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(priority__in=ALLOWED_NOTIFICATION_PRIORITIES),
                name="notifications_priority_valid",
            ),
            models.CheckConstraint(
                condition=Q(related_object_type__in=ALLOWED_RELATED_OBJECT_TYPES),
                name="notifications_rel_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(suppressed_reason__isnull=True)
                    | Q(suppressed_reason__in=ALLOWED_SUPPRESSION_REASONS)
                ),
                name="notifications_suppress_reason_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(status="suppressed") & Q(suppressed_reason__isnull=False))
                    | (~Q(status="suppressed") & Q(suppressed_reason__isnull=True))
                ),
                name="notifications_suppress_reason",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.event_type}:{self.channel}:{self.priority}:{self.status}"
