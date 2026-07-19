# mypy: ignore-errors
"""Step 4 classification service tests."""

from __future__ import annotations

from datetime import date

import pytest

from companies.models import Company, SecurityListing
from indexes.models import (
    IndexChangeCorrelation,
    IndexChangeEvent,
    IndexChangeLeg,
    IndexMembership,
    MarketIndex,
)
from indexes.services import (
    InvalidIndexChangeInput,
    classify_index_change_correlation,
    classify_index_change_event,
)


@pytest.fixture
def sp500(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="SP500")


@pytest.fixture
def nasdaq100(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="NASDAQ100")


@pytest.fixture
def russell(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="RUSSELL2000")


@pytest.fixture
def company(db: object) -> Company:
    del db
    return Company.objects.create(
        legal_name="ClassCo",
        display_name="ClassCo",
        cik="0000000700",
    )


@pytest.fixture
def listing(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="CLS",
        exchange="NYSE",
        effective_from=date(2020, 1, 1),
    )


@pytest.fixture
def listing2(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="CLS2",
        exchange="NASDAQ",
        effective_from=date(2020, 1, 1),
    )


def _mk_event(company, eff_date, **extra) -> IndexChangeEvent:
    return IndexChangeEvent.objects.create(
        company=company,
        effective_date=eff_date,
        **extra,
    )


# ---- Displacement ----


@pytest.mark.django_db
class TestDisplacementClassification:
    def test_upgrade_russell_remove_sp500_add(
        self,
        company,
        listing,
        listing2,
        russell,
        sp500,
    ):
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=russell,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 15),
        )
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing2,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(ev)
        assert result.displacement == IndexChangeEvent.Displacement.UPGRADE
        assert result.displacement_changed is True

    def test_downgrade_sp500_remove_russell_add(
        self,
        company,
        listing,
        listing2,
        sp500,
        russell,
    ):
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 15),
        )
        IndexChangeLeg.objects.create(
            event=ev,
            index=russell,
            security_listing=listing2,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(ev)
        assert result.displacement == IndexChangeEvent.Displacement.DOWNGRADE

    def test_cross_index_nasdaq_remove_sp500_add(
        self,
        company,
        listing,
        listing2,
        nasdaq100,
        sp500,
    ):
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=nasdaq100,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 15),
        )
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing2,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(ev)
        assert result.displacement == IndexChangeEvent.Displacement.CROSS_INDEX

    def test_simple_add_is_none(
        self,
        company,
        listing,
        sp500,
    ):
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(ev)
        assert result.displacement == IndexChangeEvent.Displacement.NONE

    def test_simple_remove_is_none(
        self,
        company,
        listing,
        sp500,
    ):
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(ev)
        assert result.displacement == IndexChangeEvent.Displacement.NONE

    def test_idempotent(self, company, listing, sp500, russell):
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=russell,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 15),
        )
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        r1 = classify_index_change_event(ev)
        r2 = classify_index_change_event(ev)
        assert r1.displacement == r2.displacement
        assert r2.displacement_changed is False


# ---- Monitoring Impact ----


@pytest.mark.django_db
class TestMonitoringImpact:
    def test_enters_base_pool_first_time(
        self,
        company,
        listing,
        sp500,
    ):
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(ev)
        assert result.monitoring_impact == IndexChangeEvent.MonitoringImpact.ENTERS_BASE_POOL

    def test_reenters_base_pool(
        self,
        company,
        listing,
        listing2,
        sp500,
        nasdaq100,
    ):
        """Company had membership before (ended), now re-entering."""
        # Old ended membership
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ENDED,
            effective_from=date(2025, 1, 1),
            effective_to=date(2025, 12, 31),
        )
        # New ADD event
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(ev)
        assert result.monitoring_impact == IndexChangeEvent.MonitoringImpact.REENTERS_BASE_POOL

    def test_exits_base_pool(
        self,
        company,
        listing,
        sp500,
    ):
        """Active membership exists before, event removes it."""
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(ev)
        assert result.monitoring_impact == IndexChangeEvent.MonitoringImpact.EXITS_BASE_POOL

    def test_continues_remove_one_retain_another(
        self,
        company,
        listing,
        listing2,
        sp500,
        nasdaq100,
    ):
        """Remove one index but still in another base pool index → CONTINUES."""
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
        IndexMembership.objects.create(
            index=nasdaq100,
            security_listing=listing2,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(ev)
        assert result.monitoring_impact == IndexChangeEvent.MonitoringImpact.CONTINUES

    def test_future_event_classified_without_membership_mutation(
        self,
        company,
        listing,
        sp500,
    ):
        from datetime import timedelta

        future = date.today() + timedelta(days=60)
        ev = _mk_event(company, future)
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=future,
        )
        classify_index_change_event(ev)
        # No membership created
        assert (
            IndexMembership.objects.filter(
                security_listing__company=company,
                status=IndexMembership.Status.ACTIVE,
            ).count()
            == 0
        )

    def test_idempotent_monitoring(
        self,
        company,
        listing,
        sp500,
    ):
        ev = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=ev,
            index=sp500,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 15),
        )
        r1 = classify_index_change_event(ev)
        r2 = classify_index_change_event(ev)
        assert r1.monitoring_impact == r2.monitoring_impact
        assert r2.monitoring_impact_changed is False


# ---- Cross-Date Correlation Classification ----


@pytest.mark.django_db
class TestCorrelationClassification:
    def test_confirmed_correlation_upgrade(
        self,
        company,
        listing,
        listing2,
        russell,
        sp500,
    ):
        e1 = _mk_event(company, date(2026, 6, 15))
        IndexChangeLeg.objects.create(
            event=e1,
            index=russell,
            security_listing=listing,
            action=IndexChangeLeg.Action.REMOVED,
            effective_date=date(2026, 6, 15),
        )
        e2 = _mk_event(company, date(2026, 6, 18))
        IndexChangeLeg.objects.create(
            event=e2,
            index=sp500,
            security_listing=listing2,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=date(2026, 6, 18),
        )
        corr = IndexChangeCorrelation.objects.create(
            earlier_event=e1,
            later_event=e2,
            status=IndexChangeCorrelation.Status.CONFIRMED,
        )
        result = classify_index_change_correlation(corr)
        assert result.displacement == IndexChangeEvent.Displacement.UPGRADE

    def test_pending_correlation_cannot_classify(self, company):
        e1 = _mk_event(company, date(2026, 6, 15))
        e2 = _mk_event(company, date(2026, 6, 18))
        corr = IndexChangeCorrelation.objects.create(
            earlier_event=e1,
            later_event=e2,
            status=IndexChangeCorrelation.Status.PENDING,
        )
        with pytest.raises(InvalidIndexChangeInput, match="CONFIRMED"):
            classify_index_change_correlation(corr)

    def test_rejected_correlation_cannot_classify(self, company):
        e1 = _mk_event(company, date(2026, 6, 15))
        e2 = _mk_event(company, date(2026, 6, 18))
        corr = IndexChangeCorrelation.objects.create(
            earlier_event=e1,
            later_event=e2,
            status=IndexChangeCorrelation.Status.REJECTED,
        )
        with pytest.raises(InvalidIndexChangeInput, match="CONFIRMED"):
            classify_index_change_correlation(corr)
