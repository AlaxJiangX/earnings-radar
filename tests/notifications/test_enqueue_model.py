from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError, transaction

from accounts.models import User
from notifications.models import Notification, ReminderRule

pytestmark = pytest.mark.django_db

RELATED = Notification.RelatedObjectType.EARNINGS_EVENT


def _notification(user: User, **overrides: object) -> Notification:
    values: dict[str, object] = {
        "user": user,
        "event_type": ReminderRule.EventType.FILING,
        "related_object_type": RELATED,
        "related_object_id": uuid.uuid4(),
        "event_version": "0000000000-26-000001",
        "priority": "P0",
        "channel": ReminderRule.Channel.EMAIL,
        "idempotency_key": uuid.uuid4().hex,
    }
    values.update(overrides)
    return Notification(**values)


def test_notification_status_defaults(user: User) -> None:
    notification = Notification.objects.create(
        user=user,
        event_type=ReminderRule.EventType.FILING,
        related_object_type=RELATED,
        related_object_id=uuid.uuid4(),
        event_version="v",
        priority="P0",
        channel=ReminderRule.Channel.EMAIL,
        idempotency_key=uuid.uuid4().hex,
    )

    assert notification.status == Notification.Status.PENDING
    assert notification.suppressed_reason is None


def test_notification_enforces_unique_idempotency_key(user: User) -> None:
    key = uuid.uuid4().hex
    _notification(user, idempotency_key=key).save()

    with pytest.raises(IntegrityError), transaction.atomic():
        _notification(user, idempotency_key=key).save()


def test_notification_rejects_unknown_enums(user: User) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        _notification(user, status="queued").save()
    with pytest.raises(IntegrityError), transaction.atomic():
        _notification(user, priority="P4").save()
    with pytest.raises(IntegrityError), transaction.atomic():
        _notification(user, channel="push").save()
    with pytest.raises(IntegrityError), transaction.atomic():
        _notification(user, event_type="market_trending").save()
    with pytest.raises(IntegrityError), transaction.atomic():
        _notification(user, related_object_type="watchlist_item").save()


def test_notification_suppressed_reason_is_coupled_to_status(user: User) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        _notification(
            user,
            status=Notification.Status.SUPPRESSED,
            suppressed_reason=None,
        ).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        _notification(
            user,
            status=Notification.Status.PENDING,
            suppressed_reason="rule_disabled",
        ).save()
    with pytest.raises(IntegrityError), transaction.atomic():
        _notification(
            user,
            status=Notification.Status.SUPPRESSED,
            suppressed_reason="not_a_reason",
        ).save()

    _notification(
        user,
        status=Notification.Status.SUPPRESSED,
        suppressed_reason="rule_disabled",
    ).save()
