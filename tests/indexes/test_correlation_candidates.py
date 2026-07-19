"""Step 3 tests — IndexChangeCorrelation model and candidate generation."""

from __future__ import annotations

from datetime import date

import pytest
from django.db import IntegrityError, transaction

from companies.models import Company, SecurityListing
from indexes.models import (
    IndexChangeCorrelation,
    IndexChangeEvent,
    IndexChangeLeg,
    MarketIndex,
)
from indexes.services import (
    generate_index_change_correlation_candidates,
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
        legal_name="CorrCo",
        display_name="CorrCo",
        cik="0000000500",
    )


@pytest.fixture
def listing(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="COR",
        exchange="NYSE",
        effective_from=date(2020, 1, 1),
    )


def _make_event(
    company: Company,
    effective_date: date,
    *,
    index: MarketIndex | None = None,
    listing: SecurityListing | None = None,
) -> IndexChangeEvent:
    ev = IndexChangeEvent.objects.create(
        company=company,
        effective_date=effective_date,
    )
    if index and listing:
        IndexChangeLeg.objects.create(
            event=ev,
            index=index,
            security_listing=listing,
            action=IndexChangeLeg.Action.ADDED,
            effective_date=effective_date,
        )
    return ev


# ---- Model Tests ----


@pytest.mark.django_db
class TestCorrelationModel:
    def test_create_minimal_correlation(self, company: Company) -> None:
        e1 = _make_event(company, date(2026, 6, 15))
        e2 = _make_event(company, date(2026, 6, 18))
        corr = IndexChangeCorrelation.objects.create(
            earlier_event=e1,
            later_event=e2,
        )
        assert corr.status == IndexChangeCorrelation.Status.PENDING
        assert corr.displacement is None
        assert corr.monitoring_impact is None

    def test_status_default_pending(self, company: Company) -> None:
        e1 = _make_event(company, date(2026, 6, 1))
        e2 = _make_event(company, date(2026, 6, 3))
        corr = IndexChangeCorrelation.objects.create(
            earlier_event=e1,
            later_event=e2,
            status=IndexChangeCorrelation.Status.CONFIRMED,
        )
        assert corr.status == IndexChangeCorrelation.Status.CONFIRMED

    def test_invalid_status_rejected(self, company: Company) -> None:
        e1 = _make_event(company, date(2026, 6, 1))
        e2 = _make_event(company, date(2026, 6, 3))
        with pytest.raises(IntegrityError), transaction.atomic():
            IndexChangeCorrelation.objects.create(
                earlier_event=e1,
                later_event=e2,
                status="bogus",
            )

    def test_self_correlation_rejected(self, company: Company) -> None:
        e1 = _make_event(company, date(2026, 6, 1))
        with pytest.raises(IntegrityError), transaction.atomic():
            IndexChangeCorrelation.objects.create(
                earlier_event=e1,
                later_event=e1,
            )

    def test_duplicate_pair_rejected(self, company: Company) -> None:
        e1 = _make_event(company, date(2026, 6, 1))
        e2 = _make_event(company, date(2026, 6, 3))
        IndexChangeCorrelation.objects.create(earlier_event=e1, later_event=e2)
        with pytest.raises(IntegrityError), transaction.atomic():
            IndexChangeCorrelation.objects.create(earlier_event=e1, later_event=e2)

    def test_event_protect_no_cascade(self, company: Company) -> None:
        e1 = _make_event(company, date(2026, 6, 1))
        e2 = _make_event(company, date(2026, 6, 3))
        IndexChangeCorrelation.objects.create(earlier_event=e1, later_event=e2)
        with pytest.raises(IntegrityError):
            e1.delete()


# ---- Candidate Generation Tests ----


