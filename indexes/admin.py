from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from indexes.models import IndexMembership, MarketIndex


@admin.register(MarketIndex)
class MarketIndexAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("code", "name", "index_group", "is_enabled", "updated_at")
    list_filter = ("index_group", "is_enabled")
    search_fields = ("code", "name")
    ordering = ("code",)
    readonly_fields = tuple(field.name for field in MarketIndex._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: MarketIndex | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: MarketIndex | None = None) -> bool:
        return False


@admin.register(IndexMembership)
class IndexMembershipAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "index",
        "security_listing",
        "status",
        "effective_from",
        "effective_to",
        "updated_at",
    )
    list_filter = ("status", "index")
    search_fields = ("security_listing__ticker", "index__code")
    ordering = ("security_listing", "index", "effective_from", "id")
    readonly_fields = tuple(field.name for field in IndexMembership._meta.fields)
    raw_id_fields = ("supersedes",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: IndexMembership | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: IndexMembership | None = None
    ) -> bool:
        return False
