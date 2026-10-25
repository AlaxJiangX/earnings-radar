"""Stage 7.2 admin registration for notification records: read-only + filters."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import Client, RequestFactory

from accounts.models import User
from notifications.admin import (
    NotificationAdmin,
    NotificationDeliveryAttemptAdmin,
)
from notifications.models import (
    Notification,
    NotificationDeliveryAttempt,
    ReminderRule,
)

pytestmark = pytest.mark.django_db

RELATED = Notification.RelatedObjectType.EARNINGS_EVENT


@pytest.mark.parametrize(
    ("model", "admin_class"),
    (
        (Notification, NotificationAdmin),
        (NotificationDeliveryAttempt, NotificationDeliveryAttemptAdmin),
    ),
)
def test_notification_admins_are_read_only(
    model: type[Notification] | type[NotificationDeliveryAttempt],
    admin_class: type[NotificationAdmin] | type[NotificationDeliveryAttemptAdmin],
) -> None:
    model_admin = admin_class(model, AdminSite())
    request = RequestFactory().get("/admin/notifications/")

    assert set(model_admin.readonly_fields) == {field.name for field in model._meta.fields}
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def test_notification_admin_filters_cover_ops_views() -> None:
    model_admin = NotificationAdmin(Notification, AdminSite())

    # delivery backlog, suppressed rows and per-priority queues
    assert {"status", "priority", "suppressed_reason", "channel"} <= set(model_admin.list_filter)
    assert {"=idempotency_key", "user__email"} <= set(model_admin.search_fields)


def test_notification_changelist_renders_and_filters_by_status_and_suppression(
    client: Client,
) -> None:
    user = User.objects.create_user(email="notify-admin@example.test", password="test")
    suppressed = Notification.objects.create(
        user=user,
        event_type=ReminderRule.EventType.FILING,
        related_object_type=RELATED,
        related_object_id=uuid.uuid4(),
        event_version="v-1",
        priority="P3",
        channel=ReminderRule.Channel.EMAIL,
        status="suppressed",
        suppressed_reason="channel_disabled",
        idempotency_key=uuid.uuid4().hex,
    )
    Notification.objects.create(
        user=user,
        event_type=ReminderRule.EventType.FILING,
        related_object_type=RELATED,
        related_object_id=uuid.uuid4(),
        event_version="v-2",
        priority="P1",
        channel=ReminderRule.Channel.IN_APP,
        status="pending",
        idempotency_key=uuid.uuid4().hex,
    )
    admin_user = User.objects.create_superuser(
        email="notify-super@example.test", password="fixture-password-only"
    )
    client.force_login(admin_user)

    response = client.get(
        "/admin/notifications/notification/",
        {"status__exact": "suppressed", "suppressed_reason__exact": "channel_disabled"},
    )

    assert response.status_code == 200
    changelist = response.context["cl"]
    assert [obj.pk for obj in changelist.queryset] == [suppressed.pk]
