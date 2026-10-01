"""Read-only selectors for earnings pages and Filing-derived state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from django.db.models import Case, DateField, F, IntegerField, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce

from earnings.models import (
    EarningsEvent,
    FilingEarningsLink,
    FilingEarningsRelationType,
    FilingReleaseClassification,
)
from earnings.presentation import window_bounds
from indexes.models import NORMATIVE_MEMBERSHIP_STATUSES, IndexMembership

UPCOMING_EVENT_STATUSES = ("scheduled_estimated", "scheduled_confirmed")


@dataclass(frozen=True, slots=True)
class FilingLinkDetail:
    filing_id: UUID
    form_type: str
    accepted_at: datetime
    filing_url: str
    release_filing_classification: str | None
    review_status: str
    classification_reason: str
    match_rule_version: str
    classification_rule_version: str
    current_decision_id: UUID


@dataclass(frozen=True, slots=True)
class FilingEarningsState:
    earnings_event_id: UUID
    has_release_filing: bool
    has_periodic_filing: bool
    release_filings: tuple[FilingLinkDetail, ...]
    periodic_filings: tuple[FilingLinkDetail, ...]


def get_filing_earnings_state(*, earnings_event: EarningsEvent) -> FilingEarningsState:
    """Derive release/periodic filing state for one EarningsEvent.

    ``review_status = rejected`` links are excluded.  Only ``YES`` release
    classifications set ``has_release_filing``; ``REVIEW_REQUIRED`` never does.
    """

    states = get_filing_earnings_states(earnings_event_ids=(earnings_event.pk,))
    return states[earnings_event.pk]


def get_filing_earnings_states(
    *,
    earnings_event_ids: Sequence[UUID],
) -> dict[UUID, FilingEarningsState]:
    """Derive Filing state for multiple events in one query, without N+1 reads."""

    ordered_ids = tuple(dict.fromkeys(earnings_event_ids))
    links = (
        FilingEarningsLink.objects.filter(
            earnings_event_id__in=ordered_ids,
        )
        .exclude(review_status="rejected")
        .select_related("filing")
        .order_by("filing__accepted_at", "filing_id")
    )
    grouped: dict[UUID, list[FilingEarningsLink]] = {}
    for link in links:
        grouped.setdefault(link.earnings_event_id, []).append(link)
    result: dict[UUID, FilingEarningsState] = {}
    for event_id in ordered_ids:
        event_links = grouped.get(event_id, [])
        release = tuple(
            _detail(link)
            for link in event_links
            if link.relation_type == FilingEarningsRelationType.RELEASE_FILING
        )
        periodic = tuple(
            _detail(link)
            for link in event_links
            if link.relation_type == FilingEarningsRelationType.PERIODIC_FILING
        )
        result[event_id] = FilingEarningsState(
            earnings_event_id=event_id,
            has_release_filing=any(
                link.release_filing_classification == FilingReleaseClassification.YES
                for link in event_links
                if link.relation_type == FilingEarningsRelationType.RELEASE_FILING
            ),
            has_periodic_filing=bool(periodic),
            release_filings=release,
            periodic_filings=periodic,
        )
    return result


def _detail(link: FilingEarningsLink) -> FilingLinkDetail:
    filing = link.filing
    return FilingLinkDetail(
        filing_id=filing.pk,
        form_type=filing.form_type,
        accepted_at=filing.accepted_at,
        filing_url=filing.filing_url,
        release_filing_classification=link.release_filing_classification,
        review_status=link.review_status,
        classification_reason=link.classification_reason,
        match_rule_version=link.match_rule_version,
        classification_rule_version=link.classification_rule_version,
        current_decision_id=link.current_decision_id,
    )


def display_date_expression() -> Coalesce:
    return Coalesce(
        "confirmed_release_date",
        "earnings_release_date",
        "estimated_release_date",
        output_field=DateField(),
    )


def get_earnings_events(
    *,
    as_of: date,
    window: str,
    status: str | None = None,
    session: str | None = None,
    index_code: str | None = None,
    include_candidates: bool = False,
) -> QuerySet[EarningsEvent]:
    """Return a filtered, optimized earnings list for the requested window."""

    start, end = window_bounds(as_of, window)
    queryset = _base_event_queryset(include_candidates=include_candidates)
    queryset = queryset.filter(_display_date_in_range(start=start, end=end))
    if status is not None:
        queryset = queryset.filter(status=status)
    if session is not None:
        queryset = queryset.filter(release_session=session)
    if index_code is not None:
        queryset = queryset.filter(
            company_id__in=_company_ids_for_index(index_code=index_code, as_of=as_of)
        )
    return queryset.order_by(
        display_date_expression(),
        "company__display_name",
        "id",
    )


def next_earnings_event(
    *,
    company_id: UUID,
    as_of: date,
    include_candidates: bool = False,
) -> EarningsEvent | None:
    """Return the next non-cancelled scheduled event for a Company."""

    queryset = _base_event_queryset(include_candidates=include_candidates)
    queryset = queryset.filter(
        company_id=company_id,
        status__in=UPCOMING_EVENT_STATUSES,
    ).filter(_display_date_at_or_after(as_of))
    return queryset.order_by(
        Case(
            When(status="scheduled_confirmed", then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ),
        display_date_expression(),
        "id",
    ).first()


def next_earnings_events_for_companies(
    *,
    company_ids: tuple[UUID, ...],
    as_of: date,
    include_candidates: bool = False,
) -> dict[UUID, EarningsEvent]:
    """Return at most one next event per Company using a single query."""

    if not company_ids:
        return {}
    queryset = _base_event_queryset(include_candidates=include_candidates)
    rows = (
        queryset.filter(
            company_id__in=company_ids,
            status__in=UPCOMING_EVENT_STATUSES,
        )
        .filter(_display_date_at_or_after(as_of))
        .order_by(
            "company_id",
            Case(
                When(status="scheduled_confirmed", then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            display_date_expression(),
            "id",
        )
    )
    result: dict[UUID, EarningsEvent] = {}
    for event in rows:
        result.setdefault(event.company_id, event)
    return result


def earnings_history(
    *,
    company_id: UUID,
    include_candidates: bool = False,
) -> QuerySet[EarningsEvent]:
    """Return deterministic earnings history for a Company."""

    queryset = _base_event_queryset(include_candidates=include_candidates)
    return queryset.filter(company_id=company_id).order_by(
        F("period_end_date").desc(nulls_last=True),
        display_date_expression().desc(nulls_last=True),
        "-created_at",
    )


def _base_event_queryset(*, include_candidates: bool) -> QuerySet[EarningsEvent]:
    queryset = EarningsEvent.objects.select_related(
        "company",
        "source_evidence__raw_data_record__source",
    )
    if not include_candidates:
        queryset = queryset.filter(identity_status="canonical")
    return queryset


def _company_ids_for_index(*, index_code: str, as_of: date) -> QuerySet[IndexMembership, UUID]:
    return (
        IndexMembership.objects.filter(
            index__code=index_code.upper(),
            status__in=NORMATIVE_MEMBERSHIP_STATUSES,
            effective_from__lte=as_of,
        )
        .filter(
            Q(effective_to__isnull=True) | Q(effective_to__gt=as_of),
            security_listing__effective_from__lte=as_of,
        )
        .filter(
            Q(security_listing__effective_to__isnull=True)
            | Q(security_listing__effective_to__gt=as_of)
        )
        .values_list("security_listing__company_id", flat=True)
        .distinct()
    )


def _display_date_in_range(*, start: date, end: date) -> Q:
    """Match the Coalesce display date without relying on an annotation alias."""

    return (
        Q(confirmed_release_date__range=(start, end))
        | Q(
            confirmed_release_date__isnull=True,
            earnings_release_date__range=(start, end),
        )
        | Q(
            confirmed_release_date__isnull=True,
            earnings_release_date__isnull=True,
            estimated_release_date__range=(start, end),
        )
    )


def _display_date_at_or_after(as_of: date) -> Q:
    return (
        Q(confirmed_release_date__gte=as_of)
        | Q(
            confirmed_release_date__isnull=True,
            earnings_release_date__gte=as_of,
        )
        | Q(
            confirmed_release_date__isnull=True,
            earnings_release_date__isnull=True,
            estimated_release_date__gte=as_of,
        )
    )
