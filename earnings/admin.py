from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from earnings.models import EarningsEvent


@admin.register(EarningsEvent)
class EarningsEventAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "company",
        "period_type",
        "period_end_date",
        "identity_status",
        "status",
        "includes_q4",
        "fiscal_calendar_type",
        "updated_at",
    )
    list_filter = (
        "identity_status",
        "status",
        "period_type",
        "fiscal_calendar_type",
        "includes_q4",
    )
    search_fields = ("company__display_name", "company__cik", "identity_key")
    ordering = ("-created_at",)
    readonly_fields = tuple(f.name for f in EarningsEvent._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: EarningsEvent | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: EarningsEvent | None = None) -> bool:
        return False
