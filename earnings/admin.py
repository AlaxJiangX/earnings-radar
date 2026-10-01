from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from earnings.models import (
    EarningsDateChange,
    EarningsEvent,
    FilingEarningsDecision,
    FilingEarningsLink,
    InvestorRelationsDecision,
    InvestorRelationsObservation,
)


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


@admin.register(EarningsDateChange)
class EarningsDateChangeAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "earnings_event",
        "field_name",
        "change_kind",
        "old_precision",
        "new_precision",
        "detected_at",
    )
    list_filter = ("field_name", "change_kind", "old_precision", "new_precision")
    search_fields = ("=earnings_event__id", "=data_change__change_key")
    ordering = ("-detected_at",)
    date_hierarchy = "detected_at"
    list_select_related = ("earnings_event", "data_change")
    readonly_fields = tuple(field.name for field in EarningsDateChange._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: EarningsDateChange | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: EarningsDateChange | None = None,
    ) -> bool:
        return False


@admin.register(FilingEarningsDecision)
class FilingEarningsDecisionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "filing",
        "relation_type",
        "decision_type",
        "status",
        "classification",
        "decision_source",
        "decided_at",
    )
    list_filter = (
        "relation_type",
        "decision_type",
        "status",
        "classification",
        "decision_source",
    )
    search_fields = (
        "=filing__accession_number",
        "=decision_key",
        "=request_id",
    )
    ordering = ("-decided_at",)
    date_hierarchy = "decided_at"
    list_select_related = ("filing", "target_event", "actor_user", "sync_run")
    readonly_fields = tuple(field.name for field in FilingEarningsDecision._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: FilingEarningsDecision | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: FilingEarningsDecision | None = None,
    ) -> bool:
        return False


@admin.register(FilingEarningsLink)
class FilingEarningsLinkAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "filing",
        "earnings_event",
        "relation_type",
        "release_filing_classification",
        "review_status",
        "confidence",
    )
    list_filter = (
        "relation_type",
        "release_filing_classification",
        "review_status",
        "confidence",
    )
    search_fields = (
        "=filing__accession_number",
        "=earnings_event__id",
        "=current_decision__decision_key",
    )
    ordering = ("-created_at",)
    list_select_related = ("filing", "earnings_event", "current_decision", "reviewed_by")
    readonly_fields = tuple(field.name for field in FilingEarningsLink._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: FilingEarningsLink | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: FilingEarningsLink | None = None,
    ) -> bool:
        return False


@admin.register(InvestorRelationsObservation)
class InvestorRelationsObservationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "company",
        "item_type",
        "period_type",
        "period_end_date",
        "source",
        "raw_position",
        "created_at",
    )
    list_filter = ("item_type", "period_type", "source")
    search_fields = (
        "=company__cik",
        "company__display_name",
        "source_event_identity",
        "=raw_data_record__content_hash",
    )
    ordering = ("-created_at",)
    list_select_related = ("company", "source", "raw_data_record")
    readonly_fields = tuple(field.name for field in InvestorRelationsObservation._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: InvestorRelationsObservation | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: InvestorRelationsObservation | None = None,
    ) -> bool:
        return False


@admin.register(InvestorRelationsDecision)
class InvestorRelationsDecisionAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "observation",
        "target_event",
        "decision_type",
        "status",
        "decided_at",
    )
    list_filter = ("decision_type", "status")
    search_fields = (
        "=decision_key",
        "=observation__id",
        "=target_event__id",
    )
    ordering = ("-decided_at",)
    list_select_related = ("observation", "target_event", "actor_user", "sync_run")
    readonly_fields = tuple(field.name for field in InvestorRelationsDecision._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: InvestorRelationsDecision | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: InvestorRelationsDecision | None = None,
    ) -> bool:
        return False
