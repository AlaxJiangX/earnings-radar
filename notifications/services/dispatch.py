"""Domain-change to notification mapping and enqueue orchestration (6.2).

Each ``dispatch_*`` function fans one persisted domain occurrence out to
every active user and channel: priority comes from the Stage 6.1 pure
functions (ADR-024 matrix), and enqueue-or-suppress comes from the Stage
6.1 policy resolution with the suppression reason recorded on the row.

Call these functions inside the transaction that persists the domain
change (transactional-outbox semantics, ADR-025); the advance-reminder
scan runs on its own.  Nothing here sends anything — delivery is 6.4, and
the PD-4 send-time re-check belongs to the future sender.

Company-level facts (watchlist priority + index membership) are resolved
per user at dispatch time.  A company outside the monitoring pool skips
the user unless the occurrence's priority function explicitly maps
``None`` to a level: only ``full_exit`` does (P3), because the exit is
the last pool-relevant fact for non-watchlist users.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from django.db.models import Q, QuerySet

from accounts.models import User
from audit.models import DataChange
from earnings.models import (
    EarningsDateChange,
    EarningsDateChangeKind,
    EarningsEvent,
    EventStatus,
)
from earnings.presentation import MARKET_TIMEZONE
from filings.models import Filing
from indexes.models import IndexChangeEvent, IndexChangeLeg, MarketIndex
from indexes.selectors import company_indexes_as_of
from notifications.models import (
    LEAD_TIME_ONE_DAY_MINUTES,
    Notification,
    ReminderRule,
)
from notifications.services.enqueue import (
    EnqueueOutcome,
    EnqueueResult,
    enqueue_notification,
)
from notifications.services.policy import resolve_reminder_policy
from notifications.services.priorities import (
    CompanyOutsideMonitoringPool,
    CompanyPriorityLevel,
    resolve_company_priority_level,
    resolve_event_priority,
)
from watchlists.selectors import get_watchlist_items_for_user

if TYPE_CHECKING:
    from companies.models import Company

UPCOMING = ReminderRule.EventType.EARNINGS_UPCOMING
_DATE_CHANGED = ReminderRule.EventType.DATE_CHANGED
_EARNINGS_RELEASED = ReminderRule.EventType.EARNINGS_RELEASED
_FILING = ReminderRule.EventType.FILING
_INDEX_ADDED = ReminderRule.EventType.INDEX_ADDED
_INDEX_REMOVED = ReminderRule.EventType.INDEX_REMOVED
_INDEX_TRANSFERRED = ReminderRule.EventType.INDEX_TRANSFERRED
_FULL_EXIT = ReminderRule.EventType.FULL_EXIT

_CHANNELS = (
    ReminderRule.Channel.EMAIL,
    ReminderRule.Channel.IN_APP,
)

# Priority functions receive the user-relative company level, or None when
# the company is outside the monitoring pool for that user; returning None
# skips the user entirely.
_PriorityFn = Callable[[CompanyPriorityLevel | None], str | None]


class DispatchTargetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """Aggregate result of one dispatch call, for callers and tests."""

    results: tuple[EnqueueResult, ...]

    @property
    def created(self) -> int:
        return sum(1 for item in self.results if item.outcome is EnqueueOutcome.CREATED)

    @property
    def reactivated(self) -> int:
        return sum(1 for item in self.results if item.outcome is EnqueueOutcome.REACTIVATED)

    @property
    def notifications(self) -> tuple[Notification, ...]:
        return tuple(item.notification for item in self.results)


# ---------------------------------------------------------------------------
# Earnings date change
# ---------------------------------------------------------------------------


def dispatch_earnings_date_change(earnings_date_change_id: UUID, *, as_of: date) -> DispatchOutcome:
    """Fan out one EarningsDateChange occurrence.

    Only ``value_change`` rows dispatch: whether precision refinement /
    regression should notify stays an open product question (data-model
    section 14 #1) and is deliberately not decided here; enabling them
    later is a policy change, not a schema change.
    """

    change = EarningsDateChange.objects.select_related(
        "earnings_event",
        "earnings_event__company",
        "data_change",
        "data_change__source_evidence__raw_data_record",
    ).get(pk=earnings_date_change_id)
    if change.change_kind != EarningsDateChangeKind.VALUE_CHANGE:
        return DispatchOutcome(results=())

    return DispatchOutcome(
        results=_fan_out(
            company=change.earnings_event.company,
            event_type=_DATE_CHANGED,
            related_object_type=Notification.RelatedObjectType.EARNINGS_DATE_CHANGE,
            related_object_id=change.pk,
            event_version=str(change.data_change_id),
            priority_for=_fixed_priority(_DATE_CHANGED),
            as_of=as_of,
            source_url=_evidence_source_url(change.data_change),
        )
    )


# ---------------------------------------------------------------------------
# Earnings released
# ---------------------------------------------------------------------------


def dispatch_earnings_released(
    earnings_event_id: UUID, status_data_change_id: UUID, *, as_of: date
) -> DispatchOutcome:
    """Fan out the release transition of one EarningsEvent.

    ``status_data_change_id`` is the DataChange the lifecycle service
    wrote for the ``status -> released`` transition and doubles as the
    occurrence identity: a corrected and later re-released event dispatches
    a new occurrence through its new DataChange.
    """

    event = EarningsEvent.objects.select_related("company").get(pk=earnings_event_id)
    data_change = DataChange.objects.select_related("source_evidence__raw_data_record").get(
        pk=status_data_change_id
    )
    if event.status != EventStatus.RELEASED:
        raise DispatchTargetError("dispatch_earnings_released requires the event to be released.")
    if data_change.target_id != event.pk:
        raise DispatchTargetError("DataChange does not target the given EarningsEvent.")

    return DispatchOutcome(
        results=_fan_out(
            company=event.company,
            event_type=_EARNINGS_RELEASED,
            related_object_type=Notification.RelatedObjectType.EARNINGS_EVENT,
            related_object_id=event.pk,
            event_version=str(data_change.pk),
            priority_for=_fixed_priority(_EARNINGS_RELEASED),
            as_of=as_of,
            source_url=_evidence_source_url(data_change),
        )
    )


# ---------------------------------------------------------------------------
# SEC filing
# ---------------------------------------------------------------------------


def dispatch_filing(filing_id: UUID, *, as_of: date) -> DispatchOutcome:
    """Fan out one persisted Filing (deduplicated by accession number)."""

    filing = Filing.objects.select_related("company").get(pk=filing_id)
    return DispatchOutcome(
        results=_fan_out(
            company=filing.company,
            event_type=_FILING,
            related_object_type=Notification.RelatedObjectType.FILING,
            related_object_id=filing.pk,
            event_version=filing.accession_number,
            priority_for=_fixed_priority(_FILING),
            as_of=as_of,
            source_url=filing.filing_url,
        )
    )


# ---------------------------------------------------------------------------
# Index change event (added / removed / transferred / full exit)
# ---------------------------------------------------------------------------


def dispatch_index_change_event(index_change_event_id: UUID, *, as_of: date) -> DispatchOutcome:
    """Fan out one active IndexChangeEvent occurrence.

    Mapping (ADR-025):

    - legs mixing ADDED and REMOVED -> one ``index_transferred`` per event
      (the underlying add/remove facts are not dispatched again);
    - only ADDED legs               -> one ``index_added`` per leg;
    - only REMOVED legs             -> one ``index_removed`` per leg, plus
      one ``full_exit`` when ``monitoring_impact = exits_base_pool``.

    Company-level facts include the event's own ADDED legs, so a company
    announced into an index classifies by its post-change group even
    though the membership is not yet effective as-of today.
    """

    change_event = (
        IndexChangeEvent.objects.select_related("company", "source_evidence__raw_data_record")
        .prefetch_related("legs__index", "legs__security_listing__company")
        .get(pk=index_change_event_id)
    )
    if change_event.status != IndexChangeEvent.Status.ACTIVE:
        return DispatchOutcome(results=())

    legs = list(change_event.legs.all())
    added_legs = [leg for leg in legs if leg.action == IndexChangeLeg.Action.ADDED]
    removed_legs = [leg for leg in legs if leg.action == IndexChangeLeg.Action.REMOVED]

    results: list[EnqueueResult] = []

    if added_legs and removed_legs:
        results.extend(
            _fan_out(
                company=change_event.company,
                event_type=_INDEX_TRANSFERRED,
                related_object_type=Notification.RelatedObjectType.INDEX_CHANGE_EVENT,
                related_object_id=change_event.pk,
                event_version=str(change_event.pk),
                priority_for=_fixed_priority(_INDEX_TRANSFERRED),
                as_of=as_of,
                extra_index_groups={leg.index.index_group for leg in added_legs},
                source_url=_event_source_url(change_event),
            )
        )
        return DispatchOutcome(results=tuple(results))

    for leg in added_legs:
        results.extend(_dispatch_index_leg(leg, _INDEX_ADDED, as_of=as_of))
    for leg in removed_legs:
        results.extend(_dispatch_index_leg(leg, _INDEX_REMOVED, as_of=as_of))

    if change_event.monitoring_impact == IndexChangeEvent.MonitoringImpact.EXITS_BASE_POOL:
        results.extend(_dispatch_full_exit(change_event, as_of=as_of))
    return DispatchOutcome(results=tuple(results))


def _dispatch_index_leg(
    leg: IndexChangeLeg, event_type: str, *, as_of: date
) -> tuple[EnqueueResult, ...]:
    index_is_large = leg.index.index_group == MarketIndex.IndexGroup.LARGE

    def priority_for(level: CompanyPriorityLevel | None) -> str | None:
        if level is None:
            return None
        return resolve_event_priority(
            company_level=level, event_type=event_type, index_is_large=index_is_large
        ).value

    return _fan_out(
        company=leg.security_listing.company,
        event_type=event_type,
        related_object_type=Notification.RelatedObjectType.INDEX_CHANGE_LEG,
        related_object_id=leg.pk,
        event_version=str(leg.pk),
        priority_for=priority_for,
        as_of=as_of,
        extra_index_groups={leg.index.index_group} if event_type == _INDEX_ADDED else set(),
    )


def _dispatch_full_exit(
    change_event: IndexChangeEvent, *, as_of: date
) -> tuple[EnqueueResult, ...]:
    def priority_for(level: CompanyPriorityLevel | None) -> str | None:
        if level is None:
            # The exit is pool-relevant even for non-watchlist users;
            # every non-watchlist matrix level maps to P3 for full_exit.
            return "P3"
        return resolve_event_priority(company_level=level, event_type=_FULL_EXIT).value

    return _fan_out(
        company=change_event.company,
        event_type=_FULL_EXIT,
        related_object_type=Notification.RelatedObjectType.INDEX_CHANGE_EVENT,
        related_object_id=change_event.pk,
        event_version=str(change_event.pk),
        priority_for=priority_for,
        as_of=as_of,
        source_url=_event_source_url(change_event),
    )


# ---------------------------------------------------------------------------
# Advance (one-day-before) reminder scan
# ---------------------------------------------------------------------------


def enqueue_due_advance_reminders(*, market_date: date) -> DispatchOutcome:
    """Enqueue every due ``earnings_upcoming`` reminder for *market_date*.

    Due means: the event's business date (ADR-024 market natural day) is
    exactly the effective lead away, the event is canonical and still
    scheduled, and the user's resolved policy carries that lead.  The scan
    is idempotent: rerunning it on the same market date converges on the
    same rows through the idempotency key.
    """

    candidate_leads = _candidate_lead_minutes()
    if not candidate_leads:
        return DispatchOutcome(results=())
    max_days = max(candidate_leads) // 1440

    results: list[EnqueueResult] = []
    for event in _scheduled_events_between(market_date, market_date + timedelta(days=max_days)):
        schedule = upcoming_business_date(event)
        if schedule is None:
            continue
        business_date, is_confirmed = schedule
        days_until = (business_date - market_date).days
        if days_until < 0:
            continue
        lead_minutes = days_until * 1440
        if lead_minutes not in candidate_leads:
            continue
        results.extend(
            _fan_out_upcoming(
                event=event,
                business_date=business_date,
                days_until=days_until,
                is_confirmed=is_confirmed,
                as_of=market_date,
            )
        )
    return DispatchOutcome(results=tuple(results))


def upcoming_business_date(event: EarningsEvent) -> tuple[date, bool] | None:
    """Return the (business date, is_confirmed) used by advance reminders.

    Mirrors the earnings presentation precedence: the confirmed release
    facts when their precision is known, otherwise the estimated ones.
    Exact datetimes convert to the market-timezone natural day (ADR-024);
    unknown / session-only events have no reminder date.
    """

    for prefix, confirmed in (("confirmed_release", True), ("estimated_release", False)):
        precision = getattr(event, f"{prefix}_precision")
        if precision == "exact_datetime":
            value = getattr(event, f"{prefix}_at")
            if value is not None:
                return value.astimezone(MARKET_TIMEZONE).date(), confirmed
        elif precision == "date_only":
            value_date = getattr(event, f"{prefix}_date")
            if value_date is not None:
                return value_date, confirmed
    return None


# ---------------------------------------------------------------------------
# Shared fan-out machinery
# ---------------------------------------------------------------------------


def _fan_out(
    *,
    company: Company,
    event_type: str,
    related_object_type: str,
    related_object_id: UUID,
    event_version: str,
    priority_for: _PriorityFn,
    as_of: date,
    source_url: str | None = None,
    extra_index_groups: set[str] | None = None,
    lead_time_minutes: int = 0,
) -> tuple[EnqueueResult, ...]:
    results: list[EnqueueResult] = []
    for user in _active_users():
        level = _company_level_for(
            user=user, company=company, as_of=as_of, extra_index_groups=extra_index_groups
        )
        priority = priority_for(level)
        if priority is None:
            continue
        for decision in resolve_reminder_policy(
            user=user, company=company, event_type=event_type, channels=_CHANNELS
        ):
            results.append(
                enqueue_notification(
                    user_id=user.pk,
                    event_type=event_type,
                    channel=decision.channel,
                    related_object_type=related_object_type,
                    related_object_id=related_object_id,
                    event_version=event_version,
                    priority=priority,
                    decision=decision,
                    source_url=source_url,
                    lead_time_minutes=lead_time_minutes,
                )
            )
    return tuple(results)


def _fan_out_upcoming(
    *,
    event: EarningsEvent,
    business_date: date,
    days_until: int,
    is_confirmed: bool,
    as_of: date,
) -> tuple[EnqueueResult, ...]:
    lead_minutes = days_until * 1440
    event_version = business_date.isoformat()

    def priority_for(level: CompanyPriorityLevel | None) -> str | None:
        if level is None:
            return None
        return resolve_event_priority(
            company_level=level,
            event_type=UPCOMING,
            days_until=days_until,
            is_confirmed=is_confirmed,
        ).value

    results: list[EnqueueResult] = []
    for user in _active_users():
        level = _company_level_for(user=user, company=event.company, as_of=as_of)
        priority = priority_for(level)
        if priority is None:
            continue
        for decision in resolve_reminder_policy(
            user=user, company=event.company, event_type=UPCOMING, channels=_CHANNELS
        ):
            if decision.lead_time_minutes != lead_minutes:
                continue
            results.append(
                enqueue_notification(
                    user_id=user.pk,
                    event_type=UPCOMING,
                    channel=decision.channel,
                    related_object_type=Notification.RelatedObjectType.EARNINGS_EVENT,
                    related_object_id=event.pk,
                    event_version=event_version,
                    priority=priority,
                    decision=decision,
                    lead_time_minutes=lead_minutes,
                )
            )
    return tuple(results)


def _company_level_for(
    *,
    user: User,
    company: Company,
    as_of: date,
    extra_index_groups: set[str] | None = None,
) -> CompanyPriorityLevel | None:
    """Resolve the user-relative company level, or None outside the pool."""

    watchlist_level = (
        get_watchlist_items_for_user(user)
        .filter(company=company, is_active=True)
        .values_list("priority_level", flat=True)
        .first()
    )
    groups = set(
        company_indexes_as_of(company_id=company.pk, as_of_date=as_of).values_list(
            "index_group", flat=True
        )
    )
    if extra_index_groups:
        groups |= extra_index_groups
    try:
        return resolve_company_priority_level(
            watchlist_priority_level=watchlist_level,
            in_large_index=MarketIndex.IndexGroup.LARGE in groups,
            in_small_index=MarketIndex.IndexGroup.SMALL in groups,
        )
    except CompanyOutsideMonitoringPool:
        return None


def _active_users() -> QuerySet[User]:
    return User.objects.filter(is_active=True).order_by("pk")


def _candidate_lead_minutes() -> frozenset[int]:
    leads = {
        LEAD_TIME_ONE_DAY_MINUTES,
        *ReminderRule.objects.filter(event_type=UPCOMING).values_list(
            "lead_time_minutes", flat=True
        ),
        *User.objects.filter(notification_preference__isnull=False).values_list(
            "notification_preference__default_lead_time_minutes", flat=True
        ),
    }
    return frozenset(lead for lead in leads if lead > 0 and lead % 1440 == 0)


def _scheduled_events_between(start: date, end: date) -> QuerySet[EarningsEvent]:
    # Pad the UTC pre-filter window so exact-datetime events near midnight
    # Eastern are not excluded before the market-timezone check.
    window_start_at = datetime.combine(start - timedelta(days=1), time.min, tzinfo=UTC)
    window_end_at = datetime.combine(end + timedelta(days=2), time.min, tzinfo=UTC)
    date_start = start - timedelta(days=1)
    date_end = end + timedelta(days=1)
    return (
        EarningsEvent.objects.select_related("company")
        .filter(
            identity_status="canonical",
            status__in=("scheduled_estimated", "scheduled_confirmed"),
        )
        .filter(
            Q(estimated_release_date__range=(date_start, date_end))
            | Q(confirmed_release_date__range=(date_start, date_end))
            | Q(estimated_release_at__range=(window_start_at, window_end_at))
            | Q(confirmed_release_at__range=(window_start_at, window_end_at))
        )
    )


def _fixed_priority(event_type: str) -> _PriorityFn:
    def priority_for(level: CompanyPriorityLevel | None) -> str | None:
        if level is None:
            return None
        return resolve_event_priority(company_level=level, event_type=event_type).value

    return priority_for


def _evidence_source_url(data_change: DataChange) -> str | None:
    evidence = getattr(data_change, "source_evidence", None)
    if evidence is None:
        return None
    raw = getattr(evidence, "raw_data_record", None)
    return getattr(raw, "source_url", None)


def _event_source_url(change_event: IndexChangeEvent) -> str | None:
    evidence = getattr(change_event, "source_evidence", None)
    if evidence is None:
        return None
    raw = getattr(evidence, "raw_data_record", None)
    return getattr(raw, "source_url", None)
