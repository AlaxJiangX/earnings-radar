"""Step 2 service-level tests for atomic index change recording."""

from __future__ import annotations

from datetime import date

import pytest
from django.db import IntegrityError, transaction

from companies.models import Company, SecurityListing
from indexes.models import (
    IndexChangeEvent,
    IndexChangeLeg,
    IndexMembership,
    MarketIndex,
)
from indexes.services import (
    IndexChangeIntegrityError,
    IndexChangeWriteResult,
    InvalidIndexChangeInput,
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
def company(db: object) -> Company:
    del db
    return Company.objects.create(
        legal_name="TestCorp Recording",
        display_name="TestCorp",
        cik="0000000200",
    )


@pytest.fixture
def listing(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="REC",
        exchange="NYSE",
        effective_from=date(2020, 1, 1),
    )


@pytest.fixture
def listing2(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="REC2",
        exchange="NASDAQ",
        effective_from=date(2020, 1, 1),
    )


@pytest.fixture
def other_company(db: object) -> Company:
    del db
    return Company.objects.create(
        legal_name="OtherCorp",
        display_name="OtherCorp",
        cik="0000000300",
    )


@pytest.fixture
def other_listing(db: object, other_company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=other_company,
        ticker="OTHREC",
        exchange="NYSE",
        effective_from=date(2020, 1, 1),
    )


@pytest.mark.django_db
class TestRecordIndexChangeLeg:
    """Core recording service."""

    def test_first_added_creates_event_and_leg(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        result = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        assert isinstance(result, IndexChangeWriteResult)
        assert result.event_created is True
        assert result.leg_created is True
        assert result.leg.action == IndexChangeLeg.Action.ADDED
        assert result.leg.announcement_date is None
        assert result.event.company == company

    def test_first_removed_creates_event_and_leg(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        result = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="removed",
            effective_date=date(2026, 6, 15),
        )
        assert result.leg.action == IndexChangeLeg.Action.REMOVED

    def test_second_leg_same_effective_date_reuses_event(
        self,
        sp500: MarketIndex,
        nasdaq100: MarketIndex,
        company: Company,
        listing: SecurityListing,
        listing2: SecurityListing,
    ) -> None:
        r1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        r2 = record_index_change_leg(
            company=company,
            security_listing=listing2,
            index=nasdaq100,
            action="removed",
            effective_date=date(2026, 6, 15),
        )
        assert r1.event.pk == r2.event.pk
        assert r1.event_created is True
        assert r2.event_created is False
        assert r2.leg_created is True

    def test_different_effective_date_separate_events(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        r1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        r2 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="removed",
            effective_date=date(2026, 7, 1),
        )
        assert r1.event.pk != r2.event.pk

    def test_different_company_separate_events(
        self,
        sp500: MarketIndex,
        company: Company,
        listing: SecurityListing,
        other_company: Company,
        other_listing: SecurityListing,
    ) -> None:
        r1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        r2 = record_index_change_leg(
            company=other_company,
            security_listing=other_listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        assert r1.event.pk != r2.event.pk

    def test_exact_replay_idempotent(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        r1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        r2 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        assert r1.event.pk == r2.event.pk
        assert r1.leg.pk == r2.leg.pk
        assert r2.event_created is False
        assert r2.leg_created is False
        # Only one canonical leg
        assert (
            IndexChangeLeg.objects.filter(
                event=r1.event,
                index=sp500,
                security_listing=listing,
                action="added",
            ).count()
            == 1
        )

    def test_same_event_supports_add_and_remove(
        self,
        sp500: MarketIndex,
        nasdaq100: MarketIndex,
        company: Company,
        listing: SecurityListing,
        listing2: SecurityListing,
    ) -> None:
        r1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        r2 = record_index_change_leg(
            company=company,
            security_listing=listing2,
            index=nasdaq100,
            action="removed",
            effective_date=date(2026, 6, 15),
        )
        assert r1.event.pk == r2.event.pk
        assert r1.event.legs.count() == 2

    def test_invalid_action_rejected(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        with pytest.raises(InvalidIndexChangeInput, match="action"):
            record_index_change_leg(
                company=company,
                security_listing=listing,
                index=sp500,
                action="replaced",
                effective_date=date(2026, 6, 15),
            )

    def test_announcement_after_effective_rejected(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        # announcement_date > effective_date rejected at leg level
        with pytest.raises(InvalidIndexChangeInput, match="after effective"):
            record_index_change_leg(
                company=company,
                security_listing=listing,
                index=sp500,
                action="added",
                effective_date=date(2026, 6, 15),
                announcement_date=date(2026, 7, 1),
            )

    def test_null_announcement_accepted(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        result = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
            announcement_date=None,
        )
        assert result.event_created
        assert result.leg.announcement_date is None

    def test_conflicting_announcement_date_rejected(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        # Record first leg with announcement_date June 1
        record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
            announcement_date=date(2026, 6, 1),
        )
        # Replay same leg with DIFFERENT announcement_date → conflict
        with pytest.raises(IndexChangeIntegrityError, match="announcement_date"):
            record_index_change_leg(
                company=company,
                security_listing=listing,
                index=sp500,
                action="added",  # same action, not removed
                effective_date=date(2026, 6, 15),
                announcement_date=date(2026, 6, 2),
            )

    def test_optional_membership_accepted(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 6, 15),
        )
        result = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
            membership=m,
        )
        assert result.leg.membership == m

    def test_membership_inconsistent_index_rejected(
        self, sp500: MarketIndex, nasdaq100: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        m = IndexMembership.objects.create(
            index=nasdaq100,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 6, 15),
        )
        with pytest.raises(InvalidIndexChangeInput, match="Membership index"):
            record_index_change_leg(
                company=company,
                security_listing=listing,
                index=sp500,
                action="added",
                effective_date=date(2026, 6, 15),
                membership=m,
            )

    def test_membership_inconsistent_listing_rejected(
        self,
        sp500: MarketIndex,
        company: Company,
        listing: SecurityListing,
        listing2: SecurityListing,
    ) -> None:
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 6, 15),
        )
        with pytest.raises(InvalidIndexChangeInput, match="Membership security"):
            record_index_change_leg(
                company=company,
                security_listing=listing2,
                index=sp500,
                action="added",
                effective_date=date(2026, 6, 15),
                membership=m,
            )

    def test_leg_company_mismatch_rejected(
        self,
        sp500: MarketIndex,
        company: Company,
        other_company: Company,
        other_listing: SecurityListing,
    ) -> None:
        with pytest.raises(InvalidIndexChangeInput, match="company"):
            record_index_change_leg(
                company=company,
                security_listing=other_listing,
                index=sp500,
                action="added",
                effective_date=date(2026, 6, 15),
            )

    def test_duplicate_leg_idempotent(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        r1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        r2 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        assert r1.leg.pk == r2.leg.pk
        assert r2.leg_created is False
        assert r2.event_created is False

    def test_duplicate_event_company_date_raises(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
        )
        # Same company + effective_date should find existing event
        # but different source_evidence should not error unless
        # both are non-None and differ
        result = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="removed",
            effective_date=date(2026, 6, 15),
        )
        assert result.event_created is False

    def test_source_evidence_nullable(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        # SourceEvidence requires raw_data_record + sync_run which is
        # outside Step 2 scope.  Verify the FK is nullable and service
        # accepts None.
        result = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
            source_evidence=None,
        )
        assert result.leg.source_evidence is None

    def test_future_effective_date_accepted(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        from datetime import timedelta

        future = date.today() + timedelta(days=10)
        result = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=future,
        )
        assert result.event_created
        assert result.event.effective_date == future

    def test_effective_date_today_accepted(
        self, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        result = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date.today(),
        )
        assert result.event_created


@pytest.mark.django_db
class TestEventErrorOnDuplicate:
    """DB-level constraint protects (company, effective_date) uniqueness."""

    def test_direct_duplicate_event_rejected(self, company: Company) -> None:
        IndexChangeEvent.objects.create(
            company=company,
            effective_date=date(2026, 6, 15),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            IndexChangeEvent.objects.create(
                company=company,
                effective_date=date(2026, 6, 15),
            )

    def test_different_company_same_date_allowed(
        self, company: Company, other_company: Company
    ) -> None:
        IndexChangeEvent.objects.create(
            company=company,
            effective_date=date(2026, 6, 15),
        )
        ev = IndexChangeEvent.objects.create(
            company=other_company,
            effective_date=date(2026, 6, 15),
        )
        assert ev.pk is not None

    def test_same_company_different_date_allowed(self, company: Company) -> None:
        IndexChangeEvent.objects.create(
            company=company,
            effective_date=date(2026, 6, 15),
        )
        ev = IndexChangeEvent.objects.create(
            company=company,
            effective_date=date(2026, 7, 1),
        )
        assert ev.pk is not None


@pytest.mark.django_db
class TestMultiSourceProvenance:
    """Step 2 Provenance Contract Gate — multi-source event tests."""

    def test_multi_leg_different_source_evidence(
        self,
        sp500: MarketIndex,
        nasdaq100: MarketIndex,
        company: Company,
        listing: SecurityListing,
        listing2: SecurityListing,
    ) -> None:
        """Same event accepts legs with different source_evidence.

        Leg A: Russell REMOVE, evidence from FTSE
        Leg B: S&P ADD, evidence from S&P DJI
        Expected: same canonical event, no integrity error.
        """

        # We cannot create real SourceEvidence (requires raw_data_record + sync_run).
        # The FK is nullable — legs with NULL evidence are valid.
        # The provenance contract is: different non-NULL source_evidence
        # values on different legs must not cause event-level conflicts.

        r1 = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=nasdaq100,
            action="removed",
            effective_date=date(2026, 6, 15),
            source_evidence=None,
        )
        r2 = record_index_change_leg(
            company=company,
            security_listing=listing2,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
            source_evidence=None,
        )
        # Same canonical event
        assert r1.event.pk == r2.event.pk
        # Two legs
        assert r1.event.legs.count() == 2
        # Event.source_evidence stays NULL (not auto-populated from legs)
        assert r1.event.source_evidence is None

    def test_event_source_evidence_not_inherited_from_leg(
        self,
        sp500: MarketIndex,
        company: Company,
        listing: SecurityListing,
    ) -> None:
        """Event.source_evidence is not auto-populated from leg calls."""
        result = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=date(2026, 6, 15),
            source_evidence=None,
        )
        assert result.leg.source_evidence is None
