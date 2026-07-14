import pytest
from django.contrib.admin.sites import AdminSite
from django.http import HttpRequest

from indexes.admin import MarketIndexAdmin
from indexes.models import MarketIndex


class TestMarketIndexAdmin:
    @pytest.fixture
    def admin(self) -> MarketIndexAdmin:
        return MarketIndexAdmin(model=MarketIndex, admin_site=AdminSite(name="test"))

    @pytest.fixture
    def http_request(self) -> HttpRequest:
        return HttpRequest()

    def test_admin_no_add_permission(
        self, admin: MarketIndexAdmin, http_request: HttpRequest
    ) -> None:
        assert admin.has_add_permission(http_request) is False

    def test_admin_no_change_permission(
        self, admin: MarketIndexAdmin, http_request: HttpRequest
    ) -> None:
        assert admin.has_change_permission(http_request) is False

    def test_admin_no_delete_permission(
        self, admin: MarketIndexAdmin, http_request: HttpRequest
    ) -> None:
        assert admin.has_delete_permission(http_request) is False

    def test_all_fields_readonly(self, admin: MarketIndexAdmin) -> None:
        expected = frozenset(field.name for field in MarketIndex._meta.fields)
        actual = frozenset(admin.readonly_fields)
        assert expected == actual

    def test_list_display(self, admin: MarketIndexAdmin) -> None:
        assert "code" in admin.list_display
        assert "name" in admin.list_display
        assert "index_group" in admin.list_display
        assert "is_enabled" in admin.list_display

    def test_search_fields(self, admin: MarketIndexAdmin) -> None:
        assert "code" in admin.search_fields
        assert "name" in admin.search_fields

    def test_list_filter(self, admin: MarketIndexAdmin) -> None:
        assert "index_group" in admin.list_filter
        assert "is_enabled" in admin.list_filter
