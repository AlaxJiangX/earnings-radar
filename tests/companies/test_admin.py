from datetime import date

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from accounts.models import User
from companies.admin import CompanyAdmin, SecurityListingAdmin
from companies.models import Company, SecurityListing


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("model", "admin_class"),
    ((Company, CompanyAdmin), (SecurityListing, SecurityListingAdmin)),
)
def test_company_identity_admins_are_read_only(
    model: type[Company] | type[SecurityListing],
    admin_class: type[CompanyAdmin] | type[SecurityListingAdmin],
) -> None:
    model_admin = admin_class(model, AdminSite())
    request = RequestFactory().get("/admin/companies/")
    request.user = User.objects.create_superuser(
        email=f"{model._meta.model_name}-admin@example.com",
        password="fixture-password-only",
    )

    assert set(model_admin.readonly_fields) == {field.name for field in model._meta.fields}
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


def test_company_admin_search_and_filters_cover_identity_fields() -> None:
    company_admin = CompanyAdmin(Company, AdminSite())
    listing_admin = SecurityListingAdmin(SecurityListing, AdminSite())

    assert {"legal_name", "display_name", "=cik"} <= set(company_admin.search_fields)
    assert {"ticker", "exchange", "=company__cik"} <= set(listing_admin.search_fields)
    assert {"monitoring_status", "issuer_type"} <= set(company_admin.list_filter)
    assert {"exchange", "security_type"} <= set(listing_admin.list_filter)


@pytest.mark.django_db
def test_admin_search_finds_nvda_by_ticker_and_english_company_name() -> None:
    company = Company.objects.create(
        legal_name="NVIDIA Corporation",
        display_name="NVIDIA",
        cik="0001045810",
    )
    SecurityListing.objects.create(
        company=company,
        ticker="NVDA",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
    )
    request = RequestFactory().get("/admin/companies/")
    company_admin = CompanyAdmin(Company, AdminSite())
    listing_admin = SecurityListingAdmin(SecurityListing, AdminSite())

    companies, _ = company_admin.get_search_results(request, Company.objects.all(), "NVIDIA")
    listings, _ = listing_admin.get_search_results(request, SecurityListing.objects.all(), "NVDA")

    assert list(companies) == [company]
    assert list(listings) == [company.security_listings.get()]
