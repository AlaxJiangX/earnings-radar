"""Deterministic reminder-priority computation (PRD §9, ADR-024).

Two pure stages, both free of database and clock access:

1. :func:`resolve_company_priority_level` maps watchlist / index membership
   facts onto the PRD §9.1 company ladder S / A / B / C.
2. :func:`resolve_event_priority` maps the company level plus event facts
   onto the PRD §9.4 reminder ladder P0 / P1 / P2 / P3.

The full decision matrix is normative in ADR-024; every PRD §9.4 line maps
to one cell and remaining gaps are closed there (documented, not invented):
ordinary-watchlist earnings releases take P1, non-watchlist "important"
large-index releases cover every LARGE member in MVP, and non-watchlist
estimated dates fall back to P3 because §9.2 ranks reliability below user
attention (watchlist levels never downgrade on estimate).

"Within 24 hours" is measured in market natural days (days_until of 0 or
1), matching the ADR-024 day-before anchor: same-day releases are by
definition under 24h away, and the flagship one-day-before reminder fires
exactly at days_until == 1 (PRD §11.1 "将于明日").
"""

from __future__ import annotations

from enum import StrEnum

from notifications.models import ReminderRule


class CompanyPriorityLevel(StrEnum):
    S = "S"
    A = "A"
    B = "B"
    C = "C"


class EventPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class CompanyOutsideMonitoringPool(ValueError):
    pass


class InvalidPriorityFacts(ValueError):
    pass


_WATCHLIST_LEVEL_TO_COMPANY_LEVEL = {
    "important": CompanyPriorityLevel.S,
    "normal": CompanyPriorityLevel.A,
}

# event_type -> {company level -> priority}; None placeholders for the
# fact-driven rows are handled in code below the table.
_FIXED_EVENT_MATRIX: dict[str, dict[CompanyPriorityLevel, EventPriority]] = {
    ReminderRule.EventType.EARNINGS_RELEASED: {
        CompanyPriorityLevel.S: EventPriority.P0,
        CompanyPriorityLevel.A: EventPriority.P1,
        CompanyPriorityLevel.B: EventPriority.P1,
        CompanyPriorityLevel.C: EventPriority.P3,
    },
    ReminderRule.EventType.DATE_CHANGED: {
        CompanyPriorityLevel.S: EventPriority.P0,
        CompanyPriorityLevel.A: EventPriority.P0,
        CompanyPriorityLevel.B: EventPriority.P3,
        CompanyPriorityLevel.C: EventPriority.P3,
    },
    ReminderRule.EventType.FILING: {
        CompanyPriorityLevel.S: EventPriority.P0,
        CompanyPriorityLevel.A: EventPriority.P0,
        CompanyPriorityLevel.B: EventPriority.P3,
        CompanyPriorityLevel.C: EventPriority.P3,
    },
    ReminderRule.EventType.INDEX_TRANSFERRED: {
        CompanyPriorityLevel.S: EventPriority.P0,
        CompanyPriorityLevel.A: EventPriority.P0,
        CompanyPriorityLevel.B: EventPriority.P1,
        CompanyPriorityLevel.C: EventPriority.P3,
    },
    ReminderRule.EventType.FULL_EXIT: {
        CompanyPriorityLevel.S: EventPriority.P0,
        CompanyPriorityLevel.A: EventPriority.P0,
        CompanyPriorityLevel.B: EventPriority.P3,
        CompanyPriorityLevel.C: EventPriority.P3,
    },
}

_UPCOMING_MATRIX: dict[CompanyPriorityLevel, tuple[EventPriority, ...]] = {
    # days_until bands: (0-1), (2-3), (4-7), (>7)
    CompanyPriorityLevel.S: (
        EventPriority.P0,
        EventPriority.P1,
        EventPriority.P2,
        EventPriority.P3,
    ),
    CompanyPriorityLevel.A: (
        EventPriority.P1,
        EventPriority.P1,
        EventPriority.P2,
        EventPriority.P3,
    ),
    CompanyPriorityLevel.B: (
        EventPriority.P2,
        EventPriority.P3,
        EventPriority.P3,
        EventPriority.P3,
    ),
    CompanyPriorityLevel.C: (
        EventPriority.P3,
        EventPriority.P3,
        EventPriority.P3,
        EventPriority.P3,
    ),
}


