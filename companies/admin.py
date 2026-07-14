from django.contrib import admin
from django.http import HttpRequest

from companies.models import Company, SecurityListing


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("display_name", "cik", "issuer_type", "monitoring_status", "updated_at")
    list_filter = ("issuer_type", "monitoring_status", "country_code")
    search_fields = ("legal_name", "display_name", "=cik")
    ordering = ("display_name",)
    readonly_fields = tuple(field.name for field in Company._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Company | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Company | None = None) -> bool:
        return False


@admin.register(SecurityListing)
class SecurityListingAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = (
        "ticker",
        "exchange",
        "company",
        "security_type",
        "is_primary",
        "effective_from",
        "effective_to",
    )
    list_filter = ("exchange", "security_type", "is_primary")
    search_fields = (
        "ticker",
        "exchange",
        "company__legal_name",
        "company__display_name",
        "=company__cik",
    )
    ordering = ("exchange", "ticker", "effective_from")
    list_select_related = ("company", "source_evidence")
    readonly_fields = tuple(field.name for field in SecurityListing._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: SecurityListing | None = None,
    ) -> bool:
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: SecurityListing | None = None,
    ) -> bool:
        return False