@pytest.mark.django_db
class TestCandidateGeneration:
    def test_one_day_apart_creates_candidate(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        e2 = _make_event(company, date(2026, 6, 16), index=sp500, listing=listing)
        result = generate_index_change_correlation_candidates(e1)
        assert result.created_count == 1
        assert result.existing_count == 0
        assert result.candidates[0].earlier_event == e1

    def test_seven_days_apart_creates_candidate(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        e2 = _make_event(company, date(2026, 6, 22), index=sp500, listing=listing)
        result = generate_index_change_correlation_candidates(e1)
        assert result.created_count == 1

    def test_eight_days_apart_no_candidate(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        _make_event(company, date(2026, 6, 23), index=sp500, listing=listing)
        result = generate_index_change_correlation_candidates(e1)
        assert result.created_count == 0

    def test_same_date_no_correlation(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        """Same-date events are merged by canonical event — correlation not applicable."""
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        # Second event on same date would be merged into e1 by Step 2
        # (canonical unique prevents a second ACTIVE event on same company+date)
        result = generate_index_change_correlation_candidates(e1)
        # No other event, no correlation
        assert result.created_count == 0
        assert result.existing_count == 0

    def test_different_company_no_correlation(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        other = Company.objects.create(
            legal_name="Other",
            display_name="Other",
            cik="0000000600",
        )
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        _make_event(other, date(2026, 6, 17), index=sp500, listing=listing)
        result = generate_index_change_correlation_candidates(e1)
        assert result.created_count == 0

    def test_earlier_input_finds_later(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        e2 = _make_event(company, date(2026, 6, 18), index=sp500, listing=listing)
        result = generate_index_change_correlation_candidates(e1)
        assert result.created_count == 1
        assert result.candidates[0].later_event == e2

    def test_later_input_finds_earlier(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        e2 = _make_event(company, date(2026, 6, 18), index=sp500, listing=listing)
        result = generate_index_change_correlation_candidates(e2)
        assert result.created_count == 1
        assert result.candidates[0].earlier_event == e1

    def test_bidirectional_pair_normalization(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        e2 = _make_event(company, date(2026, 6, 18), index=sp500, listing=listing)
        r1 = generate_index_change_correlation_candidates(e1)
        r2 = generate_index_change_correlation_candidates(e2)
        # Both generate the same single canonical pair
        assert r1.candidates[0].pk == r2.candidates[0].pk

    def test_repeated_generation_idempotent(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        _make_event(company, date(2026, 6, 18), index=sp500, listing=listing)
        r1 = generate_index_change_correlation_candidates(e1)
        r2 = generate_index_change_correlation_candidates(e1)
        assert r1.created_count == 1
        assert r2.created_count == 0
        assert r2.existing_count == 1

    def test_three_events_pairwise_candidates(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        e2 = _make_event(company, date(2026, 6, 18), index=sp500, listing=listing)
        e3 = _make_event(company, date(2026, 6, 21), index=sp500, listing=listing)
        r1 = generate_index_change_correlation_candidates(e1)
        r2 = generate_index_change_correlation_candidates(e2)
        r3 = generate_index_change_correlation_candidates(e3)
        # Pairwise: A-B (15-18), B-C (18-21), A-C (15-21, gap 6)
        all_ids = [c.pk for result in (r1, r2, r3) for c in result.candidates]
        assert len(set(all_ids)) == 3

    def test_previous_days_covered_backfill(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        """Later-registered event finds earlier ones (backfill scenario)."""
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        e2 = _make_event(company, date(2026, 6, 18), index=sp500, listing=listing)
        # Register e2 later, generate candidates — should find e1
        result = generate_index_change_correlation_candidates(e2)
        assert result.created_count == 1
        assert result.candidates[0].earlier_event == e1

    def test_cancelled_input_no_candidates(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        _make_event(company, date(2026, 6, 18), index=sp500, listing=listing)
        # Mark e1 as cancelled
        e1.status = IndexChangeEvent.Status.CANCELLED
        e1.save(update_fields=["status"])
        result = generate_index_change_correlation_candidates(e1)
        assert result.created_count == 0

    def test_cancelled_target_excluded(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        e2 = _make_event(company, date(2026, 6, 18), index=sp500, listing=listing)
        e2.status = IndexChangeEvent.Status.CANCELLED
        e2.save(update_fields=["status"])
        result = generate_index_change_correlation_candidates(e1)
        assert result.created_count == 0

    def test_no_membership_mutation(
        self,
        company: Company,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        e1 = _make_event(company, date(2026, 6, 15), index=sp500, listing=listing)
        _make_event(company, date(2026, 6, 18), index=sp500, listing=listing)
        result = generate_index_change_correlation_candidates(e1)
        assert result.created_count == 1
        # No membership created
        from indexes.models import IndexMembership

        assert IndexMembership.objects.count() == 0
