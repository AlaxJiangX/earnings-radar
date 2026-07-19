"""View and template tests for the index changes public page."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.test import Client

from companies.models import Company, SecurityListing
from indexes.models import IndexChangeCorrelation, IndexChangeEvent, IndexChangeLeg, MarketIndex

TODAY = date(2026, 7, 19)


def _vc_make_company(cik: str, display_name: str) -> Company:
    co, _ = Company.objects.get_or_create(
        cik=cik,
        defaults={"legal_name": f"Legal {display_name}", "display_name": display_name},
    )
    return co


def _vc_make_event(
    *,
    company: Company | None = None,
    effective_date: date = TODAY,
    status: str = "active",
    displacement: str = "none",
    monitoring_impact: str = "continues",
) -> IndexChangeEvent:
    if company is None:
        company = _vc_make_company("0000900100", "DefaultVCo")
    return IndexChangeEvent.objects.create(
        company=company,
        effective_date=effective_date,
        status=status,
        displacement=displacement,
        monitoring_impact=monitoring_impact,
    )


def _vc_make_leg(
    *,
    event: IndexChangeEvent,
    index: MarketIndex,
    action: str = "added",
    announcement_date: date | None = None,
) -> IndexChangeLeg:

    listing, _ = SecurityListing.objects.get_or_create(
        company=event.company,
        ticker=f"T{event.company.display_name[:3]}",
        defaults={"exchange": "NYSE", "effective_from": date(2020, 1, 1)},
    )
    return IndexChangeLeg.objects.create(
        event=event,
        index=index,
        security_listing=listing,
        action=action,
        effective_date=event.effective_date,
        announcement_date=announcement_date,
    )


@pytest.fixture
def sp500(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="SP500")


@pytest.fixture
def nasdaq100(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="NASDAQ100")


@pytest.mark.django_db
class TestIndexChangesView:
    def test_guest_get_returns_200(self) -> None:
        client = Client()
        response = client.get("/index-changes/")
        assert response.status_code == 200

    def test_full_page_contains_heading(self) -> None:
        client = Client()
        response = client.get("/index-changes/")
        assert "Index Changes" in response.content.decode()

    def test_full_page_contains_filter_form(self) -> None:
        client = Client()
        response = client.get("/index-changes/")
        content = response.content.decode()
        assert "<form" in content
        assert 'name="index"' in content
        assert 'name="action"' in content
        assert 'name="displacement"' in content

    def test_htmx_request_returns_partial(self) -> None:
        client = Client()
        response = client.get("/index-changes/", HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        content = response.content.decode()
        assert "<html" not in content.lower()
        assert 'id="change-list"' in content

    def test_non_htmx_request_returns_full_page(self) -> None:
        client = Client()
        response = client.get("/index-changes/")
        content = response.content.decode()
        assert "<!DOCTYPE html>" in content
        assert "<html" in content


@pytest.mark.django_db
class TestIndexChangesActiveDefault:
    def test_active_events_visible(self) -> None:
        co = _vc_make_company("0000910000", "ActiveCo")
        _vc_make_event(company=co)
        client = Client()
        response = client.get("/index-changes/")
        content = response.content.decode()
        assert co.display_name in content

    def test_cancelled_hidden_by_default(self) -> None:
        co = _vc_make_company("0000920000", "CancelCo")
        _vc_make_event(company=co, status="cancelled")
        client = Client()
        response = client.get("/index-changes/")
        content = response.content.decode()
        assert co.display_name not in content

    def test_corrected_old_revision_hidden_by_default(self) -> None:
        co = _vc_make_company("0000930000", "CorrectCo")
        _vc_make_event(company=co, status="corrected")
        client = Client()
        response = client.get("/index-changes/")
        content = response.content.decode()
        assert co.display_name not in content

    def test_history_mode_shows_cancelled(self) -> None:
        co = _vc_make_company("0000940000", "HistCanc")
        _vc_make_event(company=co, status="cancelled")
        client = Client()
        response = client.get("/index-changes/?history=all")
        content = response.content.decode()
        assert co.display_name in content
        assert "Cancelled" in content

    def test_history_mode_shows_corrected(self) -> None:
        co = _vc_make_company("0000950000", "HistCorr")
        _vc_make_event(company=co, status="corrected")
        client = Client()
        response = client.get("/index-changes/?history=all")
        content = response.content.decode()
        assert co.display_name in content
        assert "Corrected" in content


@pytest.mark.django_db
class TestIndexChangesFilters:
    def test_displacement_filter_applied(self) -> None:
        co = _vc_make_company("0000960000", "DispFilt")
        _vc_make_event(company=co, effective_date=TODAY, displacement="upgrade")
        _vc_make_event(company=co, effective_date=TODAY + timedelta(days=1), displacement="none")
        client = Client()
        response = client.get("/index-changes/?displacement=upgrade")
        assert response.status_code == 200

    def test_index_filter_applied(self, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        co = _vc_make_company("0000970000", "IdxFilt")
        ev_sp = _vc_make_event(company=co, effective_date=TODAY)
        _vc_make_leg(event=ev_sp, index=sp500)
        ev_nq = _vc_make_event(company=co, effective_date=TODAY + timedelta(days=1))
        _vc_make_leg(event=ev_nq, index=nasdaq100)
        client = Client()
        response = client.get("/index-changes/?index=SP500")
        assert response.status_code == 200
        assert co.display_name in response.content.decode()

    def test_action_filter_applied(self, sp500: MarketIndex) -> None:
        co = _vc_make_company("0000980000", "ActFilt")
        ev_add = _vc_make_event(company=co, effective_date=TODAY)
        _vc_make_leg(event=ev_add, index=sp500, action="added")
        client = Client()
        response = client.get("/index-changes/?action=added")
        assert response.status_code == 200

    def test_date_range_filter(self) -> None:
        co = _vc_make_company("0000990000", "DateFilt")
        _vc_make_event(company=co, effective_date=date(2026, 3, 15))
        client = Client()
        response = client.get("/index-changes/?from=2026-03-01&to=2026-04-01")
        assert response.status_code == 200
        assert co.display_name in response.content.decode()

    def test_invalid_filter_silent_ignore(self) -> None:
        client = Client()
        response = client.get("/index-changes/?displacement=bogus&page=abc&index=INVALID")
        assert response.status_code == 200

    def test_combined_filters(self, sp500: MarketIndex) -> None:
        co = _vc_make_company("0001000000", "CombFilt")
        ev = _vc_make_event(company=co, effective_date=date(2026, 5, 1), displacement="upgrade")
        _vc_make_leg(event=ev, index=sp500, action="added")
        client = Client()
        response = client.get(
            "/index-changes/?displacement=upgrade&index=SP500&from=2026-01-01&to=2026-12-31"
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestIndexChangesEdgeStates:
    def test_empty_no_events(self) -> None:
        client = Client()
        response = client.get("/index-changes/")
        assert "No index changes recorded yet" in response.content.decode()

    def test_empty_filter_match_nothing(self) -> None:
        co = _vc_make_company("0001010000", "EmptyFilt")
        _vc_make_event(company=co, effective_date=TODAY)
        client = Client()
        response = client.get("/index-changes/?from=1990-01-01&to=1990-12-31")
        assert "No changes match your filters" in response.content.decode()

    def test_upcoming_badge(self) -> None:
        co = _vc_make_company("0001020000", "UpcomingCo")
        _vc_make_event(company=co, effective_date=TODAY + timedelta(days=30))
        client = Client()
        response = client.get("/index-changes/")
        assert "Upcoming" in response.content.decode()

    def test_none_displacement_hidden(self) -> None:
        co = _vc_make_company("0001030000", "NoneDisp")
        _vc_make_event(company=co, displacement="none")
        client = Client()
        response = client.get("/index-changes/")
        content = response.content.decode()
        # NONE displacement should not be rendered as a badge
        # The displacement cell should show &mdash;
        assert "&mdash;" in content

    def test_continues_impact_hidden(self) -> None:
        co = _vc_make_company("0001040000", "ContImp")
        _vc_make_event(company=co, monitoring_impact="continues")
        client = Client()
        response = client.get("/index-changes/")
        content = response.content.decode()
        # Continues is default — should show &mdash; not a badge
        assert "&mdash;" in content

    def test_invalid_page_not_crash(self) -> None:
        client = Client()
        response = client.get("/index-changes/?page=999")
        assert response.status_code == 200

    def test_page_zero_not_crash(self) -> None:
        client = Client()
        response = client.get("/index-changes/?page=0")
        assert response.status_code == 200


@pytest.mark.django_db
class TestIndexChangesMultiLeg:
    def test_multi_leg_event_shows_all_legs(
        self, sp500: MarketIndex, nasdaq100: MarketIndex
    ) -> None:
        co = _vc_make_company("0001050000", "MultiLeg")
        ev = _vc_make_event(company=co, effective_date=TODAY)
        _vc_make_leg(event=ev, index=sp500, action="added")
        _vc_make_leg(event=ev, index=nasdaq100, action="removed")
        client = Client()
        response = client.get("/index-changes/")
        content = response.content.decode()
        assert "SP500" in content
        assert "NASDAQ100" in content


@pytest.mark.django_db
class TestIndexChangesCorrelation:
    def test_confirmed_correlation_shown(self) -> None:
        co = _vc_make_company("0001060000", "ConfCorr")
        e1 = _vc_make_event(company=co, effective_date=date(2026, 1, 1))
        e2 = _vc_make_event(company=co, effective_date=date(2026, 1, 3))
        IndexChangeCorrelation.objects.create(
            earlier_event=e1,
            later_event=e2,
            status="confirmed",
            displacement="upgrade",
            monitoring_impact="continues",
        )
        client = Client()
        response = client.get("/index-changes/")
        assert "Related" in response.content.decode()

    def test_pending_correlation_not_shown_as_fact(self) -> None:
        co = _vc_make_company("0001070000", "PendCorrV")
        e1 = _vc_make_event(company=co, effective_date=date(2026, 1, 1))
        e2 = _vc_make_event(company=co, effective_date=date(2026, 1, 3))
        IndexChangeCorrelation.objects.create(
            earlier_event=e1,
            later_event=e2,
            status="pending",
        )
        client = Client()
        response = client.get("/index-changes/")
        assert "PendCorrV" in response.content.decode()
        assert "Related" not in response.content.decode()


@pytest.mark.django_db
class TestIndexChangesPagination:
    def test_pagination_appears_with_many_events(self) -> None:
        co = _vc_make_company("0001080000", "PageCo")
        for i in range(30):
            _vc_make_event(company=co, effective_date=TODAY + timedelta(days=i))
        client = Client()
        response = client.get("/index-changes/")
        content = response.content.decode()
        assert "Previous" in content or "Next" in content

    def test_invalid_page_filter(self) -> None:
        client = Client()
        response = client.get("/index-changes/?page=abc")
        assert response.status_code == 200

    def test_negative_page(self) -> None:
        client = Client()
        response = client.get("/index-changes/?page=-1")
        assert response.status_code == 200


@pytest.mark.django_db
class TestIndexChangesAnnouncementDate:
    def test_announcement_date_displayed(self, sp500: MarketIndex) -> None:
        co = _vc_make_company("0001090000", "AnnDateCo")
        ev = _vc_make_event(company=co, effective_date=TODAY)
        _vc_make_leg(event=ev, index=sp500, announcement_date=date(2026, 7, 1))
        client = Client()
        response = client.get("/index-changes/")
        assert "2026-07-01" in response.content.decode()

    def test_missing_announcement_date_shows_dash(self, sp500: MarketIndex) -> None:
        co = _vc_make_company("0001100000", "NoAnnCo")
        ev = _vc_make_event(company=co, effective_date=TODAY)
        _vc_make_leg(event=ev, index=sp500)  # no announcement_date
        client = Client()
        response = client.get("/index-changes/")
        content = response.content.decode()
        assert "&mdash;" in content
        assert co.display_name in content


@pytest.mark.django_db
class TestIndexChangesPaginationFilterPreservation:
    """Regression tests for pagination preserving filter query params."""

    def test_preserves_single_filter(self, sp500: MarketIndex) -> None:
        co = _vc_make_company("0001080001", "PageFilt1")
        for i in range(30):
            ev = _vc_make_event(company=co, effective_date=TODAY + timedelta(days=i))
            _vc_make_leg(event=ev, index=sp500, action="added")
        client = Client()
        response = client.get("/index-changes/?index=SP500&page=1")
        content = response.content.decode()
        assert "Next" in content
        assert "index=SP500" in content

    def test_preserves_combined_filters(self, sp500: MarketIndex) -> None:
        co = _vc_make_company("0001080002", "PageFilt2")
        for i in range(30):
            ev = _vc_make_event(
                company=co,
                effective_date=TODAY + timedelta(days=i),
                displacement="upgrade",
            )
            _vc_make_leg(event=ev, index=sp500, action="added")
        client = Client()
        url = (
            "/index-changes/?"
            "index=SP500&action=added&displacement=upgrade&"
            "from=2026-01-01&to=2026-12-31&history=all&page=1"
        )
        response = client.get(url)
        content = response.content.decode()
        assert "Next" in content
        assert "index=SP500" in content
        assert "action=added" in content
        assert "displacement=upgrade" in content
        assert "from=2026-01-01" in content
        assert "to=2026-12-31" in content
        assert "history=all" in content

    def test_no_duplicate_page(self, sp500: MarketIndex) -> None:
        co = _vc_make_company("0001080003", "PageFilt3")
        for i in range(30):
            ev = _vc_make_event(company=co, effective_date=TODAY + timedelta(days=i))
            _vc_make_leg(event=ev, index=sp500, action="added")
        client = Client()
        response = client.get("/index-changes/?index=SP500&page=1")
        content = response.content.decode()
        assert "page=1&page=2" not in content

    def test_previous_page_preserves(self, sp500: MarketIndex) -> None:
        co = _vc_make_company("0001080004", "PageFilt4")
        for i in range(30):
            ev = _vc_make_event(company=co, effective_date=TODAY + timedelta(days=i))
            _vc_make_leg(event=ev, index=sp500, action="added")
        client = Client()
        response = client.get("/index-changes/?index=SP500&page=2")
        content = response.content.decode()
        assert "Previous" in content
        assert "index=SP500" in content

    def test_htmx_preserves(self, sp500: MarketIndex) -> None:
        co = _vc_make_company("0001080005", "PageFilt5")
        for i in range(30):
            ev = _vc_make_event(company=co, effective_date=TODAY + timedelta(days=i))
            _vc_make_leg(event=ev, index=sp500, action="added")
        client = Client()
        response = client.get(
            "/index-changes/?index=SP500&action=added&page=1",
            HTTP_HX_REQUEST="true",
        )
        content = response.content.decode()
        assert "hx-get" in content
        assert "index=SP500" in content
        assert "action=added" in content

    def test_href_and_hx_get_match(self, sp500: MarketIndex) -> None:
        co = _vc_make_company("0001080008", "PageFilt8")
        for i in range(30):
            ev = _vc_make_event(company=co, effective_date=TODAY + timedelta(days=i))
            _vc_make_leg(event=ev, index=sp500, action="added")
        client = Client()
        response = client.get("/index-changes/?index=SP500&page=1")
        content = response.content.decode()
        assert 'href="?index=SP500' in content
        assert 'hx-get="?index=SP500' in content
