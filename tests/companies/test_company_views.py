from __future__ import annotations

from datetime import date

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from companies.models import Company, SecurityListing
from earnings.models import EarningsEvent
from indexes.models import IndexMembership, MarketIndex
from tests.earnings.helpers import make_company, make_event, make_source_evidence

AS_OF = date(2026, 10, 1)


def _patch_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("earnings.views.business_date", lambda: AS_OF)
    monkeypatch.setattr("companies.views.business_date", lambda: AS_OF)


def _company_with_listing(
    *,
    name: str,
    ticker: str,
    exchange: str = "NASDAQ",
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> tuple[Company, SecurityListing]:
    company = make_company(name, display_name=name)
    listing = SecurityListing.objects.create(
        company=company,
        ticker=ticker,
        exchange=exchange,
        security_name=f"{name} {ticker}",
        security_type="common_stock",
        effective_from=effective_from,
        effective_to=effective_to,
    )
    return company, listing


def _add_index_membership(listing: SecurityListing, code: str = "SP500") -> None:
    IndexMembership.objects.create(
        index=MarketIndex.objects.get(code=code),
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )


def _event(
    company: Company,
    *,
    release_date: date,
    status: str = "scheduled_estimated",
    period_end_date: date = date(2026, 6, 30),
    period_type: str = "Q2",
    **overrides: object,
) -> EarningsEvent:
    return make_event(
        company=company,
        period_end_date=period_end_date,
        period_type=period_type,
        status=status,
        estimated_release_date=release_date,
        estimated_release_precision="date_only",
        **overrides,
    )


@pytest.mark.django_db
def test_company_list_renders_search_and_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    alpha, _alpha_listing = _company_with_listing(name="Alpha Corp", ticker="AAA")
    beta, _beta_listing = _company_with_listing(name="Beta Corp", ticker="BBB")
    client = Client()

    all_content = client.get("/companies/").content.decode()
    assert alpha.display_name in all_content
    assert beta.display_name in all_content
    assert "AAA" in all_content

    search_name = client.get("/companies/?q=Alpha").content.decode()
    assert alpha.display_name in search_name
    assert beta.display_name not in search_name

    search_ticker = client.get("/companies/?q=BBB").content.decode()
    assert beta.display_name in search_ticker
    assert alpha.display_name not in search_ticker


@pytest.mark.django_db
def test_company_list_index_exchange_and_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    member, member_listing = _company_with_listing(
        name="Member Corp", ticker="MEM", exchange="NASDAQ"
    )
    _add_index_membership(member_listing)
    outside, _outside_listing = _company_with_listing(
        name="Outside Corp", ticker="OUT", exchange="NYSE"
    )
    client = Client()

    index_content = client.get("/companies/?index=SP500").content.decode()
    assert member.display_name in index_content
    assert outside.display_name not in index_content

    exchange_content = client.get("/companies/?exchange=NYSE").content.decode()
    assert outside.display_name in exchange_content
    assert member.display_name not in exchange_content

    for index in range(30):
        _company_with_listing(name=f"Paged {index:02d}", ticker=f"P{index:02d}")
    page_one = client.get("/companies/").content.decode()
    page_two = client.get("/companies/?page=2").content.decode()
    assert "Page 1 of 2" in page_one
    assert "Page 2 of 2" in page_two
    assert "Paged 25" in page_two


@pytest.mark.django_db
def test_company_detail_renders_identity_next_history_index_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    company, listing = _company_with_listing(name="Detail Corp", ticker="DET")
    _add_index_membership(listing)
    next_event = make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        status="scheduled_confirmed",
        confirmed_release_date=date(2026, 10, 5),
        confirmed_release_precision="date_only",
    )
    _sync_run, evidence = make_source_evidence(
        event=next_event,
        field_name="confirmed_release",
        normalized_value={"date": "2026-10-05"},
        suffix="company-detail",
    )
    next_event.source_evidence = evidence
    next_event.save(update_fields=["source_evidence"])
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        status="released",
        earnings_release_date=date(2026, 7, 1),
        earnings_release_precision="date_only",
    )

    content = Client().get("/companies/DET/").content.decode()
    assert company.display_name in content
    assert "Detail Corp" in content
    assert "S&amp;P 500" in content
    assert "Oct 05, 2026" in content
    assert "2026-06-30" in content
    assert "Source:" in content


