"""Stage 7.1 home page tests: nine MVP sections, semantics, isolation, access.

The home page is the PRD §10.1 composition page: every MVP block renders with
honest empty states, phase-2 blocks (market movers / Reddit) never appear, and
the watchlist block only ever shows the signed-in user's own data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

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
    make_membership,
    make_user,
    scheduled_event,
)

pytestmark = pytest.mark.django_db

_EMPTY_STATES = (
    "No earnings scheduled for today yet.",
    "No large-index earnings scheduled this week yet.",
    "Sign in to see upcoming earnings",
    "No recent earnings date changes yet.",
    "No recent SEC filings yet.",
    "No effective index changes yet.",
    "No announced index changes with a future effective date yet.",
)

_SECTION_HEADINGS = (
    "Global company search",
    "Today's earnings",
    "Important earnings this week",
    "Your watchlist: upcoming earnings",
    "Recent earnings date changes",
    "Recent SEC filings",
    "Recent index changes",
    "Upcoming index changes",
    "GitHub",
)


def _patch_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("config.pages.business_date", lambda: AS_OF)


def _home(client: Client) -> str:
    response = client.get("/")
    assert response.status_code == 200
    return response.content.decode()


def _section(content: str, heading: str, next_heading: str) -> str:
    """Return the page slice between two section headings (headings included)."""

    start = content.index(heading)
    end = content.index(next_heading)
    return content[start:end]


def test_home_anonymous_renders_all_mvp_sections_with_empty_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    content = _home(Client())

    for heading in _SECTION_HEADINGS:
        assert heading in content, f"missing MVP section heading: {heading}"
    for empty_state in _EMPTY_STATES:
        assert empty_state in content, f"missing honest empty state: {empty_state}"


def test_home_has_no_phase_two_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    content = _home(Client()).lower()

    assert "reddit" not in content
    assert "trending" not in content
    assert "market movers" not in content
    assert "market hot" not in content


def test_home_today_block_lists_all_companies_important_block_only_large_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    sp500_company, sp500_listing = company_with_listing("HRSP")
    dow_company, dow_listing = company_with_listing("HDOW")
    russell_company, russell_listing = company_with_listing("HRUS")
    plain_company, _plain = company_with_listing("HPLN")
    next_week_company, next_week_listing = company_with_listing("HNXT")
    make_membership(sp500_listing, index_code="SP500")
    make_membership(dow_listing, index_code="DJIA")
    make_membership(russell_listing, index_code="RUSSELL2000")
    make_membership(next_week_listing, index_code="SP500")
    scheduled_event(sp500_company, release_date=AS_OF)
    scheduled_event(dow_company, release_date=AS_OF + timedelta(days=2))
    scheduled_event(russell_company, release_date=AS_OF)
    scheduled_event(plain_company, release_date=AS_OF)
    scheduled_event(next_week_company, release_date=AS_OF + timedelta(days=8))

    content = _home(Client())
    today_section = _section(content, "Today's earnings", "Important earnings this week")
    important_section = _section(
        content, "Important earnings this week", "Your watchlist: upcoming earnings"
    )

    for company in (sp500_company, russell_company, plain_company):
        assert company.display_name in today_section
    assert next_week_company.display_name not in today_section

    assert sp500_company.display_name in important_section
    assert dow_company.display_name in important_section
    assert russell_company.display_name not in important_section
    assert plain_company.display_name not in important_section
    assert next_week_company.display_name not in important_section


def test_home_anonymous_watchlist_prompt_hides_signed_in_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    user = make_user("home-anon")
    company, _listing = company_with_listing("HANO")
    scheduled_event(company, release_date=AS_OF + timedelta(days=1))
    add_item(user, company)

    content = _home(Client())
    watchlist_section = _section(
        content, "Your watchlist: upcoming earnings", "Recent earnings date changes"
    )
    assert "Sign in to see upcoming earnings" in watchlist_section
    assert company.display_name not in content


def test_home_watchlist_block_is_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    alice = make_user("home-alice")
    bob = make_user("home-bob")
    alice_company, _a = company_with_listing("HALC")
    bob_company, _b = company_with_listing("HBBC")
    scheduled_event(alice_company, release_date=AS_OF + timedelta(days=1))
    scheduled_event(bob_company, release_date=AS_OF + timedelta(days=2))
    add_item(alice, alice_company)
    add_item(bob, bob_company)

    def watchlist_section(content: str) -> str:
        return _section(
            content, "Your watchlist: upcoming earnings", "Recent earnings date changes"
        )

    alice_content = watchlist_section(_home(login_client(alice)))
    assert alice_company.display_name in alice_content
    assert bob_company.display_name not in alice_content

    bob_content = watchlist_section(_home(login_client(bob)))
    assert bob_company.display_name in bob_content
    assert alice_company.display_name not in bob_content


def test_home_watchlist_empty_states_for_signed_in_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    no_items_user = make_user("home-empty")
    no_events_user = make_user("home-quiet")
    company, _listing = company_with_listing("HQIT")
    scheduled_event(company, release_date=AS_OF + timedelta(days=20))
    add_item(no_events_user, company)

    def watchlist_section(content: str) -> str:
        return _section(
            content, "Your watchlist: upcoming earnings", "Recent earnings date changes"
        )

    empty_content = watchlist_section(_home(login_client(no_items_user)))
    assert "Your watchlist is empty." in empty_content

    quiet_content = watchlist_section(_home(login_client(no_events_user)))
    assert "No watchlist earnings events in the next 7 days." in quiet_content


def test_home_recent_and_upcoming_activity_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    change_company, change_listing = company_with_listing("HCHG")
    past_company, past_listing = company_with_listing("HPRI")
    future_company, future_listing = company_with_listing("HFUT")
    event = scheduled_event(change_company, release_date=AS_OF + timedelta(days=5))
    date_only_change(
        event,
        old_date=date(2026, 10, 10),
        new_date=AS_OF + timedelta(days=5),
        detected_at=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
        suffix="home-change",
    )
    make_filing(change_company, accepted_at=datetime(2026, 10, 20, 9, 30, tzinfo=UTC))
    make_index_change(past_company, effective_date=AS_OF - timedelta(days=1), listing=past_listing)
    make_index_change(
        future_company, effective_date=AS_OF + timedelta(days=3), listing=future_listing
    )

    content = _home(Client())
    date_change_section = _section(content, "Recent earnings date changes", "Recent SEC filings")
    filing_section = _section(content, "Recent SEC filings", "Recent index changes")
    recent_section = _section(content, "Recent index changes", "Upcoming index changes")
    upcoming_section = content[content.index("Upcoming index changes") :]

    assert change_company.display_name in date_change_section
    assert change_company.display_name in filing_section
    assert "View on SEC EDGAR" in filing_section
    assert past_company.display_name in recent_section
    assert future_company.display_name not in recent_section
    assert future_company.display_name in upcoming_section
    assert past_company.display_name not in upcoming_section


def test_home_github_link_uses_setting(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    _patch_as_of(monkeypatch)
    settings.GITHUB_URL = "https://github.com/example/earnings-radar-fork"
    content = _home(Client())

    assert content.count('href="https://github.com/example/earnings-radar-fork"') >= 2


def test_public_pages_are_anonymously_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    company, listing = company_with_listing("HPUB")
    make_membership(listing)
    scheduled_event(company, release_date=AS_OF)

    client = Client()
    public_paths = (
        "/",
        "/earnings/",
        "/companies/",
        "/companies/HPUB/",
        "/index-changes/",
        "/sources/",
    )
    for path in public_paths:
        assert client.get(path).status_code == 200, f"public page requires login: {path}"

    login_response = client.get("/dashboard/")
    assert login_response.status_code == 302
    assert "/admin/login/" in login_response["Location"]
