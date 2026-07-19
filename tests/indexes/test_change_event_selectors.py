# mypy: ignore-errors
"""Selector tests for index change event public queries."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from companies.models import Company, SecurityListing
from indexes.models import IndexChangeCorrelation, IndexChangeEvent, IndexChangeLeg, MarketIndex

TODAY = date(2026, 7, 19)


@pytest.fixture
def sp500(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="SP500")


@pytest.fixture
def nasdaq100(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="NASDAQ100")


@pytest.fixture
def djia(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="DJIA")


@pytest.fixture
def russell2000(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="RUSSELL2000")


def _make_company(cik: str, display_name: str) -> Company:
    co, _ = Company.objects.get_or_create(
        cik=cik,
        defaults={"legal_name": f"Legal {display_name}", "display_name": display_name},
    )
    return co


def _make_listing(company: Company, ticker: str, exchange: str = "NYSE") -> SecurityListing:
    listing, _ = SecurityListing.objects.get_or_create(
        company=company,
        ticker=ticker,
        defaults={"exchange": exchange, "effective_from": date(2020, 1, 1)},
    )
    return listing


def _make_event(
    *,
    company: Company | None = None,
    effective_date: date = TODAY,
    status: str = "active",
    displacement: str = "none",
    monitoring_impact: str = "continues",
) -> IndexChangeEvent:
    if company is None:
        company = _make_company("0000000100", "DefaultCo")
    return IndexChangeEvent.objects.create(
        company=company,
        effective_date=effective_date,
        status=status,
        displacement=displacement,
        monitoring_impact=monitoring_impact,
    )


def _make_leg(
    *,
    event: IndexChangeEvent,
    index: MarketIndex,
    security_listing: SecurityListing | None = None,
    action: str = "added",
    effective_date: date | None = None,
    announcement_date: date | None = None,
) -> IndexChangeLeg:
    if security_listing is None:
        security_listing = _make_listing(event.company, "TEST")
    return IndexChangeLeg.objects.create(
        event=event,
        index=index,
        security_listing=security_listing,
        action=action,
        effective_date=effective_date or event.effective_date,
        announcement_date=announcement_date,
    )


def _make_correlation(
    *,
    earlier_event: IndexChangeEvent,
    later_event: IndexChangeEvent,
    status: str = "pending",
    displacement: str = "",
    monitoring_impact: str = "",
) -> IndexChangeCorrelation:
    return IndexChangeCorrelation.objects.create(
        earlier_event=earlier_event,
        later_event=later_event,
        status=status,
        displacement=displacement,
        monitoring_impact=monitoring_impact,
    )


@pytest.mark.django_db
class TestGetChangeEventsDefault:
    def test_default_returns_active_only(self) -> None:
        from indexes.selectors import get_change_events

        active = _make_event()
        cancelled = _make_event(
            company=active.company,
            effective_date=TODAY + timedelta(days=1),
            status="cancelled",
        )
        corrected = _make_event(
            company=active.company,
            effective_date=TODAY + timedelta(days=2),
            status="corrected",
        )

        qs = get_change_events()
        pks = {e.pk for e in qs}
        assert active.pk in pks
        assert cancelled.pk not in pks
        assert corrected.pk not in pks

    def test_cancelled_excluded_by_default(self) -> None:
        from indexes.selectors import get_change_events

        cancelled = _make_event(
            company=_make_company("0000000500", "CancelCo"),
            status="cancelled",
        )
        qs = get_change_events()
        assert cancelled.pk not in {e.pk for e in qs}

    def test_corrected_old_revision_excluded_by_default(self) -> None:
        from indexes.selectors import get_change_events

        old = _make_event(status="corrected")
        qs = get_change_events()
        assert old.pk not in {e.pk for e in qs}

    def test_active_corrected_revision_shown(self) -> None:
        from indexes.selectors import get_change_events

        old = _make_event(status="corrected")
        new_revision = _make_event(
            company=old.company,
            effective_date=old.effective_date,
            status="active",
        )
        new_revision.supersedes = old
        new_revision.save(update_fields=["supersedes"])

        qs = get_change_events()
        pks = {e.pk for e in qs}
        assert new_revision.pk in pks
        assert old.pk not in pks

    def test_ordering_is_deterministic(self) -> None:
        from indexes.selectors import get_change_events

        _make_event(
            company=_make_company("0000000601", "AAA Co"),
            effective_date=date(2026, 3, 1),
        )
        _make_event(
            company=_make_company("0000000602", "BBB Co"),
            effective_date=date(2026, 3, 1),
        )
        e3 = _make_event(
            company=_make_company("0000000603", "CCC Co"),
            effective_date=date(2026, 6, 1),
        )

        qs = get_change_events()
        results = list(qs)
        assert results[0].pk == e3.pk
        same_date = [e for e in results if e.effective_date == date(2026, 3, 1)]
        assert same_date[0].company.display_name == "AAA Co"
        assert same_date[1].company.display_name == "BBB Co"


@pytest.mark.django_db
class TestGetChangeEventsFilters:
    def test_displacement_filter(self) -> None:
        from indexes.selectors import get_change_events

        co = _make_company("0000000700", "DispCo")
        upgrade_event = _make_event(
            company=co, effective_date=date(2026, 1, 1), displacement="upgrade"
        )
        none_event = _make_event(company=co, effective_date=date(2026, 2, 1), displacement="none")

        qs = get_change_events(displacement="upgrade")
        pks = {e.pk for e in qs}
        assert upgrade_event.pk in pks
        assert none_event.pk not in pks

    def test_action_filter_added(self) -> None:
        from indexes.selectors import get_change_events

        co = _make_company("0000000800", "ActionCo")
        sp500 = MarketIndex.objects.get(code="SP500")
        nasdaq100 = MarketIndex.objects.get(code="NASDAQ100")

        ev_added = _make_event(company=co, effective_date=date(2026, 1, 1))
        _make_leg(
            event=ev_added,
            index=sp500,
            security_listing=_make_listing(co, "ADD"),
            action="added",
        )

        ev_removed = _make_event(company=co, effective_date=date(2026, 2, 1))
        _make_leg(
            event=ev_removed,
            index=nasdaq100,
            security_listing=_make_listing(co, "REM"),
            action="removed",
        )

        ev_both = _make_event(company=co, effective_date=date(2026, 3, 1))
        _make_leg(
            event=ev_both, index=sp500, security_listing=_make_listing(co, "B1"), action="added"
        )
        _make_leg(
            event=ev_both,
            index=nasdaq100,
            security_listing=_make_listing(co, "B2"),
            action="removed",
        )

        qs = get_change_events(action="added")
        pks = {e.pk for e in qs}
        assert ev_added.pk in pks
        assert ev_both.pk in pks
        assert ev_removed.pk not in pks

    def test_action_filter_removed(self) -> None:
        from indexes.selectors import get_change_events

        co = _make_company("0000000850", "RemoveCo")
        sp500 = MarketIndex.objects.get(code="SP500")

        ev_added = _make_event(company=co, effective_date=date(2026, 1, 1))
        _make_leg(
            event=ev_added, index=sp500, security_listing=_make_listing(co, "ADDR"), action="added"
        )

        qs = get_change_events(action="removed")
        assert ev_added.pk not in {e.pk for e in qs}

    def test_index_filter(self) -> None:
        from indexes.selectors import get_change_events

        co = _make_company("0000000900", "IndexCo")
        sp500 = MarketIndex.objects.get(code="SP500")
        nasdaq100 = MarketIndex.objects.get(code="NASDAQ100")

        ev_sp = _make_event(company=co, effective_date=date(2026, 1, 1))
        _make_leg(
            event=ev_sp, index=sp500, security_listing=_make_listing(co, "SP"), action="added"
        )

        ev_ndq = _make_event(company=co, effective_date=date(2026, 2, 1))
        _make_leg(
            event=ev_ndq, index=nasdaq100, security_listing=_make_listing(co, "NDQ"), action="added"
        )

        qs = get_change_events(index_code="SP500")
        pks = {e.pk for e in qs}
        assert ev_sp.pk in pks
        assert ev_ndq.pk not in pks

    def test_effective_date_range_filter(self) -> None:
        from indexes.selectors import get_change_events

        co = _make_company("0000001000", "DateCo")
        e1 = _make_event(company=co, effective_date=date(2026, 1, 1))
        e2 = _make_event(company=co, effective_date=date(2026, 3, 15))
        e3 = _make_event(company=co, effective_date=date(2026, 6, 1))

        qs = get_change_events(effective_from=date(2026, 2, 1), effective_to=date(2026, 5, 1))
        pks = {e.pk for e in qs}
        assert e1.pk not in pks
        assert e2.pk in pks
        assert e3.pk not in pks

    def test_combined_filters(self) -> None:
        from indexes.selectors import get_change_events

        co = _make_company("0000001100", "ComboCo")
        sp500 = MarketIndex.objects.get(code="SP500")

        ev_match = _make_event(
            company=co,
            effective_date=date(2026, 3, 1),
            displacement="upgrade",
        )
        _make_leg(
            event=ev_match, index=sp500, security_listing=_make_listing(co, "M"), action="added"
        )

        ev_wrong_disp = _make_event(
            company=co,
            effective_date=date(2026, 3, 15),
            displacement="none",
        )
        _make_leg(
            event=ev_wrong_disp,
            index=sp500,
            security_listing=_make_listing(co, "WD"),
            action="added",
        )

        qs = get_change_events(
            index_code="SP500",
            displacement="upgrade",
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 6, 30),
        )
        pks = {e.pk for e in qs}
        assert ev_match.pk in pks
        assert ev_wrong_disp.pk not in pks


@pytest.mark.django_db
class TestGetChangeEventsHistoryMode:
    def test_history_mode_includes_cancelled(self) -> None:
        from indexes.selectors import get_change_events

        cancelled = _make_event(
            company=_make_company("0000001200", "HistCancel"),
            status="cancelled",
        )
        qs = get_change_events(include_cancelled_corrected=True)
        assert cancelled.pk in {e.pk for e in qs}

    def test_history_mode_includes_corrected(self) -> None:
        from indexes.selectors import get_change_events

        corrected = _make_event(
            company=_make_company("0000001300", "HistCorrect"),
            status="corrected",
        )
        qs = get_change_events(include_cancelled_corrected=True)
        assert corrected.pk in {e.pk for e in qs}

    def test_history_mode_still_includes_active(self) -> None:
        from indexes.selectors import get_change_events

        active = _make_event()
        qs = get_change_events(include_cancelled_corrected=True)
        assert active.pk in {e.pk for e in qs}


@pytest.mark.django_db
class TestGetChangeEventsQueryOptimization:
    def test_select_related_company(self) -> None:
        from indexes.selectors import get_change_events

        co = _make_company("0000001400", "QueryCo")
        _make_event(company=co)
        _make_event(company=co, effective_date=date(2026, 2, 1))
        _make_event(company=co, effective_date=date(2026, 3, 1))

        with CaptureQueriesContext(connection) as ctx:
            qs = get_change_events()
            _ = [(e.company.display_name, e.effective_date) for e in qs]

        query_count = len(ctx.captured_queries)
        assert query_count <= 6, f"Expected <= 6 queries, got {query_count}"

    def test_no_n1_for_legs(self) -> None:
        from indexes.selectors import get_change_events

        co = _make_company("0000001500", "N1Co")
        sp500 = MarketIndex.objects.get(code="SP500")
        nasdaq100 = MarketIndex.objects.get(code="NASDAQ100")

        ev1 = _make_event(company=co, effective_date=date(2026, 1, 1))
        _make_leg(event=ev1, index=sp500, security_listing=_make_listing(co, "L1"), action="added")
        ev2 = _make_event(company=co, effective_date=date(2026, 2, 1))
        _make_leg(
            event=ev2, index=nasdaq100, security_listing=_make_listing(co, "L2"), action="removed"
        )
        _make_leg(event=ev2, index=sp500, security_listing=_make_listing(co, "L3"), action="added")
        ev3 = _make_event(company=co, effective_date=date(2026, 3, 1))
        _make_leg(event=ev3, index=sp500, security_listing=_make_listing(co, "L4"), action="added")

        with CaptureQueriesContext(connection) as ctx:
            qs = get_change_events()
            _ = [(e, list(e.legs.all())) for e in qs]
            for e in qs:
                for leg in e.legs.all():
                    _ = leg.index.code

        query_count = len(ctx.captured_queries)
        assert query_count <= 5, f"Expected <= 5 queries, got {query_count}"


@pytest.mark.django_db
class TestGetChangeEventsCorrelation:
    def test_confirmed_correlations_prefetched(self) -> None:
        from indexes.selectors import get_change_events

        co = _make_company("0000001600", "CorrCo")
        e1 = _make_event(company=co, effective_date=date(2026, 1, 1))
        e2 = _make_event(company=co, effective_date=date(2026, 1, 3))
        _make_correlation(earlier_event=e1, later_event=e2, status="confirmed")

        with CaptureQueriesContext(connection) as ctx:
            qs = get_change_events()
            _ = [
                (e, list(e.correlations_as_earlier.all()), list(e.correlations_as_later.all()))
                for e in qs
            ]

        query_count = len(ctx.captured_queries)
        assert query_count <= 5, f"Expected <= 5 queries, got {query_count}"

    def test_pending_correlations_not_shown_in_default(self) -> None:
        from indexes.selectors import get_change_events

        co = _make_company("0000001700", "PendCorrCo")
        e1 = _make_event(company=co, effective_date=date(2026, 1, 1))
        e2 = _make_event(company=co, effective_date=date(2026, 1, 3))
        _make_correlation(earlier_event=e1, later_event=e2, status="pending")

        qs = get_change_events()
        results = list(qs)
        assert len(results) == 2

    def test_empty_queryset(self) -> None:
        from indexes.selectors import get_change_events

        qs = get_change_events(effective_from=date(1990, 1, 1), effective_to=date(1990, 12, 31))
        assert list(qs) == []

    def test_company_filter(self) -> None:
        from indexes.selectors import get_change_events

        co_a = _make_company("0000001801", "CFilterA")
        co_b = _make_company("0000001802", "CFilterB")
        ev_a = _make_event(company=co_a)
        _make_event(company=co_b)

        qs = get_change_events(company_id=co_a.pk)
        pks = {e.pk for e in qs}
        assert ev_a.pk in pks
        assert len(pks) == 1
