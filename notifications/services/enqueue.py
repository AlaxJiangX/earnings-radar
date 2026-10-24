"""Low-level notification enqueue with deterministic idempotency (ADR-025).

The idempotency key is a SHA-256 over the stable business identity of the
notification — user, event type, channel, related object, event version and
lead — never a random UUID, so independent reruns and concurrent enqueues
converge on one row per occurrence and channel (AGENTS.md section 8).
Notification rows act as the transactional outbox: callers invoke these
functions inside the transaction that persists the domain change, so the
current state, its change records and the queued notification commit or
roll back together.

Reconcile semantics for an existing row with the same key:

- ``pending`` stays pending (rerun no-op; never downgraded by a later
  suppression — the send-time re-check is the final authority, PD-4);
- ``suppressed`` flips back to pending when the same occurrence is
  re-dispatched while suppression no longer applies (e.g. the user
  re-enabled the channel or re-added the watchlist before the reminder
  date), implementing the PD-4 "re-add allows re-enqueue" clause;
- delivery states (processing / sent / failed / cancelled) are never
  touched here; they belong to Stage 6.4 and admin correction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import IntegrityError, transaction

from notifications.models import Notification

if TYPE_CHECKING:
    from notifications.services.policy import ReminderPolicyDecision

IDEMPOTENCY_KEY_VERSION = "v1"
# ADR-027 D2: only digest-priority rows extend the key with their bucket;
# every other row keeps the v1 formula byte-for-byte so ADR-025's frozen
# reconciliation behaviour is untouched.
DIGEST_IDEMPOTENCY_KEY_VERSION = "v2"


class EnqueueOutcome(StrEnum):
    CREATED = "created"
    EXISTING_PENDING = "existing_pending"
    REACTIVATED = "reactivated"
    KEPT_SUPPRESSED = "kept_suppressed"
    FROZEN_DELIVERY_STATE = "frozen_delivery_state"


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    notification: Notification
    outcome: EnqueueOutcome


def notification_idempotency_key(
    *,
    user_id: UUID,
    event_type: str,
    channel: str,
    related_object_type: str,
    related_object_id: UUID,
    event_version: str,
    lead_time_minutes: int = 0,
    digest_bucket: date | None = None,
) -> str:
    """Return the deterministic key for one notification occurrence.

    With ``digest_bucket`` the key is the v2 digest form (ADR-027 D2):
    same occurrence + same bucket converges on one row, a later bucket is
    a new row.  Without it the key is exactly the ADR-025 v1 formula.
    """

    version = (
        DIGEST_IDEMPOTENCY_KEY_VERSION if digest_bucket is not None else IDEMPOTENCY_KEY_VERSION
    )
    parts = [
        version,
        str(user_id),
        event_type,
        channel,
        related_object_type,
        str(related_object_id),
        event_version,
        str(lead_time_minutes),
    ]
    if digest_bucket is not None:
        parts.append(digest_bucket.isoformat())
    canonical = "|".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def enqueue_notification(
    *,
    user_id: UUID,
    event_type: str,
    channel: str,
    related_object_type: str,
    related_object_id: UUID,
    event_version: str,
    priority: str,
    decision: ReminderPolicyDecision,
    source_url: str | None = None,
    lead_time_minutes: int = 0,
    digest_bucket: date | None = None,
    origin_company_level: str | None = None,
) -> EnqueueResult:
    """Create (or reconcile) the notification row for one decision.

    Must run inside the caller's transaction; when the caller has none the
    inner ``atomic`` commits the single enqueue atomically.  ``digest_bucket``
    applies to digest-priority rows (idempotency key v2) and
    ``origin_company_level`` records the user-relative company level for the
    Stage 6.4 send-time re-check (ADR-027 D4); both are enqueue-time facts
    and are never rewritten by reconciliation.
    """

    key = notification_idempotency_key(
        user_id=user_id,
        event_type=event_type,
        channel=channel,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        event_version=event_version,
        lead_time_minutes=lead_time_minutes,
        digest_bucket=digest_bucket,
    )
    suppressed_reason = (
        decision.suppression_reason.value if decision.suppression_reason is not None else None
    )

    with transaction.atomic():
        try:
            with transaction.atomic():
                notification = Notification.objects.create(
                    user_id=user_id,
                    reminder_rule_id=decision.reminder_rule_id,
                    event_type=event_type,
                    related_object_type=related_object_type,
                    related_object_id=related_object_id,
                    event_version=event_version,
                    priority=priority,
                    channel=channel,
                    status=(
                        Notification.Status.SUPPRESSED
                        if suppressed_reason is not None
                        else Notification.Status.PENDING
                    ),
                    idempotency_key=key,
                    suppressed_reason=suppressed_reason,
                    source_url=source_url,
                    digest_bucket=digest_bucket,
                    origin_company_level=origin_company_level,
                )
                return EnqueueResult(notification=notification, outcome=EnqueueOutcome.CREATED)
        except IntegrityError:
            # A concurrent enqueue won the unique key; the inner atomic has
            # rolled the attempt back to its savepoint, so the persisted row
            # can be reconciled without trusting check-then-write ordering.
            pass
        return _reconcile(
            key=key,
            reminder_rule_id=decision.reminder_rule_id,
            suppressed_reason=suppressed_reason,
            source_url=source_url,
        )


def _reconcile(
    *,
    key: str,
    reminder_rule_id: UUID | None,
    suppressed_reason: str | None,
    source_url: str | None,
) -> EnqueueResult:
    notification = Notification.objects.select_for_update().get(idempotency_key=key)

    if notification.status in _DELIVERY_STATES:
        return EnqueueResult(
            notification=notification, outcome=EnqueueOutcome.FROZEN_DELIVERY_STATE
        )

    if notification.status == Notification.Status.SUPPRESSED:
        if suppressed_reason is None:
            # PD-4: the same occurrence re-enters the queue when the
            # suppression no longer applies at re-dispatch time.
            notification.status = Notification.Status.PENDING
            notification.suppressed_reason = None
            notification.reminder_rule_id = reminder_rule_id
            notification.source_url = source_url
            notification.save(
                update_fields=(
                    "status",
                    "suppressed_reason",
                    "reminder_rule",
                    "source_url",
                    "updated_at",
                )
            )
            return EnqueueResult(notification=notification, outcome=EnqueueOutcome.REACTIVATED)
        notification.suppressed_reason = suppressed_reason
        notification.reminder_rule_id = reminder_rule_id
        notification.save(update_fields=("suppressed_reason", "reminder_rule", "updated_at"))
        return EnqueueResult(notification=notification, outcome=EnqueueOutcome.KEPT_SUPPRESSED)

    return EnqueueResult(notification=notification, outcome=EnqueueOutcome.EXISTING_PENDING)


_DELIVERY_STATES = frozenset(
    {
        Notification.Status.PROCESSING,
        Notification.Status.SENT,
        Notification.Status.FAILED,
        Notification.Status.CANCELLED,
    }
)
