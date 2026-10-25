from __future__ import annotations

import uuid
from typing import cast

from django import forms
from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import URLPattern, path

from accounts.models import User
from companies.models import Company, SecurityListing
from companies.services import CompanyServiceError, update_company

# Only fields the manual-correction form may touch; CIK stays out because it
# is the stable company identity (AGENTS.md section 9), and monitoring_status
# is owned by the recalculation service, not by manual edits.
CORRECTABLE_COMPANY_FIELDS = (
    "legal_name",
    "display_name",
    "country_code",
    "issuer_type",
    "fiscal_year_end_month_day",
    "investor_relations_url",
)


class CompanyCorrectionForm(forms.Form):
    """Pre-filled diff form: only fields that differ reach the service."""

    legal_name = forms.CharField(max_length=255)
    display_name = forms.CharField(max_length=255)
    country_code = forms.CharField(max_length=2, required=False)
    issuer_type = forms.ChoiceField(choices=Company.IssuerType.choices)
    fiscal_year_end_month_day = forms.CharField(
        max_length=5,
        required=False,
        help_text="MM-DD, e.g. 12-31.",
    )
    investor_relations_url = forms.URLField(required=False, assume_scheme="https")
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=2000,
        help_text="Required. Recorded with the audit trail of this correction.",
    )

    def diff_against(self, company: Company) -> dict[str, object]:
        return {
            field: self.cleaned_data[field]
            for field in CORRECTABLE_COMPANY_FIELDS
            if self.cleaned_data[field] != getattr(company, field)
        }


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("display_name", "cik", "issuer_type", "monitoring_status", "updated_at")
    list_filter = ("issuer_type", "monitoring_status", "country_code")
    search_fields = ("legal_name", "display_name", "=cik")
    ordering = ("display_name",)
    readonly_fields = tuple(field.name for field in Company._meta.fields)
    actions = ("correct_companies",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Company | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Company | None = None) -> bool:
        return False

    def has_manual_correction_permission(self, request: HttpRequest) -> bool:
        return request.user.has_perm("companies.manual_correction")

    def get_urls(self) -> list[URLPattern]:
        urls = super().get_urls()
        custom = [
            path(
                "<uuid:company_id>/correct/",
                self.admin_site.admin_view(self.correct_view),
                name="companies_company_correct",
            ),
        ]
        return custom + urls

    @admin.action(
        description="Manually correct the selected company (requires a reason)",
        permissions=("manual_correction",),
    )
    def correct_companies(
        self,
        request: HttpRequest,
        queryset: QuerySet[Company],
    ) -> HttpResponse | None:
        if not request.user.has_perm("companies.manual_correction"):
            self.message_user(
                request,
                "You do not have permission to correct company data.",
                messages.ERROR,
            )
            return None
        if queryset.count() != 1:
            self.message_user(
                request,
                "Select exactly one company to correct.",
                messages.WARNING,
            )
            return None
        company = queryset.get()
        return redirect("admin:companies_company_correct", company_id=company.pk)

    def correct_view(self, request: HttpRequest, company_id: uuid.UUID) -> HttpResponse:
        actor = cast(User, request.user)
        if not request.user.has_perm("companies.manual_correction"):
            return HttpResponseForbidden("You do not have permission to correct company data.")
        company = get_object_or_404(Company, pk=company_id)
        initial = {field: getattr(company, field) for field in CORRECTABLE_COMPANY_FIELDS}
        if request.method == "POST":
            form = CompanyCorrectionForm(request.POST, initial=initial)
            if form.is_valid():
                changes = form.diff_against(company)
                if not changes:
                    form.add_error(None, "No field value changed; nothing to correct.")
                else:
                    try:
                        result = update_company(
                            company=company,
                            changes=changes,
                            actor_user=actor,
                            reason=form.cleaned_data["reason"],
                            request_id=f"admin-correction:{uuid.uuid4()}",
                            ip_address=request.META.get("REMOTE_ADDR"),
                        )
                    except CompanyServiceError as error:
                        form.add_error(None, str(error))
                    else:
                        if result.audit_record is not None:
                            self.message_user(
                                request,
                                "Correction recorded with full before/after audit trail.",
                                messages.SUCCESS,
                            )
                        else:
                            self.message_user(
                                request,
                                "Correction applied without value changes.",
                                messages.SUCCESS,
                            )
                        return redirect("admin:companies_company_changelist")
        else:
            form = CompanyCorrectionForm(initial=initial)
        context = {
            **self.admin_site.each_context(request),
            "form": form,
            "company": company,
            "title": f"Correct {company.display_name}",
        }
        return render(request, "admin/companies/company_correction_form.html", context)


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
