from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from audit.models import AppendOnlyAuditModel, AppendOnlyQuerySet


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


class EarningsDatePrecision(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    DATE_ONLY = "date_only", "Date only"
    EXACT_DATETIME = "exact_datetime", "Exact datetime"


class EarningsDateHistoryPrecision(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    DATE_ONLY = "date_only", "Date only"
    EXACT_DATETIME = "exact_datetime", "Exact datetime"
    SESSION_ONLY = "session_only", "Session only"


class EarningsDateChangeKind(models.TextChoices):
    VALUE_CHANGE = "value_change", "Value change"
    PRECISION_REFINEMENT = "precision_refinement", "Precision refinement"
    PRECISION_REGRESSION = "precision_regression", "Precision regression"


class EarningsDateField(models.TextChoices):
    ESTIMATED_RELEASE = "estimated_release", "Estimated release"
    CONFIRMED_RELEASE = "confirmed_release", "Confirmed release"
    EARNINGS_RELEASE = "earnings_release", "Earnings release"
    CONFERENCE_CALL = "conference_call", "Conference call"
    RELEASE_SESSION = "release_session", "Release session"


class ReconciliationDecisionType(models.TextChoices):
    CREATED_CANDIDATE = "created_candidate", "Created candidate"
    MATCHED_CANDIDATE = "matched_candidate", "Matched candidate"
    MATCHED_CANONICAL = "matched_canonical", "Matched canonical"
    DUPLICATE_OF = "duplicate_of", "Duplicate of"
    COLLISION = "collision", "Collision"
    CONFLICT = "conflict", "Conflict"
    REVIEW_REQUIRED = "review_required", "Review required"
    NO_MATCH = "no_match", "No match"
    IGNORED = "ignored", "Ignored"


class ReconciliationDecisionStatus(models.TextChoices):
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"
    REJECTED = "rejected", "Rejected"


ALLOWED_PERIOD_TYPES = frozenset({"Q1", "Q2", "Q3", "FY", "H1", "H2", "OTHER"})
ALLOWED_EVENT_STATUSES = frozenset(
    {"scheduled_estimated", "scheduled_confirmed", "released", "cancelled"}
)
ALLOWED_IDENTITY_STATUSES = frozenset({"candidate", "canonical"})
ALLOWED_FISCAL_CALENDAR_TYPES = frozenset({"month_based", "week_based_52_53", "other"})
ALLOWED_RELEASE_SESSIONS = frozenset({"pre_market", "after_market", "during_market", "unknown"})
ALLOWED_EARNINGS_DATE_PRECISIONS = frozenset({"unknown", "date_only", "exact_datetime"})
ALLOWED_EARNINGS_DATE_HISTORY_PRECISIONS = frozenset(
    {"unknown", "date_only", "exact_datetime", "session_only"}
)
ALLOWED_EARNINGS_DATE_CHANGE_KINDS = frozenset(
    {"value_change", "precision_refinement", "precision_regression"}
)
ALLOWED_EARNINGS_DATE_FIELDS = frozenset(
    {
        "estimated_release",
        "confirmed_release",
        "earnings_release",
        "conference_call",
        "release_session",
    }
)
ALLOWED_RECONCILIATION_DECISION_TYPES = frozenset(
    {
        "created_candidate",
        "matched_candidate",
        "matched_canonical",
        "duplicate_of",
        "collision",
        "conflict",
        "review_required",
        "no_match",
        "ignored",
    }
)
ALLOWED_RECONCILIATION_DECISION_STATUSES = frozenset({"open", "resolved", "rejected"})
RESOLVED_RECONCILIATION_DECISION_TYPES = frozenset(
    {"created_candidate", "matched_candidate", "matched_canonical", "duplicate_of"}
)
OPEN_RECONCILIATION_DECISION_TYPES = frozenset({"collision", "conflict", "review_required"})
REJECTED_RECONCILIATION_DECISION_TYPES = frozenset({"no_match", "ignored"})
ALLOWED_RECONCILIATION_COVERED_FIELDS = frozenset({"estimated_release", "release_session"})


def _earnings_date_state_constraint(
    *,
    prefix: str,
    name: str | None = None,
) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=(
            Q(
                **{
                    f"{prefix}_precision": "unknown",
                    f"{prefix}_at__isnull": True,
                    f"{prefix}_date__isnull": True,
                }
            )
            | Q(
                **{
                    f"{prefix}_precision": "date_only",
                    f"{prefix}_at__isnull": True,
                    f"{prefix}_date__isnull": False,
                }
            )
            | Q(
                **{
                    f"{prefix}_precision": "exact_datetime",
                    f"{prefix}_at__isnull": False,
                    f"{prefix}_date__isnull": True,
                }
            )
        ),
        name=name or f"earnings_event_{prefix}_precision_consistent",
    )


def _date_history_representation_is_consistent(*, side: str) -> Q:
    return (
        Q(
            **{
                f"{side}_precision": "unknown",
                f"{side}_date__isnull": True,
                f"{side}_datetime__isnull": True,
            }
        )
        | Q(
            **{
                f"{side}_precision": "date_only",
                f"{side}_date__isnull": False,
                f"{side}_datetime__isnull": True,
            }
        )
        | Q(
            **{
                f"{side}_precision": "exact_datetime",
                f"{side}_date__isnull": True,
                f"{side}_datetime__isnull": False,
            }
        )
    )


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

    identity_rule_version = models.CharField(  # noqa: DJ001
        max_length=16,
        null=True,
        blank=True,
        help_text="Rule version used to derive identity_key.",  # noqa: DJ001
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

    period_type = models.CharField(  # noqa: DJ001
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
    estimated_release_date = models.DateField(null=True, blank=True)
    estimated_release_precision = models.CharField(
        max_length=16,
        choices=EarningsDatePrecision.choices,
        default=EarningsDatePrecision.UNKNOWN,
    )
    confirmed_release_at = models.DateTimeField(null=True, blank=True)
    confirmed_release_date = models.DateField(null=True, blank=True)
    confirmed_release_precision = models.CharField(
        max_length=16,
        choices=EarningsDatePrecision.choices,
        default=EarningsDatePrecision.UNKNOWN,
    )
    earnings_release_at = models.DateTimeField(null=True, blank=True)
    earnings_release_date = models.DateField(null=True, blank=True)
    earnings_release_precision = models.CharField(
        max_length=16,
        choices=EarningsDatePrecision.choices,
        default=EarningsDatePrecision.UNKNOWN,
    )
    conference_call_at = models.DateTimeField(null=True, blank=True)
    conference_call_date = models.DateField(null=True, blank=True)
    conference_call_precision = models.CharField(
        max_length=16,
        choices=EarningsDatePrecision.choices,
        default=EarningsDatePrecision.UNKNOWN,
    )

    release_session = models.CharField(  # noqa: DJ001
        max_length=16,
        choices=ReleaseSession.choices,
        default=ReleaseSession.UNKNOWN,
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
            models.Index(fields=("status", "estimated_release_date")),
            models.Index(fields=("status", "confirmed_release_date")),
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
                condition=Q(release_session__in=ALLOWED_RELEASE_SESSIONS),
                name="earnings_event_release_session_valid",
            ),
            _earnings_date_state_constraint(prefix="estimated_release"),
            _earnings_date_state_constraint(prefix="confirmed_release"),
            _earnings_date_state_constraint(prefix="earnings_release"),
            _earnings_date_state_constraint(prefix="conference_call"),
            # --- includes_q4 bidirectional invariant ---
            models.CheckConstraint(
                condition=(
                    (Q(period_type__isnull=False) & Q(period_type="FY") & Q(includes_q4=True))
                    | (
                        Q(includes_q4=False)
                        & (
                            Q(period_type__isnull=True)
                            | (Q(period_type__isnull=False) & ~Q(period_type="FY"))
                        )
                    )
                ),
                name="earnings_event_includes_q4_consistent",
            ),
            # --- 52/53-week ---
            models.CheckConstraint(
                condition=(
                    ~Q(fiscal_calendar_type="week_based_52_53")
                    | (Q(period_length_weeks__isnull=False) & Q(period_length_weeks__in=(52, 53)))
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
        return (
            f"{company_name} {self.period_type or '?'} "
            f"@{self.period_end_date or '?'} [{self.status}]"
        )


class EarningsDateChange(AppendOnlyAuditModel):
    class Meta:
        ordering = ("-detected_at", "-created_at")
        indexes = [
            models.Index(fields=("earnings_event", "detected_at")),
            models.Index(fields=("field_name", "change_kind", "detected_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(field_name__in=ALLOWED_EARNINGS_DATE_FIELDS),
                name="earnings_date_change_field_valid",
            ),
            models.CheckConstraint(
                condition=Q(change_kind__in=ALLOWED_EARNINGS_DATE_CHANGE_KINDS),
                name="earnings_date_change_kind_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(old_precision__in=ALLOWED_EARNINGS_DATE_HISTORY_PRECISIONS)
                    & Q(new_precision__in=ALLOWED_EARNINGS_DATE_HISTORY_PRECISIONS)
                ),
                name="earnings_date_change_precision_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(field_name=EarningsDateField.RELEASE_SESSION)
                        & Q(old_precision=EarningsDateHistoryPrecision.SESSION_ONLY)
                        & Q(new_precision=EarningsDateHistoryPrecision.SESSION_ONLY)
                        & Q(old_date__isnull=True)
                        & Q(new_date__isnull=True)
                        & Q(old_datetime__isnull=True)
                        & Q(new_datetime__isnull=True)
                        & Q(old_session__isnull=False)
                        & Q(new_session__isnull=False)
                    )
                    | (
                        ~Q(field_name=EarningsDateField.RELEASE_SESSION)
                        & _date_history_representation_is_consistent(side="old")
                        & _date_history_representation_is_consistent(side="new")
                        & Q(old_session__isnull=True)
                        & Q(new_session__isnull=True)
                    )
                ),
                name="earnings_date_change_value_shape_valid",
            ),
        ]

    objects = AppendOnlyQuerySet.as_manager()

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    earnings_event = models.ForeignKey(
        EarningsEvent,
        on_delete=models.PROTECT,
        related_name="date_changes",
    )
    field_name = models.CharField(max_length=32, choices=EarningsDateField.choices)
    change_kind = models.CharField(max_length=32, choices=EarningsDateChangeKind.choices)

    old_precision = models.CharField(
        max_length=16,
        choices=EarningsDateHistoryPrecision.choices,
    )
    new_precision = models.CharField(
        max_length=16,
        choices=EarningsDateHistoryPrecision.choices,
    )

    old_date = models.DateField(null=True, blank=True)
    new_date = models.DateField(null=True, blank=True)
    old_datetime = models.DateTimeField(null=True, blank=True)
    new_datetime = models.DateTimeField(null=True, blank=True)
    old_session = models.CharField(  # noqa: DJ001
        max_length=16,
        choices=ReleaseSession.choices,
        null=True,
        blank=True,
    )
    new_session = models.CharField(  # noqa: DJ001
        max_length=16,
        choices=ReleaseSession.choices,
        null=True,
        blank=True,
    )

    data_change = models.OneToOneField(
        "audit.DataChange",
        on_delete=models.PROTECT,
        related_name="earnings_date_change",
    )
    detected_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.earnings_event_id}:{self.field_name}:{self.change_kind}"


class EarningsCalendarObservation(AppendOnlyAuditModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    source = models.ForeignKey(
        "audit.DataSource",
        on_delete=models.PROTECT,
        related_name="earnings_calendar_observations",
    )
    raw_data_record = models.ForeignKey(
        "audit.RawDataRecord",
        on_delete=models.PROTECT,
        related_name="earnings_calendar_observations",
    )

    provider_key = models.CharField(max_length=64)
    provider_version = models.CharField(max_length=100)
    parser_version = models.CharField(max_length=100)
    provider_event_id = models.CharField(max_length=255)
    raw_position = models.PositiveIntegerField()

    cik = models.CharField(max_length=10, blank=True)
    ticker = models.CharField(max_length=32, blank=True)
    exchange = models.CharField(max_length=32, blank=True)
    provider_symbol = models.CharField(max_length=64, blank=True)
    company_name = models.CharField(max_length=255, blank=True)

    fiscal_label_raw = models.CharField(max_length=64, blank=True)
    fiscal_year = models.IntegerField(null=True, blank=True)
    period_end_date = models.DateField(null=True, blank=True)
    period_type = models.CharField(  # noqa: DJ001
        max_length=8,
        choices=PeriodType.choices,
        null=True,
        blank=True,
    )
    fiscal_calendar_type = models.CharField(  # noqa: DJ001
        max_length=20,
        choices=FiscalCalendarType.choices,
        null=True,
        blank=True,
    )
    period_length_weeks = models.PositiveSmallIntegerField(null=True, blank=True)

    estimated_release_date = models.DateField(null=True, blank=True)
    estimated_release_at = models.DateTimeField(null=True, blank=True)
    estimated_release_precision = models.CharField(
        max_length=16,
        choices=EarningsDatePrecision.choices,
        default=EarningsDatePrecision.UNKNOWN,
    )
    release_session = models.CharField(  # noqa: DJ001
        max_length=16,
        choices=ReleaseSession.choices,
        default=ReleaseSession.UNKNOWN,
    )

    source_observed_at = models.DateTimeField(null=True, blank=True)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("source", "provider_event_id")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("raw_data_record", "parser_version", "provider_event_id"),
                name="earnings_calendar_observation_record_parser_event_unique",
            ),
            models.CheckConstraint(
                condition=Q(provider_key__regex=r"^[a-z][a-z0-9._-]{1,63}$"),
                name="earnings_calendar_observation_provider_key_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(provider_version__regex=r"[^[:space:]]")
                    & Q(parser_version__regex=r"[^[:space:]]")
                    & Q(provider_event_id__regex=r"[^[:space:]]")
                ),
                name="earnings_calendar_observation_identity_not_blank",
            ),
            models.CheckConstraint(
                condition=Q(cik="") | Q(cik__regex=r"^[0-9]{10}$"),
                name="earnings_calendar_observation_cik_valid",
            ),
            models.CheckConstraint(
                condition=(Q(period_type__isnull=True) | Q(period_type__in=ALLOWED_PERIOD_TYPES)),
                name="earnings_calendar_observation_period_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(fiscal_calendar_type__isnull=True)
                    | Q(fiscal_calendar_type__in=ALLOWED_FISCAL_CALENDAR_TYPES)
                ),
                name="earnings_calendar_observation_calendar_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(release_session__in=ALLOWED_RELEASE_SESSIONS),
                name="earnings_calendar_observation_release_session_valid",
            ),
            _earnings_date_state_constraint(
                prefix="estimated_release",
                name="earnings_calendar_observation_estimated_precision_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(confidence__isnull=True) | (Q(confidence__gte=0) & Q(confidence__lte=1))
                ),
                name="earnings_calendar_observation_confidence_range",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source_id}:{self.provider_event_id}@{self.raw_position}"


