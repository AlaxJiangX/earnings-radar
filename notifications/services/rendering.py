"""Notification email content rendering (PRD section 11, ADR-027).

Every renderer produces the PRD-mandated fields for its event type plus the
recorded source link, with times shown in both the market reference
(America/New_York) and the recipient's local timezone — the same presentation
conventions as the earnings pages.  Rendering is read-only and never mutates
domain tables.

Known limitation (ADR-027 section 11): PRD 11.3's press-release and IR-page
links have no fields in the current data model; the release email includes
the linked 8-K / 10-Q / 10-K filing URLs and the recorded source URL, and
gains the remaining links when the earnings model grows them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from earnings.models import (
    EarningsDateChange,
    EarningsEvent,
    FilingEarningsLink,
)
from earnings.presentation import MARKET_TIMEZONE
from filings.models import Filing
from indexes.models import IndexChangeEvent, IndexChangeLeg
from notifications.models import Notification
from watchlists.selectors import get_watchlist_items_for_user

if TYPE_CHECKING:
    from accounts.models import User

_EVENT_TYPE_LABELS = {
    "earnings_upcoming": "Earnings upcoming",
    "date_changed": "Earnings date changed",
    "earnings_released": "Earnings released",
    "filing": "SEC filing",
    "index_added": "Index addition",
    "index_removed": "Index removal",
    "index_transferred": "Index transfer",
    "full_exit": "Exited all base indexes",
}

_PRECISION_LABELS = {
    "unknown": "unknown",
    "date_only": "date only",
    "exact_datetime": "exact datetime",
    "session_only": "session only",
}

_RELEASE_FORM_TYPES = ("8-K", "10-Q", "10-K")


def _dual_time(value: datetime, user_timezone: ZoneInfo | None) -> str:
    eastern = value.astimezone(MARKET_TIMEZONE)
    label = eastern.strftime("%Y-%m-%d %H:%M ET")
    if user_timezone is not None and user_timezone is not MARKET_TIMEZONE:
        local = value.astimezone(user_timezone)
        label += f" / {local.strftime('%Y-%m-%d %H:%M %Z')}"
    return label


def _session_label(event: EarningsEvent) -> str:
    return event.get_release_session_display() or "unknown session"


def _fiscal_label(event: EarningsEvent) -> str:
    period = event.period_type or "FY"
    end = (
        event.period_end_date.isoformat()
        if event.period_end_date is not None
        else "unknown period end"
    )
    return f"{period} period ending {end}"


def _status_label(event: EarningsEvent) -> str:
    return event.get_status_display()


def _source_line(notification: Notification) -> str:
    if notification.source_url:
        return f"Source: {notification.source_url}"
    return "Source: not recorded"


def _release_filing_links(event_id: UUID) -> list[str]:
    links = (
        FilingEarningsLink.objects.filter(earnings_event_id=event_id)
        .select_related("filing")
        .order_by("filing__form_type", "filing__accepted_at")
    )
    return [
        f"{link.filing.form_type}: {link.filing.filing_url}"
        for link in links
        if link.filing.form_type in _RELEASE_FORM_TYPES
    ]


def _render_upcoming(
    notification: Notification, event: EarningsEvent, user_timezone: ZoneInfo | None
) -> tuple[str, list[str]]:
    timing: list[str] = []
    if event.confirmed_release_at is not None:
        timing.append(f"Confirmed time: {_dual_time(event.confirmed_release_at, user_timezone)}")
    elif event.confirmed_release_date is not None:
        timing.append(
            f"Confirmed date: {event.confirmed_release_date.isoformat()} ({_session_label(event)})"
        )
    elif event.estimated_release_at is not None:
        timing.append(f"Estimated time: {_dual_time(event.estimated_release_at, user_timezone)}")
    elif event.estimated_release_date is not None:
        timing.append(
            f"Estimated date: {event.estimated_release_date.isoformat()} ({_session_label(event)})"
        )
    else:
        timing.append("Timing: unknown")
    company = event.company.display_name
    subject = f"{company}: earnings on {notification.event_version} ({_status_label(event)})"
    body = [
        f"Company: {company}",
        f"Fiscal period: {_fiscal_label(event)}",
        *timing,
        f"Current status: {_status_label(event)}",
    ]
    if event.conference_call_at is not None:
        body.append(f"Conference call: {_dual_time(event.conference_call_at, user_timezone)}")
    body.append(_source_line(notification))
    return subject, body


def _history_value(
    *,
    precision: str,
    date_value: date | None,
    at_value: datetime | None,
    session_value: str | None,
) -> str:
    if precision == "exact_datetime" and at_value is not None:
        return f"{at_value} (exact datetime)"
    if precision == "date_only" and date_value is not None:
        return f"{date_value} (date only)"
    if precision == "session_only" and session_value is not None:
        return f"{session_value} session (session only)"
    return "unknown"


def _render_date_change(
    notification: Notification,
    change: EarningsDateChange,
    user_timezone: ZoneInfo | None,
) -> tuple[str, list[str]]:
    del user_timezone  # date-change history stores dates/sessions, not instants
    event = change.earnings_event
    company = event.company.display_name
    old_value = _history_value(
        precision=change.old_precision,
        date_value=change.old_date,
        at_value=change.old_datetime,
        session_value=change.old_session,
    )
    new_value = _history_value(
        precision=change.new_precision,
        date_value=change.new_date,
        at_value=change.new_datetime,
        session_value=change.new_session,
    )
    confirmed = change.field_name.startswith("confirmed")
    subject = f"{company}: earnings date changed"
    body = [
        f"Company: {company}",
        f"Fiscal period: {_fiscal_label(event)}",
        f"Field: {change.field_name}",
        f"Old value: {old_value} ({_PRECISION_LABELS[change.old_precision]})",
        f"New value: {new_value} ({_PRECISION_LABELS[change.new_precision]})",
        f"Formally confirmed: {'yes' if confirmed else 'no'}",
        f"Detected at: {change.detected_at.isoformat()}",
        _source_line(notification),
    ]
    return subject, body


def _render_released(
    notification: Notification, event: EarningsEvent, user_timezone: ZoneInfo | None
) -> tuple[str, list[str]]:
    company = event.company.display_name
    release: str
    if event.earnings_release_at is not None:
        release = _dual_time(event.earnings_release_at, user_timezone)
    elif event.earnings_release_date is not None:
        release = f"{event.earnings_release_date.isoformat()} ({_session_label(event)})"
    else:
        release = "unknown"
    subject = f"{company}: earnings released"
    body = [
        f"Company: {company}",
        f"Fiscal period: {_fiscal_label(event)}",
        f"Released at: {release}",
    ]
    body.extend(_release_filing_links(event.pk))
    body.append(_source_line(notification))
    return subject, body


def _render_filing(
    notification: Notification, filing: Filing, user_timezone: ZoneInfo | None
) -> tuple[str, list[str]]:
    company = filing.company.display_name
    subject = f"{company}: new SEC filing ({filing.form_type})"
    body = [
        f"Company: {company}",
        f"Form type: {filing.form_type}",
        f"Accession number: {filing.accession_number}",
        f"Accepted at: {_dual_time(filing.accepted_at, user_timezone)}",
        f"Filing URL: {filing.filing_url}",
        _source_line(notification),
    ]
    return subject, body


def _index_event_facts(
    leg: IndexChangeLeg,
) -> tuple[str, str, date | None, date]:
    return (
        leg.index.name,
        leg.action,
        leg.announcement_date,
        leg.effective_date,
    )


def _watchlist_company_ids(user: User) -> frozenset[UUID]:
    return frozenset(
        get_watchlist_items_for_user(user)
        .filter(is_active=True)
        .values_list("company_id", flat=True)
    )


def _render_index_leg(
    notification: Notification,
    leg: IndexChangeLeg,
    *,
    in_watchlist: bool,
) -> tuple[str, list[str]]:
    company = leg.security_listing.company.display_name
    index_name, action, announced, effective = _index_event_facts(leg)
    verb = "added to" if action == IndexChangeLeg.Action.ADDED else "removed from"
    subject = f"{company}: {verb} {index_name}"
    body = [
        f"Company: {company}",
        f"Index: {index_name} ({action})",
        f"Announcement date: {announced.isoformat() if announced else 'not recorded'}",
        f"Effective date: {effective.isoformat()}",
        f"Monitoring impact: {leg.event.get_monitoring_impact_display()}",
        f"In your watchlist: {'yes' if in_watchlist else 'no'}",
        _source_line(notification),
    ]
    return subject, body


def _render_index_event(
    notification: Notification,
    change_event: IndexChangeEvent,
    *,
    in_watchlist: bool,
) -> tuple[str, list[str]]:
    company = change_event.company.display_name
    legs = list(change_event.legs.select_related("index"))
    removed = [leg.index.name for leg in legs if leg.action == IndexChangeLeg.Action.REMOVED]
    added = [leg.index.name for leg in legs if leg.action == IndexChangeLeg.Action.ADDED]
    is_exit = notification.event_type == "full_exit"
    subject = f"{company}: exited all base indexes" if is_exit else f"{company}: index transfer"
    announced = next(
        (leg.announcement_date for leg in legs if leg.announcement_date is not None),
        None,
    )
    body = [
        f"Company: {company}",
        f"From index: {', '.join(removed) if removed else '-'}",
        f"To index: {', '.join(added) if added else '-'}",
        f"Displacement type: {change_event.get_displacement_display()}",
        f"Announcement date: {announced.isoformat() if announced else 'not recorded'}",
        f"Effective date: {change_event.effective_date.isoformat()}",
        f"Monitoring impact: {change_event.get_monitoring_impact_display()}",
        f"In your watchlist: {'yes' if in_watchlist else 'no'}",
        _source_line(notification),
    ]
    return subject, body


def render_notification(
    notification: Notification,
    *,
    user: User,
    user_timezone: ZoneInfo | None,
) -> tuple[str, str]:
    """Return the (subject, body) email content for one notification row."""

    content = load_notification_content([notification], users={notification.user_id: user})
    subject, lines = content.render(notification, user_timezone=user_timezone)
    return subject, "\n".join(lines)


class NotificationContent:
    """Batch-loaded domain facts backing one delivery run's rendering."""

    def __init__(
        self,
        *,
        events: dict[UUID, EarningsEvent],
        date_changes: dict[UUID, EarningsDateChange],
        filings: dict[UUID, Filing],
        legs: dict[UUID, IndexChangeLeg],
        change_events: dict[UUID, IndexChangeEvent],
        watchlist_company_ids: dict[UUID, frozenset[UUID]],
    ) -> None:
        self._events = events
        self._date_changes = date_changes
        self._filings = filings
        self._legs = legs
        self._change_events = change_events
        self._watchlist_company_ids = watchlist_company_ids

    def subject_company_id(self, notification: Notification) -> UUID | None:
        related_type = notification.related_object_type
        related_id = notification.related_object_id
        if related_type == Notification.RelatedObjectType.EARNINGS_EVENT:
            event = self._events.get(related_id)
            return event.company_id if event is not None else None
        if related_type == Notification.RelatedObjectType.EARNINGS_DATE_CHANGE:
            change = self._date_changes.get(related_id)
            return change.earnings_event.company_id if change is not None else None
        if related_type == Notification.RelatedObjectType.FILING:
            filing = self._filings.get(related_id)
            return filing.company_id if filing is not None else None
        if related_type == Notification.RelatedObjectType.INDEX_CHANGE_LEG:
            leg = self._legs.get(related_id)
            return leg.security_listing.company_id if leg is not None else None
        if related_type == Notification.RelatedObjectType.INDEX_CHANGE_EVENT:
            change_event = self._change_events.get(related_id)
            return change_event.company_id if change_event is not None else None
        return None

    def render(
        self, notification: Notification, *, user_timezone: ZoneInfo | None
    ) -> tuple[str, list[str]]:
        related_id = notification.related_object_id
        watched = self._watchlist_company_ids.get(notification.user_id, frozenset())
        in_watchlist = self.subject_company_id(notification) in watched

        if notification.related_object_type == Notification.RelatedObjectType.EARNINGS_EVENT:
            return _render_upcoming(notification, self._events[related_id], user_timezone)
        if notification.related_object_type == Notification.RelatedObjectType.EARNINGS_DATE_CHANGE:
            return _render_date_change(notification, self._date_changes[related_id], user_timezone)
        if notification.related_object_type == Notification.RelatedObjectType.FILING:
            return _render_filing(notification, self._filings[related_id], user_timezone)
        if notification.related_object_type == Notification.RelatedObjectType.INDEX_CHANGE_LEG:
            return _render_index_leg(
                notification, self._legs[related_id], in_watchlist=in_watchlist
            )
        return _render_index_event(
            notification, self._change_events[related_id], in_watchlist=in_watchlist
        )


