"""Step 1 model-level tests for IndexChangeEvent and IndexChangeLeg."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from django.db import IntegrityError, transaction

from indexes.models import IndexChangeEvent, IndexChangeLeg, IndexMembership, MarketIndex


@pytest.fixture
def sp500(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="SP500")


@pytest.fixture
def nasdaq100(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="NASDAQ100")


def _make_event(
    *,
    company_id: str = "0000000001",
    effective_date: date | None = None,
    **overrides,
) -> IndexChangeEvent:
    from companies.models import Company

    co, _ = Company.objects.get_or_create(
        cik=company_id,
        defaults={
            "legal_name": f"TestCo {company_id}",
            "display_name": f"TestCo {company_id}",
        },
    )
    kwargs = {
        "company": co,
        "effective_date": effective_date or date(2026, 1, 1),
        **overrides,
    }
    return IndexChangeEvent.objects.create(**kwargs)


def _make_listing(
    *,
    ticker: str = "TEST",
    exchange: str = "NYSE",
    company_id: str = "0000000001",
) -> SecurityListing:  # noqa: F821
    from companies.models import Company, SecurityListing

    co, _ = Company.objects.get_or_create(
        cik=company_id,
        defaults={
            "legal_name": f"TestCo {company_id}",
            "display_name": f"TestCo {company_id}",
        },
    )
    return SecurityListing.objects.create(
        company=co,
        ticker=ticker,
        exchange=exchange,
        effective_from=date(2020, 1, 1),
    )


# ---- IndexChangeEvent ----


@pytest.mark.django_db
class TestIndexChangeEventModel:
    def test_create_minimal_event(self) -> None:
        ev = _make_event()
        assert ev.status == IndexChangeEvent.Status.ACTIVE
        assert ev.displacement == IndexChangeEvent.Displacement.NONE
        assert ev.monitoring_impact == IndexChangeEvent.MonitoringImpact.CONTINUES
        assert ev.effective_date == date(2026, 1, 1)

    def test_status_must_be_valid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_event(status="bogus")

    def test_displacement_must_be_valid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_event(displacement="invalid_dir")

    def test_monitoring_impact_must_be_valid(self) -> None:
        with pytest.raises(IntegrityError), transaction.atomic():
            _make_event(monitoring_impact="unknown_impact")

    def test_supersedes_chain(self) -> None:
        original = _make_event(effective_date=date(2026, 1, 1))
        corrected = _make_event(
            effective_date=date(2026, 7, 1),  # different date for the correction
            supersedes=original,
        )
        assert corrected.supersedes == original
        assert original.superseded_by is not None
        assert original.superseded_by == corrected

    def test_effective_date_index(self) -> None:
        e1 = _make_event(effective_date=date(2026, 2, 1), company_id="0000000010")
        e2 = _make_event(effective_date=date(2026, 1, 1), company_id="0000000011")
        events = list(IndexChangeEvent.objects.order_by("-effective_date"))
        assert events[0] == e1
        assert events[1] == e2


# ---- IndexChangeLeg ----


@pytest.mark.django_db
class TestIndexChangeLegModel:
    def test_create_added_leg(self, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        ev = _make_event(effective_date=date(2026, 6, 1))
        listing = _make_listing(ticker="ADDCO")
        leg = IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 1),
        )
        assert leg.action == IndexChangeLeg.Action.ADDED
        assert leg.event == ev
        assert leg.index == sp500

    def test_create_removed_leg(self, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        ev = _make_event(effective_date=date(2026, 6, 1))
        listing = _make_listing(ticker="RMCO")
        leg = IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 1),
        )
        assert leg.action == IndexChangeLeg.Action.REMOVED

    def test_action_must_be_valid(self, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        ev = _make_event()
        listing = _make_listing()
        with pytest.raises(IntegrityError), transaction.atomic():
            IndexChangeLeg.objects.create(
                event=ev,
                index=sp500,
                security_listing=listing,
                action="replaced",
                effective_date=date(2026, 1, 1),
            )

    def test_event_has_multiple_legs(self, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        ev = _make_event(effective_date=date(2026, 6, 1), company_id="0000000100")
        l1 = _make_listing(ticker="BIGCO")
        l2 = _make_listing(ticker="SMALLCO", company_id="0000000100")
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=l1,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 1),
        )
        IndexChangeLeg.objects.create(
            event=ev,
            index=nasdaq100,
            security_listing=l2,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 1),
        )
        assert ev.legs.count() == 2

    def test_same_security_different_indexes_allowed(
        self, sp500: MarketIndex, nasdaq100: MarketIndex
    ) -> None:
        ev1 = _make_event(effective_date=date(2026, 6, 1))
        ev2 = _make_event(effective_date=date(2026, 6, 15))
        listing = _make_listing(ticker="MOVECO")
        leg1 = IndexChangeLeg.objects.create(
            event=ev1,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 1),
        )
        leg2 = IndexChangeLeg.objects.create(
            event=ev2,
            index=nasdaq100,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 15),
        )
        assert leg1.pk != leg2.pk

    def test_duplicate_action_in_same_event_rejected(
        self, sp500: MarketIndex, nasdaq100: MarketIndex
    ) -> None:
        ev = _make_event()
        listing = _make_listing(ticker="DUPCO")
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 1, 1),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            IndexChangeLeg.objects.create(
                event=ev,
                index=sp500,
                security_listing=listing,
                action=IndexChangeLeg.Action.ADDED,
                effective_date=date(2026, 1, 1),
            )

    def test_add_and_remove_same_event_allowed(
        self, sp500: MarketIndex, nasdaq100: MarketIndex
    ) -> None:
        ev = _make_event()
        listing = _make_listing(ticker="BOTHCO")
        add = IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 1, 1),
        )
        rem = IndexChangeLeg.objects.create(
            event=ev,
            index=nasdaq100,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 1, 1),
        )
        assert add.pk != rem.pk

    def test_membership_nullable(self, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        ev = _make_event()
        listing = _make_listing()
        leg = IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 1, 1),
        )
        assert leg.membership is None

    def test_membership_linkable(self, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        ev = _make_event()
        listing = _make_listing()
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 1, 1),
        )
        leg = IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 1, 1),
            membership=m,
        )
        assert leg.membership == m

    def test_detected_at_defaults_to_now(self, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        ev = _make_event()
        listing = _make_listing()
        before = datetime.now(UTC) - timedelta(seconds=1)
        leg = IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 1, 1),
        )
        after = datetime.now(UTC) + timedelta(seconds=1)
        assert before <= leg.detected_at <= after

    def test_event_ordering(self) -> None:
        e1 = _make_event(effective_date=date(2026, 3, 1), company_id="0000000100")
        e2 = _make_event(effective_date=date(2026, 1, 1), company_id="0000000101")
        events = list(IndexChangeEvent.objects.all())
        assert events[0] == e1  # newer first
        assert events[1] == e2

    def test_leg_ordering(self, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        ev = _make_event(effective_date=date(2026, 6, 1))
        listing = _make_listing(ticker="ORDER")
        leg2 = IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 1),
        )
        legs = list(IndexChangeLeg.objects.all())
        # newer effective_date first
        assert legs[0] == leg2