def resolve_company_priority_level(
    *,
    watchlist_priority_level: str | None,
    in_large_index: bool,
    in_small_index: bool,
) -> CompanyPriorityLevel:
    """Map watchlist / index membership facts to the PRD §9.1 ladder.

    ``watchlist_priority_level`` must be the active item's level ("important"
    / "normal") or ``None`` when the user does not follow the company.
    Companies neither watched nor in any base index are outside the
    monitoring pool and raise :class:`CompanyOutsideMonitoringPool`.
    """

    if watchlist_priority_level is not None:
        if watchlist_priority_level not in _WATCHLIST_LEVEL_TO_COMPANY_LEVEL:
            raise InvalidPriorityFacts(
                "watchlist_priority_level must be 'important', 'normal' or None."
            )
        return _WATCHLIST_LEVEL_TO_COMPANY_LEVEL[watchlist_priority_level]
    if in_large_index:
        return CompanyPriorityLevel.B
    if in_small_index:
        return CompanyPriorityLevel.C
    raise CompanyOutsideMonitoringPool(
        "Company has no active watchlist item and no base-index membership."
    )


def resolve_event_priority(
    *,
    company_level: CompanyPriorityLevel,
    event_type: str,
    days_until: int | None = None,
    is_confirmed: bool | None = None,
    index_is_large: bool | None = None,
) -> EventPriority:
    """Map company level plus event facts to the PRD §9.4 ladder.

    Fact requirements (fail closed on ambiguity):

    - ``earnings_upcoming`` needs ``days_until`` (market natural days until
      the release date, >= 0) and ``is_confirmed``;
    - ``index_added`` / ``index_removed`` need ``index_is_large``;
    - all other event types must not receive any of the three facts.
    """

    if not isinstance(company_level, CompanyPriorityLevel):
        raise InvalidPriorityFacts("company_level must be a CompanyPriorityLevel.")

    if event_type == ReminderRule.EventType.EARNINGS_UPCOMING:
        _ensure_unused(index_is_large=index_is_large)
        if not isinstance(days_until, int) or isinstance(days_until, bool):
            raise InvalidPriorityFacts("earnings_upcoming requires integer days_until.")
        if not isinstance(is_confirmed, bool):
            raise InvalidPriorityFacts("earnings_upcoming requires boolean is_confirmed.")
        if days_until < 0:
            raise InvalidPriorityFacts("days_until must be >= 0 for upcoming events.")
        priority = _UPCOMING_MATRIX[company_level][_upcoming_band(days_until)]
        if company_level in (CompanyPriorityLevel.B, CompanyPriorityLevel.C) and not is_confirmed:
            # PRD §9.4 P3: "尚未正式确认的预计日期"; §9.2 puts user attention
            # above reliability, so watchlist levels keep their band priority.
            return EventPriority.P3
        return priority

    if event_type in (ReminderRule.EventType.INDEX_ADDED, ReminderRule.EventType.INDEX_REMOVED):
        _ensure_unused(days_until=days_until, is_confirmed=is_confirmed)
        if not isinstance(index_is_large, bool):
            raise InvalidPriorityFacts(f"{event_type} requires boolean index_is_large.")
        # PRD §9.4 P1 "大型指数公司加入或移除" applies regardless of company
        # level; SMALL-index membership changes stay digest-level.
        return EventPriority.P1 if index_is_large else EventPriority.P3

    if event_type in _FIXED_EVENT_MATRIX:
        _ensure_unused(
            days_until=days_until,
            is_confirmed=is_confirmed,
            index_is_large=index_is_large,
        )
        return _FIXED_EVENT_MATRIX[event_type][company_level]

    raise InvalidPriorityFacts(f"Unknown event_type: {event_type!r}.")


def _upcoming_band(days_until: int) -> int:
    if days_until <= 1:
        return 0
    if days_until <= 3:
        return 1
    if days_until <= 7:
        return 2
    return 3


def _ensure_unused(**facts: object) -> None:
    unexpected = [name for name, value in facts.items() if value is not None]
    if unexpected:
        raise InvalidPriorityFacts(f"Unexpected facts for this event type: {unexpected}.")
