"""Stage 5.2 /watchlist page tests: permissions, sorting, HTMX, isolation."""

from __future__ import annotations

from datetime import date

import pytest
from django.test import Client

from tests.pages.helpers import (
    AS_OF,
    add_item,
    company_with_listing,
    login_client,
    make_user,
    scheduled_event,
)
from watchlists.models import WatchlistItem

pytestmark = pytest.mark.django_db


def _patch_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("config.pages.business_date", lambda: AS_OF)


def test_watchlist_requires_login() -> None:
    response = Client().get("/watchlist/")
    assert response.status_code == 302
    assert response["Location"].startswith("/admin/login/")
    assert "next=/watchlist/" in response["Location"]


def test_watchlist_renders_items_and_sort_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("render")
    company, _listing = company_with_listing("REND")
    add_item(user, company)
    content = login_client(user).get("/watchlist/").content.decode()

    assert company.display_name in content
    assert "Sort by:" in content
    assert "Next earnings" in content
    assert "Reminder priority (S before A)" in content
    assert "America/New_York (ET)" in content
    assert "Add company by ticker" in content


def test_watchlist_empty_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("empty")
    content = login_client(user).get("/watchlist/").content.decode()
    assert "Your watchlist is empty." in content


def test_watchlist_hides_other_users_items(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    owner = make_user("owner")
    other = make_user("other")
    owned_company, _listing = company_with_listing("MINE")
    other_company, _other_listing = company_with_listing("YOURS")
    add_item(owner, owned_company)
    add_item(other, other_company)

    content = login_client(owner).get("/watchlist/").content.decode()
    assert owned_company.display_name in content
    assert other_company.display_name not in content


def test_watchlist_sort_by_next_earnings(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("sort-next")
    later_company, _l = company_with_listing("LATER")
    earlier_company, _e = company_with_listing("EARLY")
    none_company, _n = company_with_listing("NOEVENT")
    scheduled_event(later_company, release_date=date(2026, 10, 25))
    scheduled_event(earlier_company, release_date=date(2026, 10, 22))
    for company in (later_company, earlier_company, none_company):
        add_item(user, company)

    content = login_client(user).get("/watchlist/?sort=next_earnings").content.decode()
    assert (
        content.index(earlier_company.display_name)
        < content.index(later_company.display_name)
        < content.index(none_company.display_name)
    )


def test_watchlist_sort_by_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("sort-prio")
    normal_early, _e = company_with_listing("NEARLY")
    important_late, _l = company_with_listing("ILATE")
    scheduled_event(normal_early, release_date=date(2026, 10, 21))
    scheduled_event(important_late, release_date=date(2026, 10, 28))
    add_item(user, normal_early, priority_level=WatchlistItem.PriorityLevel.NORMAL)
    add_item(user, important_late, priority_level=WatchlistItem.PriorityLevel.IMPORTANT)

    by_next = login_client(user).get("/watchlist/?sort=next_earnings").content.decode()
    assert by_next.index(normal_early.display_name) < by_next.index(important_late.display_name)

    by_priority = login_client(user).get("/watchlist/?sort=priority").content.decode()
    assert by_priority.index(important_late.display_name) < by_priority.index(
        normal_early.display_name
    )


def test_watchlist_invalid_sort_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("sort-bad")
    company, _listing = company_with_listing("FBCK")
    add_item(user, company)
    response = login_client(user).get("/watchlist/?sort=nonsense")
    assert response.status_code == 200
    assert "sort=nonsense" not in response.content.decode()


def test_watchlist_add_non_js_redirects_and_creates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("add")
    company, _listing = company_with_listing("ADDA")
    client = login_client(user)
    response = client.post(
        "/watchlist/add/",
        {"ticker": "ADDA", "priority_level": "important"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/watchlist/?sort=next_earnings"
    item = WatchlistItem.objects.get(user=user, company=company)
    assert item.is_active is True
    assert item.priority_level == WatchlistItem.PriorityLevel.IMPORTANT


def test_watchlist_add_twice_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("add-twice")
    company, _listing = company_with_listing("DUPA")
    client = login_client(user)
    for _ in range(2):
        response = client.post("/watchlist/add/", {"ticker": "DUPA", "priority_level": "normal"})
        assert response.status_code == 302
    assert WatchlistItem.objects.filter(user=user, company=company).count() == 1


def test_watchlist_add_requires_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("csrf")
    company_with_listing("CSRF")
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    response = client.post("/watchlist/add/", {"ticker": "CSRF"})
    assert response.status_code == 403


def test_watchlist_add_unknown_ticker_shows_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("add-unknown")
    client = login_client(user)
    response = client.post("/watchlist/add/", {"ticker": "NOPE", "priority_level": "normal"})
    assert response.status_code == 302
    assert "error=company_not_found" in response["Location"]

    follow_up = client.get(response["Location"])
    assert b"No company with an effective listing matches that ticker." in follow_up.content
    assert not WatchlistItem.objects.filter(user=user).exists()


def test_watchlist_add_empty_ticker_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("add-empty")
    client = login_client(user)
    response = client.post("/watchlist/add/", {"ticker": "  ", "priority_level": "normal"})
    assert "error=invalid_ticker" in response["Location"]


def test_watchlist_add_htmx_returns_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("add-htmx")
    company, _listing = company_with_listing("HTMX")
    client = login_client(user)
    response = client.post(
        "/watchlist/add/",
        {"ticker": "HTMX", "priority_level": "normal"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    content = response.content.decode()
    assert "<html" not in content.lower()
    assert 'id="watchlist-results"' in content
    assert company.display_name in content

    failure = client.post(
        "/watchlist/add/",
        {"ticker": "MISSING", "priority_level": "normal"},
        HTTP_HX_REQUEST="true",
    )
    assert failure.status_code == 400
    assert "No company with an effective listing matches that ticker." in (failure.content.decode())


def test_watchlist_htmx_get_returns_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("get-htmx")
    company, _listing = company_with_listing("HGET")
    add_item(user, company)
    response = login_client(user).get("/watchlist/?sort=priority", HTTP_HX_REQUEST="true")
    content = response.content.decode()
    assert "<html" not in content.lower()
    assert 'id="watchlist-results"' in content


def test_watchlist_remove_own_item(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("remove")
    company, _listing = company_with_listing("RMME")
    item = add_item(user, company)
    response = login_client(user).post(
        "/watchlist/remove/", {"item_id": str(item.id), "sort": "priority"}
    )
    assert response.status_code == 302
    assert response["Location"] == "/watchlist/?sort=priority"
    item.refresh_from_db()
    assert item.is_active is False
    assert item.deactivated_at is not None


def test_watchlist_remove_foreign_item_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    owner = make_user("remove-owner")
    attacker = make_user("remove-attacker")
    company, _listing = company_with_listing("ATTK")
    item = add_item(owner, company)
    response = login_client(attacker).post("/watchlist/remove/", {"item_id": str(item.id)})
    assert "error=item_not_found" in response["Location"]
    item.refresh_from_db()
    assert item.is_active is True


def test_watchlist_priority_and_alerts_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("flows")
    company, _listing = company_with_listing("FLOW")
    item = add_item(user, company)
    client = login_client(user)

    promoted = client.post(
        "/watchlist/priority/",
        {"item_id": str(item.id), "priority_level": "important", "sort": "priority"},
    )
    assert promoted.status_code == 302
    item.refresh_from_db()
    assert item.priority_level == WatchlistItem.PriorityLevel.IMPORTANT

    muted = client.post("/watchlist/alerts/", {"item_id": str(item.id), "alerts_enabled": "off"})
    assert muted.status_code == 302
    item.refresh_from_db()
    assert item.alerts_enabled is False

    re_enabled = client.post(
        "/watchlist/alerts/",
        {"item_id": str(item.id), "alerts_enabled": "on"},
        HTTP_HX_REQUEST="true",
    )
    assert re_enabled.status_code == 200
    item.refresh_from_db()
    assert item.alerts_enabled is True


def test_watchlist_invalid_priority_value_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("bad-prio")
    company, _listing = company_with_listing("BADP")
    item = add_item(user, company)
    response = login_client(user).post(
        "/watchlist/priority/",
        {"item_id": str(item.id), "priority_level": "urgent"},
    )
    assert "error=invalid_input" in response["Location"]
    item.refresh_from_db()
    assert item.priority_level == WatchlistItem.PriorityLevel.NORMAL


def test_watchlist_invalid_item_id_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("bad-id")
    response = login_client(user).post("/watchlist/remove/", {"item_id": "not-a-uuid"})
    assert "error=invalid_input" in response["Location"]


def test_watchlist_add_get_method_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("add-get")
    response = login_client(user).get("/watchlist/add/")
    assert response.status_code == 302
    assert response["Location"] == "/watchlist/"
