from __future__ import annotations

from datetime import date

import pytest

from companies.models import Company, SecurityListing
from indexes.models import IndexMembership, MarketIndex
from indexes.selectors import (
    get_normative_memberships_for_index,
    get_normative_memberships_for_listing,
    get_normative_memberships_in_period,
)


@pytest.fixture
def sp500(db: object) -> MarketIndex:
    del db
    return MarketIndex.objects.get(code="SP500")


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


class TestNormativeSelectors:
    def test_as_of_includes_active(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = get_normative_memberships_for_index(
            index=sp500,
            as_of_date=date(2024, 6, 1),
        )
        assert qs.filter(pk=m.pk).exists()

    def test_as_of_includes_ended_in_period(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ENDED,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 6, 15),
        )
        qs = get_normative_memberships_for_index(
            index=sp500,
            as_of_date=date(2024, 6, 1),
        )
        assert qs.filter(pk=m.pk).exists()

    def test_as_of_excludes_ended_after_period(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ENDED,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 6, 15),
        )
        qs = get_normative_memberships_for_index(
            index=sp500,
            as_of_date=date(2024, 6, 15),
        )
        assert not qs.filter(pk=m.pk).exists()

    def test_as_of_includes_announced_reached_effective(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2024, 6, 1),
        )
        qs = get_normative_memberships_for_index(
            index=sp500,
            as_of_date=date(2024, 6, 15),
        )
        assert qs.filter(pk=m.pk).exists()

    def test_as_of_excludes_cancelled(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CANCELLED,
            effective_from=date(2024, 1, 1),
        )
        qs = get_normative_memberships_for_index(
            index=sp500,
            as_of_date=date(2024, 6, 1),
        )
        assert not qs.filter(pk=m.pk).exists()

    def test_as_of_excludes_corrected(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CORRECTED,
            effective_from=date(2024, 1, 1),
        )
        qs = get_normative_memberships_for_index(
            index=sp500,
            as_of_date=date(2024, 6, 1),
        )
        assert not qs.filter(pk=m.pk).exists()

    def test_listing_selector(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = get_normative_memberships_for_listing(
            security_listing_id=listing.pk,
            as_of_date=date(2024, 6, 1),
        )
        assert qs.filter(pk=m.pk).exists()

    def test_period_selector(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 6, 30),
        )
        qs = get_normative_memberships_in_period(
            index=sp500,
            from_date=date(2024, 3, 1),
            to_date=date(2024, 9, 1),
        )
        assert qs.filter(pk=m.pk).exists()
