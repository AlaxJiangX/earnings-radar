# mypy: ignore-errors
"""Step 6 — Stage 3.3 full integration scenarios."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from companies.models import Company, SecurityListing
from indexes.models import (
    IndexChangeCorrelation,
    IndexChangeEvent,
    IndexMembership,
    MarketIndex,
)
from indexes.services import (
    EventCorrectionLegSpec,
    classify_index_change_correlation,
    classify_index_change_event,
    correct_index_change_event,
    generate_index_change_correlation_candidates,
    record_index_change_leg,
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
        legal_name="IntegrationCo",
        display_name="IntCo",
        cik="0000000900",
    )


@pytest.fixture
def listing(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="INT",
        exchange="NYSE",
        effective_from=date(2020, 1, 1),
    )


@pytest.fixture
def listing2(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="INT2",
        exchange="NASDAQ",
        effective_from=date(2020, 1, 1),
    )


# ---- Core integration scenarios ----


@pytest.mark.django_db
class TestSimpleAdd:
    def test_add_then_classify(self, company: Company, listing: SecurityListing, sp500: MarketIndex) -> None:
        r = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        assert r.event_created
        assert r.leg_created
        result = classify_index_change_event(r.event)
        assert result.displacement == IndexChangeEvent.Displacement.NONE
        # no prior membership → enters
        assert result.monitoring_impact == IndexChangeEvent.MonitoringImpact.ENTERS_BASE_POOL
        assert IndexMembership.objects.count() == 0


@pytest.mark.django_db
class TestSimpleRemove:
    def test_remove_then_classify(self, company: Company, listing: SecurityListing, sp500: MarketIndex) -> None:
        # Give them active membership before
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
        r = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="removed",
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(r.event)
        assert result.displacement == IndexChangeEvent.Displacement.NONE
        assert result.monitoring_impact == IndexChangeEvent.MonitoringImpact.EXITS_BASE_POOL


@pytest.mark.django_db
class TestSameDateEvents:
    def test_upgrade(self, company: Company, listing: SecurityListing, listing2: SecurityListing, russell: MarketIndex, sp500: MarketIndex) -> None:
        r = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=russell,
            action="removed",
            effective_date=date(2026, 6, 15),
        )
        r2 = record_index_change_leg(
            company=company,
            security_listing=listing2,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        assert r.event.pk == r2.event.pk
        result = classify_index_change_event(r.event)
        assert result.displacement == IndexChangeEvent.Displacement.UPGRADE

    def test_downgrade(self, company: Company, listing: SecurityListing, listing2: SecurityListing, sp500: MarketIndex, russell: MarketIndex) -> None:
        r = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="removed",
            effective_date=date(2026, 6, 15),
        )
        record_index_change_leg(
            company=company,
            security_listing=listing2,
            index=russell,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(r.event)
        assert result.displacement == IndexChangeEvent.Displacement.DOWNGRADE

    def test_cross_index(self, company: Company, listing: SecurityListing, listing2: SecurityListing, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        r = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="removed",
            effective_date=date(2026, 6, 15),
        )
        record_index_change_leg(
            company=company,
            security_listing=listing2,
            index=nasdaq100,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(r.event)
        assert result.displacement == IndexChangeEvent.Displacement.CROSS_INDEX


@pytest.mark.django_db
class TestMonitoringScenarios:
    def test_continues_add_another(self, company: Company, listing: SecurityListing, listing2: SecurityListing, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
        r = record_index_change_leg(
            company=company,
            security_listing=listing2,
            index=nasdaq100,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(r.event)
        assert result.monitoring_impact == IndexChangeEvent.MonitoringImpact.CONTINUES

    def test_partial_exit_continues(self, company: Company, listing: SecurityListing, listing2: SecurityListing, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
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
        r = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="removed",
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(r.event)
        assert result.monitoring_impact == IndexChangeEvent.MonitoringImpact.CONTINUES

    def test_reentry(self, company: Company, listing: SecurityListing, sp500: MarketIndex) -> None:
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ENDED,
            effective_from=date(2025, 1, 1),
            effective_to=date(2025, 12, 31),
        )
        r = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(r.event)
        assert result.monitoring_impact == IndexChangeEvent.MonitoringImpact.REENTERS_BASE_POOL

    def test_full_exit(self, company: Company, listing: SecurityListing, sp500: MarketIndex) -> None:
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
        r = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="removed",
            effective_date=date(2026, 6, 15),
        )
        result = classify_index_change_event(r.event)
        assert result.monitoring_impact == IndexChangeEvent.MonitoringImpact.EXITS_BASE_POOL


@pytest.mark.django_db
class TestCrossDateCorrelation:
    def test_correlation_candidate_and_classify(
        self,
        company,
        listing,
        listing2,
        russell,
        sp500,
    ):
        e1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=russell,
            action="removed",
            effective_date=date(2026, 6, 15),
        ).event
        record_index_change_leg(
            company=company,
            security_listing=listing2,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 18),
        )
        gen = generate_index_change_correlation_candidates(e1)
        assert gen.created_count == 1
        corr = gen.candidates[0]
        corr.status = IndexChangeCorrelation.Status.CONFIRMED
        corr.save()
        result = classify_index_change_correlation(corr)
        assert result.displacement == IndexChangeEvent.Displacement.UPGRADE

    def test_eight_day_separation_no_candidate(
        self,
        company,
        listing,
        sp500,
    ):
        e1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        ).event
        record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="removed",
            effective_date=date(2026, 6, 23),
        )
        result = generate_index_change_correlation_candidates(e1)
        assert result.created_count == 0

    def test_backfill_finds_earlier(self, company: Company, listing: SecurityListing, sp500: MarketIndex, russell: MarketIndex) -> None:
        e1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=russell,
            action="added",
            effective_date=date(2026, 6, 15),
        ).event
        e2 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="removed",
            effective_date=date(2026, 6, 18),
        ).event
        result = generate_index_change_correlation_candidates(e2)
        assert result.created_count == 1
        assert result.candidates[0].earlier_event == e1


@pytest.mark.django_db
class TestFutureChange:
    def test_future_accepted_no_membership(self, company: Company, listing: SecurityListing, sp500: MarketIndex) -> None:
        future = date.today() + timedelta(days=60)
        r = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=future,
        )
        assert r.event_created
        classify_index_change_event(r.event)
        assert (
            IndexMembership.objects.filter(
                status=IndexMembership.Status.ACTIVE,
            ).count()
            == 0
        )


@pytest.mark.django_db
class TestCorrectionChain:
    def test_multi_revision(self, company: Company, listing: SecurityListing, listing2: SecurityListing, sp500: MarketIndex, nasdaq100: MarketIndex, russell: MarketIndex) -> None:
        e1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=russell,
            action="removed",
            effective_date=date(2026, 6, 15),
        ).event
        classify_index_change_event(e1)
        r1 = correct_index_change_event(
            e1,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing,
                    index=sp500,
                    action="added",
                ),
            ],
        )
        e1.refresh_from_db()
        r2 = correct_index_change_event(
            r1.new_event,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing2,
                    index=nasdaq100,
                    action="added",
                ),
            ],
        )
        e1.refresh_from_db()
        r1.new_event.refresh_from_db()
        assert e1.status == IndexChangeEvent.Status.CORRECTED
        assert r1.new_event.status == IndexChangeEvent.Status.CORRECTED
        assert r2.new_event.status == IndexChangeEvent.Status.ACTIVE
        assert r2.new_event.supersedes == r1.new_event
        assert r1.new_event.supersedes == e1
        # Only E3 is ACTIVE for canonical key
        active = IndexChangeEvent.objects.filter(
            company=e1.company,
            effective_date=e1.effective_date,
            status=IndexChangeEvent.Status.ACTIVE,
        )
        assert active.count() == 1
        assert active.first() == r2.new_event

    def test_noop_correction_creates_revision(self, company: Company, listing: SecurityListing, sp500: MarketIndex) -> None:
        """Current semantics: even same-leg correction creates a new revision."""
        e1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        ).event
        result = correct_index_change_event(
            e1,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing,
                    index=sp500,
                    action="added",
                ),
            ],
        )
        e1.refresh_from_db()
        assert e1.status == IndexChangeEvent.Status.CORRECTED
        assert result.new_event.status == IndexChangeEvent.Status.ACTIVE
        assert result.new_event.supersedes == e1

    def test_correction_rollback_on_failure(self, company: Company, listing: SecurityListing, sp500: MarketIndex, nasdaq100: MarketIndex) -> None:
        e1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        ).event
        from django.db import transaction as tx

        from indexes.services import InvalidIndexChangeInput

        with pytest.raises(InvalidIndexChangeInput):
            with tx.atomic():
                correct_index_change_event(
                    e1,
                    corrected_legs=[
                        EventCorrectionLegSpec(
                            security_listing=listing,
                            index=sp500,
                            action="invalid",
                        ),
                    ],
                )
        e1.refresh_from_db()
        assert e1.status == IndexChangeEvent.Status.ACTIVE
        assert IndexChangeEvent.objects.filter(supersedes=e1).count() == 0


@pytest.mark.django_db
class TestHistoricalPreservation:
    def test_correlation_survives_correction(
        self,
        company,
        listing,
        listing2,
        sp500,
        russell,
    ):
        e1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=russell,
            action="removed",
            effective_date=date(2026, 6, 15),
        ).event
        e2 = record_index_change_leg(
            company=company,
            security_listing=listing2,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 18),
        ).event
        corr = IndexChangeCorrelation.objects.create(
            earlier_event=e1,
            later_event=e2,
        )
        correct_index_change_event(
            e1,
            corrected_legs=[
                EventCorrectionLegSpec(
                    security_listing=listing,
                    index=sp500,
                    action="added",
                ),
            ],
        )
        assert IndexChangeCorrelation.objects.filter(pk=corr.pk).exists()
        assert IndexMembership.objects.count() == 0
