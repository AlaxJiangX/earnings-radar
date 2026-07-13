import json

from django.contrib import admin
from django.http import HttpRequest

from audit.models import (
    AuditRecord,
    DataChange,
    DataSource,
    RawDataObservation,
    RawDataRecord,
    SourceEvidence,
    SyncRun,
)


def _json_preview(value: object, *, maximum_length: int = 240) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(rendered) <= maximum_length:
        return rendered
    return f"{rendered[: maximum_length - 3]}..."


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


@admin.register(RawDataRecord)
class RawDataRecordAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "source",
        "content_hash",
        "payload_size_bytes",
        "parser_status",
        "fetched_at",
    )
    list_filter = ("source", "parser_status", "content_type")
    search_fields = ("content_hash", "request_fingerprint", "source_url")
    ordering = ("-fetched_at",)
    date_hierarchy = "fetched_at"
    exclude = ("payload",)
    readonly_fields = tuple(
        field.name for field in RawDataRecord._meta.fields if field.name != "payload"
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: RawDataRecord | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: RawDataRecord | None = None,
    ) -> bool:
        return False


@admin.register(RawDataObservation)
class RawDataObservationAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("sync_run", "raw_data_record", "observed_at")
    list_filter = ("raw_data_record__source",)
    search_fields = (
        "sync_run__idempotency_key",
        "raw_data_record__content_hash",
    )
    ordering = ("-observed_at",)
    date_hierarchy = "observed_at"
    readonly_fields = tuple(field.name for field in RawDataObservation._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: RawDataObservation | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: RawDataObservation | None = None,
    ) -> bool:
        return False


@admin.register(SourceEvidence)
class SourceEvidenceAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "target_type",
        "target_id",
        "field_name",
        "source_key",
        "is_official",
        "confidence",
        "observed_at",
    )
    list_filter = ("target_type", "is_official", "raw_data_record__source")
    search_fields = (
        "=evidence_key",
        "=target_id",
        "field_name",
        "normalizer_version",
        "raw_data_record__source__key",
    )
    ordering = ("-observed_at",)
    date_hierarchy = "observed_at"
    list_select_related = ("raw_data_record__source", "sync_run")
    readonly_fields = tuple(field.name for field in SourceEvidence._meta.fields)

    @admin.display(description="Source", ordering="raw_data_record__source__key")
    def source_key(self, obj: SourceEvidence) -> str:
        return obj.raw_data_record.source.key

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: SourceEvidence | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: SourceEvidence | None = None,
    ) -> bool:
        return False


@admin.register(DataChange)
class DataChangeAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "target_type",
        "target_id",
        "field_name",
        "actor_user",
        "sync_run",
        "changed_at",
    )
    list_filter = ("target_type", "changed_at", "actor_user", "sync_run")
    search_fields = ("=change_key", "=target_id", "field_name", "origin_key", "reason")
    ordering = ("-changed_at",)
    date_hierarchy = "changed_at"
    list_select_related = ("actor_user", "sync_run", "source_evidence")
    exclude = ("old_value", "new_value")
    readonly_fields = tuple(
        field.name
        for field in DataChange._meta.fields
        if field.name not in {"old_value", "new_value"}
    ) + ("old_value_preview", "new_value_preview")

    @admin.display(description="Old value preview")
    def old_value_preview(self, obj: DataChange) -> str:
        return _json_preview(obj.old_value)

    @admin.display(description="New value preview")
    def new_value_preview(self, obj: DataChange) -> str:
        return _json_preview(obj.new_value)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: DataChange | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: DataChange | None = None,
    ) -> bool:
        return False


@admin.register(AuditRecord)
class AuditRecordAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "action",
        "target_type",
        "target_id",
        "actor_user",
        "sync_run",
        "request_id",
        "created_at",
    )
    list_filter = ("action", "target_type", "created_at", "actor_user", "sync_run")
    search_fields = ("=audit_key", "=target_id", "request_id", "reason")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_select_related = ("actor_user", "sync_run")
    exclude = ("before", "after")
    readonly_fields = tuple(
        field.name for field in AuditRecord._meta.fields if field.name not in {"before", "after"}
    ) + ("before_preview", "after_preview")

    @admin.display(description="Before preview")
    def before_preview(self, obj: AuditRecord) -> str:
        return _json_preview(obj.before)

    @admin.display(description="After preview")
    def after_preview(self, obj: AuditRecord) -> str:
        return _json_preview(obj.after)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: AuditRecord | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: AuditRecord | None = None,
    ) -> bool:
        return False
