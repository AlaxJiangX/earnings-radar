from django.contrib import admin
from django.http import HttpRequest

from audit.models import DataSource, SyncRun


@admin.register(DataSource)
class DataSourceAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("key", "name", "source_type", "is_official", "is_enabled", "updated_at")
    list_filter = ("source_type", "is_official", "is_enabled")
    search_fields = ("key", "name", "provider_adapter", "base_url")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("key",)


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "job_type",
        "source",
        "status",
        "started_at",
        "finished_at",
        "fetched_count",
        "failed_count",
    )
    list_filter = ("status", "job_type", "source")
    search_fields = ("idempotency_key", "job_type", "error_summary")
    ordering = ("-started_at",)
    date_hierarchy = "started_at"
    readonly_fields = tuple(field.name for field in SyncRun._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: SyncRun | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: SyncRun | None = None) -> bool:
        return False
