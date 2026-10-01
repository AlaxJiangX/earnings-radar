from django.contrib import admin
from django.http import HttpRequest

from filings.models import Filing, FilingDocument


class ReadOnlyFilingAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False


@admin.register(Filing)
class FilingAdmin(ReadOnlyFilingAdmin):
    list_display = ("accession_number", "form_type", "company", "accepted_at")
    search_fields = ("=accession_number", "=company__cik", "company__display_name")
    list_filter = ("form_type",)
    readonly_fields = tuple(field.name for field in Filing._meta.fields)


@admin.register(FilingDocument)
class FilingDocumentAdmin(ReadOnlyFilingAdmin):
    list_display = ("filename", "filing", "document_type")
    search_fields = ("filename", "=filing__accession_number")
    readonly_fields = tuple(field.name for field in FilingDocument._meta.fields)