class EarningsReconciliationDecision(AppendOnlyAuditModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    observation = models.ForeignKey(
        EarningsCalendarObservation,
        on_delete=models.PROTECT,
        related_name="reconciliation_decisions",
    )
    decision_type = models.CharField(
        max_length=32,
        choices=ReconciliationDecisionType.choices,
    )
    status = models.CharField(
        max_length=16,
        choices=ReconciliationDecisionStatus.choices,
    )
    target_event = models.ForeignKey(
        EarningsEvent,
        on_delete=models.PROTECT,
        related_name="reconciliation_decisions",
        null=True,
        blank=True,
    )
    covered_fields = models.JSONField(default=list, blank=True)
    rule_version = models.CharField(max_length=100)
    match_factors = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=2000, blank=True)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="earnings_reconciliation_decisions",
        null=True,
        blank=True,
    )
    sync_run = models.ForeignKey(
        "audit.SyncRun",
        on_delete=models.PROTECT,
        related_name="earnings_reconciliation_decisions",
        null=True,
        blank=True,
    )
    request_id = models.CharField(max_length=255, blank=True)
    decided_at = models.DateTimeField(default=timezone.now)
    supersedes = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="superseding_decisions",
        null=True,
        blank=True,
    )
    decision_key = models.CharField(max_length=64, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ("-decided_at", "-created_at", "-id")
        indexes = [
            models.Index(fields=("observation", "decided_at")),
            models.Index(fields=("decision_type", "status", "decided_at")),
            models.Index(fields=("target_event", "decided_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(decision_type__in=ALLOWED_RECONCILIATION_DECISION_TYPES),
                name="earnings_reconciliation_decision_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=ALLOWED_RECONCILIATION_DECISION_STATUSES),
                name="earnings_reconciliation_decision_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(status="resolved")
                        & Q(decision_type__in=RESOLVED_RECONCILIATION_DECISION_TYPES)
                        & Q(target_event__isnull=False)
                    )
                    | (Q(status="open") & Q(decision_type__in=OPEN_RECONCILIATION_DECISION_TYPES))
                    | (
                        Q(status="rejected")
                        & Q(decision_type__in=REJECTED_RECONCILIATION_DECISION_TYPES)
                        & Q(target_event__isnull=True)
                    )
                ),
                name="earnings_reconciliation_decision_outcome_valid",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(actor_user__isnull=False) & ~Q(reason="") & ~Q(request_id=""))
                    | (Q(actor_user__isnull=True) & Q(sync_run__isnull=False) & Q(request_id=""))
                ),
                name="earnings_reconciliation_decision_context_valid",
            ),
            models.CheckConstraint(
                condition=Q(decision_key__regex=r"^[0-9a-f]{64}$"),
                name="earnings_reconciliation_decision_key_valid",
            ),
            models.CheckConstraint(
                condition=Q(rule_version__regex=r"[^[:space:]]"),
                name="earnings_reconciliation_decision_rule_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(supersedes__isnull=True) | ~Q(supersedes=F("id")),
                name="earnings_reconciliation_decision_not_self",
            ),
            models.UniqueConstraint(
                fields=("decision_key",),
                name="earnings_reconciliation_decision_key_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.observation_id}:{self.decision_type}:{self.status}"


class MonitoringPoolSnapshot(AppendOnlyAuditModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    as_of_date = models.DateField()
    selector_version = models.CharField(max_length=100)
    enabled_index_codes = models.JSONField(default=list)
    input_revision = models.CharField(max_length=64, editable=False)
    pool_hash = models.CharField(max_length=64, editable=False)
    member_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ("-as_of_date", "selector_version", "pool_hash")
        indexes = [
            models.Index(fields=("as_of_date", "selector_version")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(selector_version__regex=r"[^[:space:]]"),
                name="earnings_pool_snapshot_selector_version_not_empty",
            ),
            models.CheckConstraint(
                condition=~Q(enabled_index_codes=[]),
                name="earnings_pool_snapshot_enabled_indexes_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(input_revision__regex=r"^[0-9a-f]{64}$"),
                name="earnings_pool_snapshot_input_revision_valid",
            ),
            models.CheckConstraint(
                condition=Q(pool_hash__regex=r"^[0-9a-f]{64}$"),
                name="earnings_pool_snapshot_hash_valid",
            ),
            models.UniqueConstraint(
                fields=("as_of_date", "selector_version", "pool_hash"),
                name="earnings_pool_snapshot_identity_unique",
            ),
            models.UniqueConstraint(
                fields=("as_of_date", "selector_version", "input_revision"),
                name="earnings_pool_snapshot_revision_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.as_of_date}:{self.selector_version}:{self.pool_hash[:12]}"


class MonitoringPoolMember(AppendOnlyAuditModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    snapshot = models.ForeignKey(
        MonitoringPoolSnapshot,
        on_delete=models.PROTECT,
        related_name="members",
    )
    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.PROTECT,
        related_name="monitoring_pool_members",
    )
    ordinal = models.PositiveIntegerField()
    basis = models.JSONField(default=list)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ("snapshot", "ordinal")
        constraints = [
            models.CheckConstraint(
                condition=~Q(basis=[]),
                name="earnings_pool_member_basis_not_empty",
            ),
            models.UniqueConstraint(
                fields=("snapshot", "company"),
                name="earnings_pool_member_company_unique",
            ),
            models.UniqueConstraint(
                fields=("snapshot", "ordinal"),
                name="earnings_pool_member_ordinal_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.snapshot_id}:{self.ordinal}:{self.company_id}"
