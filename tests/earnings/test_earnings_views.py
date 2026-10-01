from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from companies.models import Company, SecurityListing
from earnings.models import EarningsEvent
from earnings.presentation import (
    MARKET_TIMEZONE,
    build_event_display,
    business_date,
    window_bounds,
)
from indexes.models import IndexMembership, MarketIndex
from tests.earnings.helpers import make_company, make_event, make_source_evidence

AS_OF = date(2026, 10, 1)


def _patch_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("earnings.views.business_date", lambda: AS_OF)
    monkeypatch.setattr("companies.views.business_date", lambda: AS_OF)


def _company_with_listing(
    *,
    ticker: str,
    exchange: str = "NASDAQ",
    effective_from: date = date(2026, 1, 1),
) -> tuple[Company, SecurityListing]:
    company = make_company(ticker)
    listing = SecurityListing.objects.create(
        company=company,
        ticker=ticker,
        exchange=exchange,
        security_name=f"{company.display_name} {ticker}",
        security_type="common_stock",
        effective_from=effective_from,
    )
    return company, listing


def _add_index_membership(listing: SecurityListing, code: str = "SP500") -> None:
    index = MarketIndex.objects.get(code=code)
    IndexMembership.objects.create(
        index=index,
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )


def _date_only_event(
    company: Company,
    *,
    release_date: date,
    status: str = "scheduled_estimated",
    session: str = "unknown",
    **overrides: object,
) -> EarningsEvent:
    return make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        status=status,
        release_session=session,
        estimated_release_date=release_date,
        estimated_release_precision="date_only",
        **overrides,
    )


def _candidate_event(company: Company, *, release_date: date) -> EarningsEvent:
    return make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type=None,
        identity_status="candidate",
        identity_key=None,
        identity_rule_version=None,
        estimated_release_date=release_date,
        estimated_release_precision="date_only",
    )


@pytest.mark.django_db
def test_earnings_page_renders_and_has_filters() -> None:
    response = Client().get("/earnings/")
    assert response.status_code == 200
    content = response.content.decode()
    assert "Earnings Calendar" in content
    assert 'name="window"' in content
    assert 'name="status"' in content
    assert 'name="session"' in content
    assert 'name="index"' in content
    assert "America/New_York (ET)" in content


@pytest.mark.django_db
def test_earnings_window_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    today_company, _today_listing = _company_with_listing(ticker="TODAY")
    week_company, _week_listing = _company_with_listing(ticker="WEEK")
    later_company, _later_listing = _company_with_listing(ticker="LATER")
    _date_only_event(today_company, release_date=AS_OF)
    _date_only_event(week_company, release_date=date(2026, 10, 4))
    _date_only_event(later_company, release_date=date(2026, 10, 30))
    client = Client()

    today = client.get("/earnings/?window=today").content.decode()
    assert today_company.display_name in today
    assert week_company.display_name not in today
    assert later_company.display_name not in today

    week = client.get("/earnings/?window=week").content.decode()
    assert week_company.display_name in week
    assert later_company.display_name not in week

    next30 = client.get("/earnings/?window=next30").content.decode()
    assert today_company.display_name in next30
    assert later_company.display_name in next30


@pytest.mark.django_db
def test_earnings_status_session_and_index_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    estimated_company, estimated_listing = _company_with_listing(ticker="EST")
    confirmed_company, confirmed_listing = _company_with_listing(ticker="CONF")
    outside_company, _outside_listing = _company_with_listing(ticker="OUT")
    _date_only_event(estimated_company, release_date=AS_OF, session="pre_market")
    make_event(
        company=confirmed_company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        status="scheduled_confirmed",
        confirmed_release_date=AS_OF,
        confirmed_release_precision="date_only",
        release_session="after_market",
    )
    _add_index_membership(estimated_listing)
    _add_index_membership(confirmed_listing)
    client = Client()

    confirmed = client.get("/earnings/?status=scheduled_confirmed").content.decode()
    assert confirmed_company.display_name in confirmed
    assert estimated_company.display_name not in confirmed

    pre_market = client.get("/earnings/?session=pre_market").content.decode()
    assert estimated_company.display_name in pre_market
    assert confirmed_company.display_name not in pre_market

    index = client.get("/earnings/?index=SP500").content.decode()
    assert estimated_company.display_name in index
    assert outside_company.display_name not in index

    combined = client.get(
        "/earnings/?window=today&status=scheduled_estimated&session=pre_market&index=SP500"
    ).content.decode()
    assert estimated_company.display_name in combined
    assert confirmed_company.display_name not in combined
    assert outside_company.display_name not in combined


@pytest.mark.django_db
def test_earnings_ordering_and_estimated_confirmed_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    later_company, _later_listing = _company_with_listing(ticker="LATER")
    earlier_company, _earlier_listing = _company_with_listing(ticker="EARLY")
    _date_only_event(later_company, release_date=date(2026, 10, 3))
    make_event(
        company=earlier_company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        status="scheduled_confirmed",
        confirmed_release_date=date(2026, 10, 2),
        confirmed_release_precision="date_only",
    )
    content = Client().get("/earnings/?window=next30").content.decode()
    assert content.index(earlier_company.display_name) < content.index(later_company.display_name)
    assert "Confirmed" in content
    assert "Estimated" in content