def load_notification_content(
    notifications: Iterable[Notification],
    *,
    users: dict[UUID, User],
) -> NotificationContent:
    """Batch-load every related object needed to render *notifications*."""

    rows = list(notifications)
    events: dict[UUID, EarningsEvent] = {}
    date_changes: dict[UUID, EarningsDateChange] = {}
    filings: dict[UUID, Filing] = {}
    legs: dict[UUID, IndexChangeLeg] = {}
    change_events: dict[UUID, IndexChangeEvent] = {}

    def _ids(related_type: str) -> list[UUID]:
        return [row.related_object_id for row in rows if row.related_object_type == related_type]

    event_ids = _ids(Notification.RelatedObjectType.EARNINGS_EVENT)
    if event_ids:
        events = {
            event.pk: event
            for event in EarningsEvent.objects.select_related("company").filter(pk__in=event_ids)
        }
    change_ids = _ids(Notification.RelatedObjectType.EARNINGS_DATE_CHANGE)
    if change_ids:
        date_changes = {
            change.pk: change
            for change in EarningsDateChange.objects.select_related(
                "earnings_event", "earnings_event__company"
            ).filter(pk__in=change_ids)
        }
    filing_ids = _ids(Notification.RelatedObjectType.FILING)
    if filing_ids:
        filings = {
            filing.pk: filing
            for filing in Filing.objects.select_related("company").filter(pk__in=filing_ids)
        }
    leg_ids = _ids(Notification.RelatedObjectType.INDEX_CHANGE_LEG)
    if leg_ids:
        legs = {
            leg.pk: leg
            for leg in IndexChangeLeg.objects.select_related(
                "event", "index", "security_listing__company"
            ).filter(pk__in=leg_ids)
        }
    change_event_ids = _ids(Notification.RelatedObjectType.INDEX_CHANGE_EVENT)
    if change_event_ids:
        change_events = {
            change_event.pk: change_event
            for change_event in IndexChangeEvent.objects.select_related("company")
            .prefetch_related("legs__index")
            .filter(pk__in=change_event_ids)
        }

    watchlist_ids = {user_id: _watchlist_company_ids(user) for user_id, user in users.items()}
    return NotificationContent(
        events=events,
        date_changes=date_changes,
        filings=filings,
        legs=legs,
        change_events=change_events,
        watchlist_company_ids=watchlist_ids,
    )


def render_digest(
    notifications: Sequence[Notification],
    *,
    bucket: date,
    user: User,
    user_timezone: ZoneInfo | None,
    content: NotificationContent | None = None,
) -> tuple[str, str]:
    """Return the (subject, body) of one user's daily digest email."""

    loaded = (
        content
        if content is not None
        else load_notification_content(notifications, users={user.pk: user})
    )
    subject = f"Earnings Radar daily digest - {bucket.isoformat()}"
    blocks: list[str] = []
    ordered = sorted(
        notifications,
        key=lambda row: (row.priority, row.event_type, row.created_at, row.id),
    )
    for notification in ordered:
        item_subject, lines = loaded.render(notification, user_timezone=user_timezone)
        block_lines = [f"- {item_subject} [{notification.priority}]"]
        block_lines.extend(f"    {line}" for line in lines)
        blocks.append("\n".join(block_lines))
    item_word = "item" if len(ordered) == 1 else "items"
    header = f"Digest for {bucket.isoformat()} ({len(ordered)} {item_word}):"
    body = "\n".join((header, "", *blocks))
    return subject, body


def event_type_label(event_type: str) -> str:
    return _EVENT_TYPE_LABELS.get(event_type, event_type)
