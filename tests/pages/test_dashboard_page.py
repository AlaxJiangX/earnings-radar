"""Stage 5.2 /dashboard page tests: sections, 7-day window, isolation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from django.test import Client

from tests.pages.helpers import (
    AS_OF,
    add_item,
    company_with_listing,
    date_only_change,
    login_client,
    make_filing,
    make_index_change,
    make_user,
    scheduled_event,
)

pytestmark = pytest.mark.django_db


def _patch_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("config.pages.business_date", lambda: AS_OF)


def test_dashboard_requires_login() -> None:
    response = Client().get("/dashboard/")
    assert response.status_code == 302
    assert response["Location"].startswith("/admin/login/")
    assert "next=/dashboard/" in response["Location"]


def test_dashboard_empty_sections_and_notifications_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("dash-empty")
    content = login_client(user).get("/dashboard/").content.decode()
    assert "Important watchlist (S)" in content
    assert "Normal watchlist (A)" in content
    assert "next 7 days" in content
    assert "No watchlist earnings events in the next 7 days." in content
    assert "Notifications are not available yet" in content


def test_dashboard_groups_priority_and_shows_next_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("dash-groups")
    important_company, _i = company_with_listing("IMPT")
    normal_company, _n = company_with_listing("NORM")
    scheduled_event(important_company, release_date=AS_OF + timedelta(days=3))
    scheduled_event(normal_company, release_date=AS_OF + timedelta(days=5))
    add_item(user, important_company, priority_level="important")
    add_item(user, normal_company, priority_level="normal")

    content = login_client(user).get("/dashboard/").content.decode()
    important_section = content.split("Normal watchlist (A)")[0]
    assert important_company.display_name in important_section
    assert normal_company.display_name not in important_section
    normal_section = content.split("Normal watchlist (A)")[1]
    assert normal_company.display_name in normal_section


def test_dashboard_upcoming_window_includes_7th_day_excludes_8th(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("dash-window")
    inside_company, _in = company_with_listing("INDAY")
    edge_company, _edge = company_with_listing("DAY7")
    outside_company, _out = company_with_listing("DAY8")
    scheduled_event(inside_company, release_date=AS_OF)
    scheduled_event(edge_company, release_date=AS_OF + timedelta(days=6))
    scheduled_event(outside_company, release_date=AS_OF + timedelta(days=7))
    for company in (inside_company, edge_company, outside_company):
        add_item(user, company)

    content = login_client(user).get("/dashboard/").content.decode()
    upcoming_start = content.split("Watchlist earnings in the next 7 days")[1]
    upcoming_section = upcoming_start.split("Recent earnings date changes")[0]
    assert inside_company.display_name in upcoming_section
    assert edge_company.display_name in upcoming_section
    assert outside_company.display_name not in upcoming_section
    assert f"{AS_OF.isoformat()} to {(AS_OF + timedelta(days=6)).isoformat()}" in content


def test_dashboard_cross_user_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    alice = make_user("dash-alice")
    bob = make_user("dash-bob")
    alice_company, _a = company_with_listing("ALCO")
    bob_company, _b = company_with_listing("BBCO")
    scheduled_event(alice_company, release_date=AS_OF + timedelta(days=1))
    scheduled_event(bob_company, release_date=AS_OF + timedelta(days=2))
    add_item(alice, alice_company)
    add_item(bob, bob_company)

    alice_content = login_client(alice).get("/dashboard/").content.decode()
    assert alice_company.display_name in alice_content
    assert bob_company.display_name not in alice_content

    bob_content = login_client(bob).get("/dashboard/").content.decode()
    assert bob_company.display_name in bob_content
    assert alice_company.display_name not in bob_content


def test_dashboard_recent_date_changes_section(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("dash-changes")
    company, _listing = company_with_listing("CHNG")
    add_item(user, company)
    event = scheduled_event(company, release_date=AS_OF + timedelta(days=5))
    date_only_change(
        event,
        old_date=date(2026, 10, 18),
        new_date=date(2026, 10, 25),
        detected_at=datetime(2026, 10, 19, 10, 0, tzinfo=UTC),
        suffix="dash-change",
    )

    content = login_client(user).get("/dashboard/").content.decode()
    assert "Recent earnings date changes" in content
    assert "2026-10-18 → 2026-10-25" in content
    assert "2026-10-19 10:00" in content


def test_dashboard_recent_releases_section(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("dash-releases")
    company, _listing = company_with_listing("RELS")
    add_item(user, company)
    scheduled_event(
        company,
        release_date=AS_OF - timedelta(days=2),
        status="released",
        period_end_date=date(2026, 6, 30),
    )

    content = login_client(user).get("/dashboard/").content.decode()
    assert "Recent watchlist earnings releases" in content
    assert company.display_name in content
    assert "Released" in content


def test_dashboard_recent_filings_section(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("dash-filings")
    company, _listing = company_with_listing("FILG")
    add_item(user, company)
    make_filing(
        company,
        accepted_at=datetime(2026, 10, 19, 14, 30, tzinfo=UTC),
    )

    content = login_client(user).get("/dashboard/").content.decode()
    assert "Recent SEC filings" in content
    assert "8-K accepted 2026-10-19 14:30 UTC" in content
    assert "View on SEC EDGAR" in content


def test_dashboard_index_changes_section(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("dash-index")
    company, listing = company_with_listing("IDXC")
    add_item(user, company)
    make_index_change(
        company,
        effective_date=date(2026, 10, 15),
        listing=listing,
        action="added",
    )

    content = login_client(user).get("/dashboard/").content.decode()
    assert "Watchlist index changes" in content
    assert "added SP500" in content
