from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from indexes.models import (
    IndexChangeCorrelation,
    IndexChangeEvent,
    IndexChangeLeg,
    IndexMembership,
    MarketIndex,
)


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


@admin.register(IndexChangeEvent)
class IndexChangeEventAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "company",
        "effective_date",
        "status",
        "displacement",
        "monitoring_impact",
        "created_at",
    )
    list_filter = ("status", "displacement", "monitoring_impact")
    search_fields = ("company__display_name", "=company__cik")
    ordering = ("-effective_date",)
    date_hierarchy = "effective_date"
    list_select_related = ("company",)
    readonly_fields = tuple(field.name for field in IndexChangeEvent._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: IndexChangeEvent | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: IndexChangeEvent | None = None
    ) -> bool:
        return False


@admin.register(IndexChangeLeg)
class IndexChangeLegAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "effective_date",
        "index",
        "security_listing",
        "action",
        "announcement_date",
        "detected_at",
    )
    list_filter = ("action", "index")
    search_fields = (
        "security_listing__ticker",
        "=event__id",
        "index__code",
    )
    ordering = ("-effective_date",)
    date_hierarchy = "effective_date"
    list_select_related = ("index", "security_listing", "security_listing__company")
    readonly_fields = tuple(field.name for field in IndexChangeLeg._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: IndexChangeLeg | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: IndexChangeLeg | None = None
    ) -> bool:
        return False


@admin.register(IndexChangeCorrelation)
class IndexChangeCorrelationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "earlier_event",
        "later_event",
        "status",
        "displacement",
        "monitoring_impact",
        "created_at",
    )
    list_filter = ("status", "displacement", "monitoring_impact")
    search_fields = ("=earlier_event__id", "=later_event__id")
    ordering = ("-created_at",)
    list_select_related = (
        "earlier_event__company",
        "later_event__company",
    )
    readonly_fields = tuple(field.name for field in IndexChangeCorrelation._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: IndexChangeCorrelation | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: IndexChangeCorrelation | None = None
    ) -> bool:
        return False
