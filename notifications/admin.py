from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from notifications.models import Notification, NotificationDeliveryAttempt


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "user",
        "event_type",
        "priority",
        "channel",
        "status",
        "suppressed_reason",
        "related_object_type",
        "created_at",
    )
    # status / priority / suppressed_reason filters cover the ops views:
    # delivery backlog (pending/processing/failed), suppressed rows and
    # per-priority queues (ADR-024 / ADR-027).
    list_filter = ("status", "priority", "channel", "event_type", "suppressed_reason")
    search_fields = (
        "=idempotency_key",
        "=related_object_id",
        "user__email",
        "last_error_code",
    )
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_select_related = ("user",)
    readonly_fields = tuple(field.name for field in Notification._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: Notification | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: Notification | None = None,
    ) -> bool:
        return False


@admin.register(NotificationDeliveryAttempt)
class NotificationDeliveryAttemptAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "notification",
        "attempt_number",
        "outcome",
        "error_code",
        "started_at",
        "finished_at",
    )
    list_filter = ("outcome",)
    search_fields = (
        "=notification__idempotency_key",
        "provider_message_id",
        "error_code",
    )
    ordering = ("-created_at",)
    list_select_related = ("notification", "notification__user")
    readonly_fields = tuple(field.name for field in NotificationDeliveryAttempt._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: NotificationDeliveryAttempt | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: NotificationDeliveryAttempt | None = None,
    ) -> bool:
        return False
