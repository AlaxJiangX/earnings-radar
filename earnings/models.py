from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q


class PeriodType(models.TextChoices):
    Q1 = "Q1", "Q1"
    Q2 = "Q2", "Q2"
    Q3 = "Q3", "Q3"
    FY = "FY", "FY"
    H1 = "H1", "H1"
    H2 = "H2", "H2"
    OTHER = "OTHER", "Other"


class IdentityStatus(models.TextChoices):
    CANDIDATE = "candidate", "Candidate"
    CANONICAL = "canonical", "Canonical"


class EventStatus(models.TextChoices):
    SCHEDULED_ESTIMATED = "scheduled_estimated", "Scheduled (Estimated)"
    SCHEDULED_CONFIRMED = "scheduled_confirmed", "Scheduled (Confirmed)"
    RELEASED = "released", "Released"
    CANCELLED = "cancelled", "Cancelled"


class FiscalCalendarType(models.TextChoices):
    MONTH_BASED = "month_based", "Month-based"
    WEEK_BASED_52_53 = "week_based_52_53", "52/53-week"
    OTHER = "other", "Other"


class ReleaseSession(models.TextChoices):
    PRE_MARKET = "pre_market", "Pre-market"
    AFTER_MARKET = "after_market", "After-market"
    DURING_MARKET = "during_market", "During market"
    UNKNOWN = "unknown", "Unknown"


ALLOWED_PERIOD_TYPES = frozenset({"Q1", "Q2", "Q3", "FY", "H1", "H2", "OTHER"})
ALLOWED_EVENT_STATUSES = frozenset(
    {"scheduled_estimated", "scheduled_confirmed", "released", "cancelled"}
)
ALLOWED_IDENTITY_STATUSES = frozenset({"candidate", "canonical"})
ALLOWED_FISCAL_CALENDAR_TYPES = frozenset({"month_based", "week_based_52_53", "other"})
ALLOWED_RELEASE_SESSIONS = frozenset({"pre_market", "after_market", "during_market", "unknown"})


class EarningsEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # --- Identity ---
    identity_status = models.CharField(
        max_length=16,
        choices=IdentityStatus.choices,
        default=IdentityStatus.CANDIDATE,
    )

    identity_key = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        help_text="SHA-256 canonical identity hash; NULL for CANDIDATE events.",
    )

    identity_rule_version = models.CharField(
        max_length=16,
        null=True,
        blank=True,
        help_text="Rule version used to derive identity_key.",
    )

    # --- Business identity fields ---
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="earnings_events",
    )

    period_end_date = models.DateField(
        null=True,
        blank=True,
        help_text="Fiscal period end date; required for CANONICAL events.",
    )

    period_type = models.CharField(
        max_length=8,
        choices=PeriodType.choices,
        null=True,
        blank=True,
        help_text="Required for CANONICAL events.",
    )

    includes_q4 = models.BooleanField(
        default=False,
        help_text="Always true for FY; always false for Q1/Q2/Q3/H1/H2/OTHER.",
    )

    fiscal_year = models.IntegerField(
        null=True,
        blank=True,
        help_text="Display/presentation attribute; not part of canonical identity.",
    )

    fiscal_calendar_type = models.CharField(
        max_length=20,
        choices=FiscalCalendarType.choices,
        default=FiscalCalendarType.MONTH_BASED,
    )

    period_length_weeks = models.IntegerField(
        null=True,
        blank=True,
        help_text="Required (52 or 53) when fiscal_calendar_type is week_based_52_53.",
    )

    # --- Status ---
    status = models.CharField(
        max_length=32,
        choices=EventStatus.choices,
        default=EventStatus.SCHEDULED_ESTIMATED,
    )

    # --- Date / time ---
    estimated_release_at = models.DateTimeField(null=True, blank=True)
    confirmed_release_at = models.DateTimeField(null=True, blank=True)
    earnings_release_at = models.DateTimeField(null=True, blank=True)
    conference_call_at = models.DateTimeField(null=True, blank=True)

    release_session = models.CharField(
        max_length=16,
        choices=ReleaseSession.choices,
        null=True,
        blank=True,
    )

    # --- Provenance ---
    source_evidence = models.ForeignKey(
        "audit.SourceEvidence",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="earnings_events",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("company", "period_end_date")),
            models.Index(fields=("status", "estimated_release_at")),
            models.Index(fields=("status", "confirmed_release_at")),
            models.Index(fields=("period_end_date",)),
        ]
        constraints = [
            # --- Enum validity ---
            models.CheckConstraint(
                condition=Q(period_type__in=ALLOWED_PERIOD_TYPES) | Q(period_type__isnull=True),
                name="earnings_event_period_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=ALLOWED_EVENT_STATUSES),
                name="earnings_event_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(identity_status__in=ALLOWED_IDENTITY_STATUSES),
                name="earnings_event_identity_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(fiscal_calendar_type__in=ALLOWED_FISCAL_CALENDAR_TYPES),
                name="earnings_event_calendar_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(release_session__in=ALLOWED_RELEASE_SESSIONS)
                | Q(release_session__isnull=True),
                name="earnings_event_release_session_valid",
            ),
            # --- includes_q4 bidirectional invariant ---
            models.CheckConstraint(
                condition=(
                    Q(period_type="FY", includes_q4=True)
                    | (~Q(period_type="FY") & Q(includes_q4=False))
                ),
                name="earnings_event_includes_q4_consistent",
            ),
            # --- 52/53-week ---
            models.CheckConstraint(
                condition=(
                    ~Q(fiscal_calendar_type="week_based_52_53")
                    | Q(period_length_weeks__in=(52, 53))
                ),
                name="earnings_event_week_length_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(fiscal_calendar_type="week_based_52_53") | Q(period_length_weeks__isnull=True)
                ),
                name="earnings_event_week_length_null",
            ),
            # --- CANONICAL completeness ---
            models.CheckConstraint(
                condition=(
                    ~Q(identity_status="canonical")
                    | (
                        Q(period_end_date__isnull=False)
                        & Q(period_type__isnull=False)
                        & Q(identity_key__isnull=False)
                        & Q(identity_rule_version__isnull=False)
                    )
                ),
                name="earnings_event_canonical_complete",
            ),
            # --- CANDIDATE null identity_key ---
            models.CheckConstraint(
                condition=(~Q(identity_status="candidate") | Q(identity_key__isnull=True)),
                name="earnings_event_candidate_key_null",
            ),
            # --- CANDIDATE null identity_rule_version ---
            models.CheckConstraint(
                condition=(~Q(identity_status="candidate") | Q(identity_rule_version__isnull=True)),
                name="earnings_event_candidate_version_null",
            ),
            # --- Canonical business uniqueness ---
            models.UniqueConstraint(
                fields=("company", "period_end_date", "period_type"),
                condition=Q(identity_status="canonical"),
                name="earnings_event_canonical_business_unique",
            ),
        ]

    def __str__(self) -> str:
        company_name = self.company.display_name if self.company_id else "?"
        return f"{company_name} {self.period_type or '?'} @ {self.period_end_date or '?'} [{self.status}]"