@pytest.mark.django_db
def test_candidate_visibility_respects_license_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    company, _listing = _company_with_listing(ticker="CAND")
    _candidate_event(company, release_date=AS_OF)
    client = Client()
    anonymous = client.get("/earnings/?window=today").content.decode()
    assert company.display_name not in anonymous
    assert "Candidate / Incomplete" not in anonymous

    user = User.objects.create_user(email="owner@example.com", password="fixture-password")
    client.force_login(user)
    authenticated = client.get("/earnings/?window=today").content.decode()
    assert company.display_name in authenticated
    assert "Candidate / Incomplete" in authenticated


@pytest.mark.django_db
def test_earnings_empty_state_and_htmx_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    client = Client()
    empty = client.get("/earnings/?window=today").content.decode()
    assert "No earnings events are scheduled in this window." in empty

    partial = client.get("/earnings/?window=today", HTTP_HX_REQUEST="true").content.decode()
    assert "<html" not in partial.lower()
    assert 'id="earnings-results"' in partial


@pytest.mark.django_db
def test_earnings_page_query_count(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    for index in range(5):
        company, _listing = _company_with_listing(ticker=f"Q{index}")
        _date_only_event(company, release_date=AS_OF)
    with CaptureQueriesContext(connection) as queries:
        response = Client().get("/earnings/?window=today")
    assert response.status_code == 200
    assert len(queries) <= 6


@pytest.mark.django_db
def test_views_do_not_call_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)

    def _fail_provider(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Stage 4.3 page made a provider/network call.")

    monkeypatch.setattr(
        "providers.alpha_vantage_reference.AlphaVantageReferenceProvider.fetch",
        _fail_provider,
    )
    assert Client().get("/earnings/").status_code == 200
    assert Client().get("/companies/").status_code == 200


def test_window_bounds_are_deterministic() -> None:
    as_of = date(2026, 10, 1)  # Thursday
    assert window_bounds(as_of, "today") == (as_of, as_of)
    assert window_bounds(as_of, "week") == (date(2026, 9, 28), date(2026, 10, 4))
    assert window_bounds(as_of, "next30") == (as_of, date(2026, 10, 30))


@pytest.mark.django_db
def test_market_timezone_dst_boundaries() -> None:
    spring_before = make_event(
        status="released",
        earnings_release_at=datetime(2026, 3, 8, 6, 30, tzinfo=UTC),
        earnings_release_precision="exact_datetime",
    )
    spring_after = make_event(
        status="released",
        earnings_release_at=datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
        earnings_release_precision="exact_datetime",
    )
    fall_before = make_event(
        status="released",
        earnings_release_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        earnings_release_precision="exact_datetime",
    )
    fall_after = make_event(
        status="released",
        earnings_release_at=datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
        earnings_release_precision="exact_datetime",
    )

    assert "EST" in build_event_display(spring_before).timing.time_label
    assert "EDT" in build_event_display(spring_after).timing.time_label
    assert "EDT" in build_event_display(fall_before).timing.time_label
    assert "EST" in build_event_display(fall_after).timing.time_label


@pytest.mark.django_db
def test_user_local_time_and_precision_display() -> None:
    exact_event = make_event(
        status="released",
        earnings_release_at=datetime(2026, 11, 18, 21, 20, tzinfo=UTC),
        earnings_release_precision="exact_datetime",
    )
    exact = build_event_display(exact_event, user_timezone=ZoneInfo("Asia/Tokyo"))
    assert exact.timing.precision == "exact_datetime"
    assert "ET:" not in exact.timing.date_label
    assert "JST" in exact.timing.local_label
    assert "21:20 UTC" == exact.timing.utc_label

    date_only_event = make_event(
        estimated_release_date=date(2026, 11, 18),
        estimated_release_precision="date_only",
    )
    date_only = build_event_display(date_only_event)
    assert date_only.timing.precision == "date_only"
    assert date_only.timing.time_label == ""

    session_only_event = make_event(release_session="after_market")
    session_only = build_event_display(session_only_event)
    assert session_only.timing.precision == "session_only"
    assert session_only.timing.session_label == "After-market"


@pytest.mark.django_db
def test_source_provenance_display_has_no_credentials() -> None:
    company = make_company("source")
    event = _date_only_event(company, release_date=date(2026, 10, 2))
    _sync_run, evidence = make_source_evidence(
        event=event,
        field_name="estimated_release",
        normalized_value={"date": "2026-10-02"},
        suffix="stage-4-3",
    )
    event.source_evidence = evidence
    event.save(update_fields=["source_evidence"])
    display = build_event_display(event)
    assert display.source_label.startswith("Source:")
    assert "apikey" not in display.source_label.lower()


def test_business_date_uses_market_timezone() -> None:
    assert isinstance(business_date(), date)
    assert MARKET_TIMEZONE.key == "America/New_York"
