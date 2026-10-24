"""Stage 6.4 delivery relay: claim, re-check, send, record (ADR-027).

``Notification`` is the transactional outbox (ADR-025 D1); this module is
its relay.  One run claims due ``pending`` rows with
``select_for_update(skip_locked=True)`` so two concurrent commands never
claim the same row, executes the send-time re-check (ADR-027 D4 — the final
suppression authority), delivers per channel (email via the adapter,
in-app as an immediate status close-out per the 2026-10-25 product
decision), groups P3 email rows into one digest email per user, and applies
the decided retry policy (3 attempts total, fixed 15-minute interval,
permanent failures stop immediately).

Every real send attempt appends one ``NotificationDeliveryAttempt`` row;
suppression is not an attempt and records only ``status=suppressed`` plus
the reason.  Time-dependent inputs are passed in (``now``) so replays and
tests stay deterministic; nothing in this module reads the wall clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q

from indexes.models import MarketIndex
from indexes.selectors import company_indexes_as_of
from notifications.models import (
    Notification,
    NotificationDeliveryAttempt,
    UserNotificationPreference,
)
from notifications.services.digest import (
    DIGEST_TIMEZONE_FALLBACK,
    effective_digest_time,
    is_digest_bucket_due,
    next_digest_bucket,
    user_digest_timezone,
)
from notifications.services.email import (
    DeliveryOutcome,
    EmailRequest,
    EmailSender,
    EmailSendResult,
    default_email_sender,
)
from notifications.services.lead_time import market_date_from_utc
from notifications.services.priorities import (
    CompanyOutsideMonitoringPool,
    resolve_company_priority_level,
)
from notifications.services.rendering import (
    NotificationContent,
    load_notification_content,
    render_digest,
)
from watchlists.selectors import get_watchlist_items_for_user

if TYPE_CHECKING:
    from accounts.models import User

MAX_DELIVERY_ATTEMPTS = 3
RETRY_INTERVAL = timedelta(minutes=15)

_DIGEST_PRIORITY = Notification.Priority.P3
_RETRYABLE_OUTCOMES = frozenset({DeliveryOutcome.TEMPORARY_FAILURE, DeliveryOutcome.UNKNOWN})


@dataclass(frozen=True, slots=True)
class DeliveryRunResult:
    """Aggregate outcome of one send run, for the command and tests."""

    claimed: int = 0
    sent: int = 0
    suppressed: int = 0
    failed: int = 0
    scheduled_retry: int = 0
    digest_emails: int = 0


class DeliveryError(RuntimeError):
    pass


def send_pending_notifications(
    *,
    now: datetime,
    sender: EmailSender | None = None,
    batch_size: int = 50,
) -> DeliveryRunResult:
    """Claim and deliver every due notification; safe to rerun.

    Rows are processed in batches until no due row remains; each batch claim
    and each row finalization commits independently, so a crash leaves at
    worst ``processing`` rows for manual inspection (ADR-027 section 7).
    """

    if now.tzinfo is None:
        raise DeliveryError("now must be timezone-aware.")
    resolved_sender = sender if sender is not None else default_email_sender()
    totals = DeliveryRunResult()
    while True:
        claimed = _claim_due_batch(now=now, limit=batch_size)
        if not claimed:
            break
        batch = _process_claimed(rows=claimed, now=now, sender=resolved_sender)
        totals = DeliveryRunResult(
            claimed=totals.claimed + batch.claimed,
            sent=totals.sent + batch.sent,
            suppressed=totals.suppressed + batch.suppressed,
            failed=totals.failed + batch.failed,
            scheduled_retry=totals.scheduled_retry + batch.scheduled_retry,
            digest_emails=totals.digest_emails + batch.digest_emails,
        )
    return totals


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def _preference_of(user: User) -> UserNotificationPreference | None:
    """Safe reverse-OneToOne read: a missing preference row means defaults.

    Accessing ``user.notification_preference`` directly raises
    ``RelatedObjectDoesNotExist`` for users without a row, even under
    ``select_related``; every read goes through this helper instead.
    """

    try:
        return user.notification_preference
    except ObjectDoesNotExist:
        return None


class _DigestWindow:
    """Per-user digest due facts, cached across one candidate scan."""

    def __init__(self) -> None:
        self._cache: dict[UUID, tuple[ZoneInfo, time]] = {}

    def window(self, user: User) -> tuple[ZoneInfo, time]:
        key = user.pk
        if key not in self._cache:
            self._cache[key] = (
                user_digest_timezone(user),
                effective_digest_time(_preference_of(user)),
            )
        return self._cache[key]

    def is_due(self, row: Notification, *, now: datetime) -> bool:
        timezone, digest_time = self.window(row.user)
        return is_digest_bucket_due(
            now,
            bucket=self.effective_bucket(row, now=now),
            digest_time=digest_time,
            timezone=timezone,
        )

    def effective_bucket(self, row: Notification, *, now: datetime) -> date:
        """The row's digest bucket; pre-6.4 legacy rows derive theirs from
        ``created_at`` exactly as the enqueue-time rule would have."""

        timezone, digest_time = self.window(row.user)
        if row.digest_bucket is not None:
            return row.digest_bucket
        return next_digest_bucket(row.created_at, digest_time=digest_time, timezone=timezone)


def _claim_due_batch(*, now: datetime, limit: int) -> list[Notification]:
    """Claim up to *limit* due rows as ``processing`` (concurrency-safe)."""

    due_ids = _due_candidate_ids(now=now)
    if not due_ids:
        return []
    with transaction.atomic():
        # Lock only the notification rows: FOR UPDATE cannot touch the
        # nullable side of the preference reverse join, and locking the
        # user rows would only add contention.
        locked = list(
            Notification.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("user")
            .filter(
                pk__in=due_ids[:limit],
                status=Notification.Status.PENDING,
            )
            .filter(
                Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
                Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now),
            )
            .order_by("created_at", "id")
        )
        window = _DigestWindow()
        claimed: list[Notification] = []
        for row in locked:
            if row.priority == _DIGEST_PRIORITY and not window.is_due(row, now=now):
                # A preference edit can move a bucket between the candidate
                # scan and the lock; never claim an undigested row.
                continue
            row.status = Notification.Status.PROCESSING
            row.claimed_at = now
            row.save(update_fields=("status", "claimed_at", "updated_at"))
            claimed.append(row)
    return claimed


def _due_candidate_ids(*, now: datetime) -> list[UUID]:
    window = _DigestWindow()
    due_ids: list[UUID] = []
    candidates = (
        Notification.objects.filter(status=Notification.Status.PENDING)
        .filter(
            Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
            Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now),
        )
        .select_related("user", "user__notification_preference")
        .order_by("created_at", "id")
    )
    for row in candidates:
        if row.priority == _DIGEST_PRIORITY and not window.is_due(row, now=now):
            continue
        due_ids.append(row.pk)
    return due_ids


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def _process_claimed(
    *, rows: list[Notification], now: datetime, sender: EmailSender
) -> DeliveryRunResult:
    users = {row.user_id: row.user for row in rows}
    content = load_notification_content(rows, users=users)
    alerts_by_user = _active_alerts_map(users=users, content=content, rows=rows)

    deliverable: list[Notification] = []
    suppressed = 0
    for row in rows:
        reason = _recheck_suppression(row, content=content, alerts_by_user=alerts_by_user, now=now)
        if reason is not None:
            _finalize_suppressed(row, reason=reason)
            suppressed += 1
        else:
            deliverable.append(row)

    sent = 0
    failed = 0
    scheduled_retry = 0
    digest_emails = 0
    digest_rows: dict[UUID, list[Notification]] = {}
    for row in deliverable:
        if row.channel == "email" and row.priority == _DIGEST_PRIORITY:
            digest_rows.setdefault(row.user_id, []).append(row)
            continue
        state = _deliver_row(row, now=now, sender=sender, content=content)
        if state == "sent":
            sent += 1
        elif state == "failed":
            failed += 1
        else:
            scheduled_retry += 1

    for user_rows in digest_rows.values():
        digest_emails += 1
        for state in _deliver_digest(rows=user_rows, now=now, sender=sender, content=content):
            if state == "sent":
                sent += 1
            elif state == "failed":
                failed += 1
            else:
                scheduled_retry += 1

    return DeliveryRunResult(
        claimed=len(rows),
        sent=sent,
        suppressed=suppressed,
        failed=failed,
        scheduled_retry=scheduled_retry,
        digest_emails=digest_emails,
    )


def _active_alerts_map(
    *, users: dict[UUID, User], content: NotificationContent, rows: list[Notification]
) -> dict[UUID, dict[UUID, bool]]:
    """Per-user map of subject company id -> active alerts_enabled."""

    result: dict[UUID, dict[UUID, bool]] = {}
    for user_id, user in users.items():
        company_ids = {
            company_id
            for company_id in (
                content.subject_company_id(row) for row in rows if row.user_id == user_id
            )
            if company_id is not None
        }
        entries = (
            get_watchlist_items_for_user(user)
            .filter(company_id__in=company_ids, is_active=True)
            .values_list("company_id", "alerts_enabled")
        )
        result[user_id] = {company_id: enabled for company_id, enabled in entries}
    return result


def _recheck_suppression(
    row: Notification,
    *,
    content: NotificationContent,
    alerts_by_user: dict[UUID, dict[UUID, bool]],
    now: datetime,
) -> str | None:
    """ADR-027 D4 re-check: the final suppression authority before delivery.

    Re-runs the channel gate and the company gate / watchlist membership.
    Rule-level re-resolution is deliberately out of scope (D4.3).
    """

    preference = _preference_of(row.user)
    if preference is not None:
        if row.channel == "email" and not preference.email_enabled:
            return "channel_disabled"
        if row.channel == "in_app" and not preference.in_app_enabled:
            return "channel_disabled"

    company_id = content.subject_company_id(row)
    if company_id is None:
        return None
    alerts_enabled = alerts_by_user.get(row.user_id, {}).get(company_id)

    origin = row.origin_company_level
    if origin in {"S", "A"}:
        # Watchlist-originated rows: PD-4 membership re-check.
        if alerts_enabled is not True:
            return "company_alerts_disabled"
        return None
    if origin in {"B", "C", "none"}:
        # No watchlist relationship at enqueue; suppress only when the user
        # has since added the company with company alerts turned off.
        if alerts_enabled is False:
            return "company_alerts_disabled"
        return None

    # Legacy pre-6.4 rows (origin NULL): suppress only when the company has
    # left the user's monitoring pool entirely (ADR-027 D4.2).
    if _legacy_out_of_pool(row, company_id=company_id, now=now):
        return "company_alerts_disabled"
    return None


def _legacy_out_of_pool(row: Notification, *, company_id: UUID, now: datetime) -> bool:
    watchlist_level = (
        get_watchlist_items_for_user(row.user)
        .filter(company_id=company_id, is_active=True)
        .values_list("priority_level", flat=True)
        .first()
    )
    groups = set(
        company_indexes_as_of(
            company_id=company_id, as_of_date=market_date_from_utc(now)
        ).values_list("index_group", flat=True)
    )
    try:
        resolve_company_priority_level(
            watchlist_priority_level=watchlist_level,
            in_large_index=MarketIndex.IndexGroup.LARGE in groups,
            in_small_index=MarketIndex.IndexGroup.SMALL in groups,
        )
    except CompanyOutsideMonitoringPool:
        return True
    return False


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------


def _finalize_suppressed(row: Notification, *, reason: str) -> None:
    with transaction.atomic():
        row.status = Notification.Status.SUPPRESSED
        row.suppressed_reason = reason
        row.save(update_fields=("status", "suppressed_reason", "updated_at"))


def _finalize_attempt_outcome(
    row: Notification,
    *,
    now: datetime,
    result: EmailSendResult,
    subject: str | None = None,
    body: str | None = None,
) -> str:
    """Record one attempt and move the row; returns sent|retry|failed."""

    outcome = result.outcome.value
    with transaction.atomic():
        NotificationDeliveryAttempt.objects.create(
            notification_id=row.pk,
            attempt_number=row.attempt_count + 1,
            started_at=now,
            finished_at=now,
            outcome=outcome,
            provider_message_id=result.provider_message_id,
            error_code=result.error_code,
            error_message=result.error_message,
            response_metadata={},
        )
        row.attempt_count += 1
        if result.outcome is DeliveryOutcome.ACCEPTED:
            row.status = Notification.Status.SENT
            row.sent_at = now
            row.next_attempt_at = None
            row.last_error_code = None
            row.last_error_message = None
            if subject is not None:
                row.subject_snapshot = subject
            if body is not None:
                row.body_snapshot = body
            state = "sent"
        elif result.outcome in _RETRYABLE_OUTCOMES and row.attempt_count < MAX_DELIVERY_ATTEMPTS:
            row.status = Notification.Status.PENDING
            row.next_attempt_at = now + RETRY_INTERVAL
            row.last_error_code = result.error_code
            row.last_error_message = result.error_message
            state = "retry"
        else:
            row.status = Notification.Status.FAILED
            row.next_attempt_at = None
            row.last_error_code = result.error_code
            row.last_error_message = result.error_message
            state = "failed"
        row.save(
            update_fields=(
                "attempt_count",
                "status",
                "sent_at",
                "next_attempt_at",
                "last_error_code",
                "last_error_message",
                "subject_snapshot",
                "body_snapshot",
                "updated_at",
            )
        )
    return state


def _deliver_row(
    row: Notification,
    *,
    now: datetime,
    sender: EmailSender,
    content: NotificationContent,
) -> str:
    """Deliver one non-digest row: in-app closes out, email goes out."""

    if row.channel == "in_app":
        # 2026-10-25 product decision: the in-app channel closes out as
        # sent when the re-checked row is processed; visibility has existed
        # since enqueue (architecture 5.5.5), read state stays independent.
        accepted = EmailSendResult(outcome=DeliveryOutcome.ACCEPTED)
        return _finalize_attempt_outcome(row, now=now, result=accepted)

    subject, body = render_row(row, content=content)
    result = sender.send(EmailRequest(recipient=row.user.email, subject=subject, body=body))
    return _finalize_attempt_outcome(row, now=now, result=result, subject=subject, body=body)


def _deliver_digest(
    *,
    rows: list[Notification],
    now: datetime,
    sender: EmailSender,
    content: NotificationContent,
) -> list[str]:
    """Send one digest email for a user's due P3 email rows (ADR-027 D2).

    The whole bucket shares one attempt outcome: accepted marks every member
    row sent, a failure retries (or fails) every member row together.
    """

    window = _DigestWindow()
    user = rows[0].user
    bucket = max(window.effective_bucket(row, now=now) for row in rows)
    subject, body = render_digest(
        rows,
        bucket=bucket,
        user=user,
        user_timezone=recipient_timezone(user),
        content=content,
    )
    result = sender.send(EmailRequest(recipient=user.email, subject=subject, body=body))
    return [
        _finalize_attempt_outcome(row, now=now, result=result, subject=subject, body=body)
        for row in rows
    ]


def render_row(row: Notification, *, content: NotificationContent) -> tuple[str, str]:
    subject, lines = content.render(row, user_timezone=recipient_timezone(row.user))
    return subject, "\n".join(lines)


def recipient_timezone(user: User) -> ZoneInfo | None:
    """The recipient's display timezone; unset -> ET-only, invalid -> ET."""

    value = (getattr(user, "timezone", None) or "").strip()
    if not value:
        return None
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return DIGEST_TIMEZONE_FALLBACK
