from __future__ import annotations

from datetime import date

import pytest
from django.db import IntegrityError, transaction

from companies.models import Company, SecurityListing
from indexes.models import IndexMembership, MarketIndex


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
        legal_name="Test Corp",
        display_name="Test Corp",
        cik="0000000001",
    )


@pytest.fixture
def listing(db: object, company: Company) -> SecurityListing:
    del db
    return SecurityListing.objects.create(
        company=company,
        ticker="TEST",
        exchange="NYSE",
        effective_from=date(2020, 1, 1),
    )


class TestIndexMembershipModel:
    def test_create_announced_membership(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2024, 6, 1),
        )
        assert m.status == "announced"
        assert m.effective_from == date(2024, 6, 1)
        assert m.effective_to is None

    def test_status_must_be_valid(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status="invalid_status",
                    effective_from=date(2024, 1, 1),
                )

    def test_effective_to_must_be_after_effective_from(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status=IndexMembership.Status.ACTIVE,
                    effective_from=date(2024, 6, 1),
                    effective_to=date(2024, 1, 1),
                )

    def test_ended_must_have_effective_to(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status=IndexMembership.Status.ENDED,
                    effective_from=date(2024, 1, 1),
                )

    def test_announcement_date_not_after_effective_from(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status=IndexMembership.Status.ANNOUNCED,
                    effective_from=date(2024, 1, 1),
                    announcement_date=date(2024, 6, 1),
                )

    def test_normative_identity_unique(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2024, 1, 1),
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status=IndexMembership.Status.ACTIVE,
                    effective_from=date(2024, 1, 1),
                )

    def test_normative_overlap_rejected(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 6, 1),
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status=IndexMembership.Status.ACTIVE,
                    effective_from=date(2024, 3, 1),
                )

    def test_cancelled_not_in_normative_constraints(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CANCELLED,
            effective_from=date(2024, 1, 1),
        )
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )

    def test_corrected_not_in_normative_constraints(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CORRECTED,
            effective_from=date(2024, 1, 1),
        )
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )

    def test_supersedes_one_to_one(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CORRECTED,
            effective_from=date(2024, 1, 1),
        )
        replacement = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            supersedes=old,
        )
        assert replacement.supersedes == old
        assert old.superseded_by == replacement

    def test_supersedes_protects_old(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CORRECTED,
            effective_from=date(2024, 1, 1),
        )
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            supersedes=old,
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                old.delete()

    def test_str_representation(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2024, 6, 1),
        )
        assert "SP500" in str(m)
        assert "TEST" in str(m)
        assert "announced" in str(m)

    def test_normative_overlap_across_indexes_allowed(
        self,
        db: object,
        sp500: MarketIndex,
        nasdaq100: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        del db
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 6, 1),
        )
        IndexMembership.objects.create(
            index=nasdaq100,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 3, 1),
        )
