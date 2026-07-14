from __future__ import annotations

from datetime import date

import pytest
from django.contrib.admin.sites import AdminSite
from django.http import HttpRequest

from companies.models import Company, SecurityListing
from indexes.admin import IndexMembershipAdmin
from indexes.models import IndexMembership, MarketIndex


@pytest.fixture
def sp500(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="SP500")


@pytest.fixture
def company(db: object) -> Company:
    del db
    return Company.objects.create(
        legal_name="Test Corp",
        display_name="Test Corp",
        cik="0000000001",
    )


@pytest.fixture
def listing(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="TEST",
        exchange="NYSE",
        effective_from=date(2020, 1, 1),
    )


@pytest.fixture
def admin_site() -> AdminSite:
    return AdminSite()


class TestMembershipAdmin:
    def test_readonly(self, admin_site: AdminSite) -> None:
        admin = IndexMembershipAdmin(IndexMembership, admin_site)
        assert admin.has_add_permission(HttpRequest()) is False
        assert admin.has_change_permission(HttpRequest()) is False
        assert admin.has_delete_permission(HttpRequest()) is False

    def test_list_display_configured(
        self, admin_site: AdminSite, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del sp500
        admin = IndexMembershipAdmin(IndexMembership, admin_site)
        assert "index" in admin.list_display
        assert "security_listing" in admin.list_display
        assert "status" in admin.list_display

    def test_search_fields_configured(self, admin_site: AdminSite) -> None:
        admin = IndexMembershipAdmin(IndexMembership, admin_site)
        assert any("ticker" in f for f in admin.search_fields)
        assert any("code" in f for f in admin.search_fields)
