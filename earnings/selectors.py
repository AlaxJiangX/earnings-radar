"""Read-only selectors for earnings pages."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from django.db.models import Case, DateField, F, IntegerField, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce

from earnings.models import EarningsEvent
from earnings.presentation import window_bounds
from indexes.models import NORMATIVE_MEMBERSHIP_STATUSES, IndexMembership

UPCOMING_EVENT_STATUSES = ("scheduled_estimated", "scheduled_confirmed")


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