@pytest.mark.django_db
def test_company_detail_ticker_resolution_and_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    company, _listing = _company_with_listing(name="Case Corp", ticker="CASE")
    client = Client()
    assert client.get("/companies/case/").status_code == 200
    assert company.display_name in client.get("/companies/CASE/").content.decode()

    _other_company, _other_listing = _company_with_listing(
        name="Other Case Corp",
        ticker="CASE",
        exchange="NYSE",
    )
    assert client.get("/companies/CASE/").status_code == 404


@pytest.mark.django_db
def test_company_detail_historical_ticker_is_not_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    _company, _listing = _company_with_listing(
        name="Historical Corp",
        ticker="HIST",
        effective_to=date(2026, 9, 30),
    )
    assert Client().get("/companies/HIST/").status_code == 404


@pytest.mark.django_db
def test_company_detail_excludes_cancelled_from_next_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    company, _listing = _company_with_listing(name="Cancel Corp", ticker="CANC")
    _event(company, release_date=date(2026, 10, 2), status="cancelled")
    _event(
        company,
        release_date=date(2026, 10, 5),
        status="scheduled_confirmed",
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
    )
    content = Client().get("/companies/CANC/").content.decode()
    assert "Oct 05, 2026" in content
    assert "Cancelled" in content


@pytest.mark.django_db
def test_company_detail_history_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    company, _listing = _company_with_listing(name="History Corp", ticker="HIST2")
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        status="released",
        earnings_release_date=date(2026, 7, 1),
        earnings_release_precision="date_only",
    )
    make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        status="released",
        earnings_release_date=date(2026, 10, 1),
        earnings_release_precision="date_only",
    )
    content = Client().get("/companies/HIST2/").content.decode()
    assert content.index("2026-09-30") < content.index("2026-06-30")


@pytest.mark.django_db
def test_company_detail_candidate_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    company, _listing = _company_with_listing(name="Candidate Corp", ticker="CAND2")
    make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type=None,
        identity_status="candidate",
        identity_key=None,
        identity_rule_version=None,
        estimated_release_date=date(2026, 10, 2),
        estimated_release_precision="date_only",
    )
    client = Client()
    anonymous = client.get("/companies/CAND2/").content.decode()
    assert "Candidate / Incomplete" not in anonymous

    user = User.objects.create_user(
        email="candidate-owner@example.com", password="fixture-password"
    )
    client.force_login(user)
    authenticated = client.get("/companies/CAND2/").content.decode()
    assert "Candidate / Incomplete" in authenticated


@pytest.mark.django_db
def test_company_pages_query_counts_and_no_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_as_of(monkeypatch)
    company, listing = _company_with_listing(name="Count Corp", ticker="COUNT")
    _add_index_membership(listing)
    _event(company, release_date=date(2026, 10, 2))

    def _fail_provider(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Stage 4.3 page made a provider/network call.")

    monkeypatch.setattr(
        "providers.alpha_vantage_reference.AlphaVantageReferenceProvider.fetch",
        _fail_provider,
    )
    client = Client()
    with CaptureQueriesContext(connection) as list_queries:
        assert client.get("/companies/").status_code == 200
    with CaptureQueriesContext(connection) as detail_queries:
        assert client.get("/companies/COUNT/").status_code == 200
    assert len(list_queries) <= 8
    assert len(detail_queries) <= 10


@pytest.mark.django_db
def test_company_detail_does_not_leak_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_as_of(monkeypatch)
    company, _listing = _company_with_listing(name="Credential Corp", ticker="CRED")
    _event(company, release_date=date(2026, 10, 2))
    content = Client().get("/companies/CRED/").content.decode()
    assert "apikey" not in content.lower()
    assert "api_key" not in content.lower()
