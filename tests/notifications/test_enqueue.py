"""Low-level enqueue semantics: deterministic keys and reconciliation."""

from __future__ import annotations

import uuid

import pytest

from accounts.models import User
from notifications.models import Notification, ReminderRule
from notifications.services.enqueue import (
    EnqueueOutcome,
    EnqueueResult,
    enqueue_notification,
    notification_idempotency_key,
)
from notifications.services.policy import (
    PolicyOrigin,
    ReminderPolicyDecision,
    SuppressionReason,
)

pytestmark = pytest.mark.django_db

RELATED = Notification.RelatedObjectType.FILING


def _decision(
    *,
    enabled: bool = True,
    suppression: SuppressionReason | None = None,
    rule_id: uuid.UUID | None = None,
) -> ReminderPolicyDecision:
    return ReminderPolicyDecision(
        event_type=ReminderRule.EventType.FILING,
        channel=ReminderRule.Channel.EMAIL,
        lead_time_minutes=0,
        origin=PolicyOrigin.BUILTIN_DEFAULT,
        is_enabled=enabled,
        suppression_reason=suppression,
        reminder_rule_id=rule_id,
    )


def _enqueue(
    user: User,
    *,
    decision: ReminderPolicyDecision,
    event_version: str = "0000000000-26-000001",
    lead_time_minutes: int = 0,
) -> EnqueueResult:
    return enqueue_notification(
        user_id=user.pk,
        event_type=ReminderRule.EventType.FILING,
        channel=ReminderRule.Channel.EMAIL,
        related_object_type=RELATED,
        related_object_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        event_version=event_version,
        priority="P0",
        decision=decision,
        lead_time_minutes=lead_time_minutes,
    )


class TestIdempotencyKey:
    def test_same_inputs_yield_same_key(self) -> None:
        def key() -> str:
            return notification_idempotency_key(
                user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
                event_type="filing",
                channel="email",
                related_object_type="filing",
                related_object_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
                event_version="0000000000-26-000001",
            )

        assert key() == key()

    @pytest.mark.parametrize(
        ("override", "value"),
        [
            ("user_id", uuid.UUID("22222222-2222-2222-2222-222222222222")),
            ("event_type", "date_changed"),
            ("channel", "in_app"),
            ("event_version", "0000000000-26-000002"),
            ("lead_time_minutes", 2880),
        ],
    )
    def test_each_identity_component_changes_the_key(self, override: str, value: object) -> None:
        def key_with(**overrides: object) -> str:
            values: dict[str, object] = {
                "user_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
                "event_type": "filing",
                "channel": "email",
                "related_object_type": "filing",
                "related_object_id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
                "event_version": "0000000000-26-000001",
                "lead_time_minutes": 1440,
            }
            values.update(overrides)
            return notification_idempotency_key(
                user_id=values["user_id"],  # type: ignore[arg-type]
                event_type=values["event_type"],  # type: ignore[arg-type]
                channel=values["channel"],  # type: ignore[arg-type]
                related_object_type=values["related_object_type"],  # type: ignore[arg-type]
                related_object_id=values["related_object_id"],  # type: ignore[arg-type]
                event_version=values["event_version"],  # type: ignore[arg-type]
                lead_time_minutes=values["lead_time_minutes"],  # type: ignore[arg-type]
            )

        baseline = key_with()
        assert key_with(**{override: value}) != baseline


class TestEnqueueAndReconcile:
    def test_creates_pending_row(self, user: User) -> None:
        rule = ReminderRule.objects.create(
            user=user,
            event_type=ReminderRule.EventType.FILING,
            channel=ReminderRule.Channel.EMAIL,
            lead_time_minutes=0,
        )

        result = _enqueue(user, decision=_decision(rule_id=rule.pk))

        assert result.outcome is EnqueueOutcome.CREATED
        assert result.notification.status == Notification.Status.PENDING
        assert result.notification.reminder_rule_id == rule.pk
        assert result.notification.suppressed_reason is None

    def test_rerun_is_a_no_op_not_a_second_row(self, user: User) -> None:
        _enqueue(user, decision=_decision())
        second = _enqueue(user, decision=_decision())

        assert second.outcome is EnqueueOutcome.EXISTING_PENDING
        assert Notification.objects.count() == 1

    def test_suppressed_row_records_reason(self, user: User) -> None:
        result = _enqueue(
            user,
            decision=_decision(enabled=True, suppression=SuppressionReason.CHANNEL_DISABLED),
        )

        assert result.outcome is EnqueueOutcome.CREATED
        assert result.notification.status == Notification.Status.SUPPRESSED
        assert result.notification.suppressed_reason == "channel_disabled"

    def test_still_suppressed_rerun_keeps_reason_updated(self, user: User) -> None:
        _enqueue(
            user,
            decision=_decision(suppression=SuppressionReason.CHANNEL_DISABLED),
        )
        second = _enqueue(
            user,
            decision=_decision(suppression=SuppressionReason.RULE_DISABLED),
        )

        assert second.outcome is EnqueueOutcome.KEPT_SUPPRESSED
        assert second.notification.status == Notification.Status.SUPPRESSED
        assert second.notification.suppressed_reason == "rule_disabled"
        assert Notification.objects.count() == 1

    def test_suppressed_flips_back_to_pending_when_reenforced(self, user: User) -> None:
        """PD-4: re-enqueue of the same occurrence after suppression lifts."""

        _enqueue(
            user,
            decision=_decision(suppression=SuppressionReason.COMPANY_ALERTS_DISABLED),
        )
        second = _enqueue(user, decision=_decision())

        assert second.outcome is EnqueueOutcome.REACTIVATED
        assert second.notification.status == Notification.Status.PENDING
        assert second.notification.suppressed_reason is None
        assert Notification.objects.count() == 1

    def test_pending_is_never_downgraded_by_later_suppression(self, user: User) -> None:
        _enqueue(user, decision=_decision())
        second = _enqueue(
            user,
            decision=_decision(suppression=SuppressionReason.CHANNEL_DISABLED),
        )

        assert second.outcome is EnqueueOutcome.EXISTING_PENDING
        assert second.notification.status == Notification.Status.PENDING
        assert second.notification.suppressed_reason is None

    def test_delivery_states_are_frozen(self, user: User) -> None:
        first = _enqueue(user, decision=_decision())
        first.notification.status = Notification.Status.SENT
        first.notification.save(update_fields=("status", "updated_at"))

        second = _enqueue(user, decision=_decision())

        assert second.outcome is EnqueueOutcome.FROZEN_DELIVERY_STATE
        second.notification.refresh_from_db()
        assert second.notification.status == Notification.Status.SENT

    def test_different_occurrences_coexist(self, user: User) -> None:
        _enqueue(user, decision=_decision(), event_version="0000000000-26-000001")
        _enqueue(user, decision=_decision(), event_version="0000000000-26-000002")

        assert Notification.objects.count() == 2
