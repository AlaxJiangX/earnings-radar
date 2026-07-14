from __future__ import annotations

from datetime import date

import pytest

from companies.models import Company, SecurityListing
from indexes.models import IndexMembership, MarketIndex
from indexes.selectors import (
    company_indexes_as_of,
    current_listing_indexes_as_of,
    get_normative_memberships_for_index,
    get_normative_memberships_for_listing,
    get_normative_memberships_in_period,
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
        qs = get_normative_memberships_for_index(index=sp500, as_of_date=date(2024, 6, 1))
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
        qs = get_normative_memberships_for_index(index=sp500, as_of_date=date(2024, 6, 1))
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
        qs = get_normative_memberships_for_index(index=sp500, as_of_date=date(2024, 6, 15))
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
        qs = get_normative_memberships_for_index(index=sp500, as_of_date=date(2024, 6, 15))
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
        qs = get_normative_memberships_for_index(index=sp500, as_of_date=date(2024, 6, 1))
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
        qs = get_normative_memberships_for_index(index=sp500, as_of_date=date(2024, 6, 1))
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
            security_listing_id=listing.pk, as_of_date=date(2024, 6, 1)
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
            index=sp500, from_date=date(2024, 3, 1), to_date=date(2024, 9, 1)
        )
        assert qs.filter(pk=m.pk).exists()


class TestCurrentSelectors:
    def test_current_listing_with_enabled_only(
        self,
        db: object,
        sp500: MarketIndex,
        nasdaq100: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        del db
        m1 = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        IndexMembership.objects.create(
            index=nasdaq100,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = current_listing_indexes_as_of(
            security_listing_id=listing.pk, as_of_date=date(2024, 6, 1), is_enabled=True
        )
        assert qs.filter(pk=m1.pk).exists()
        # Both indexes are enabled by default
        assert qs.count() == 2

    def test_current_listing_disabled_excluded(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        sp500.is_enabled = False
        sp500.save(update_fields=["is_enabled"])
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = current_listing_indexes_as_of(
            security_listing_id=listing.pk, as_of_date=date(2024, 6, 1), is_enabled=True
        )
        assert not qs.exists()

    def test_current_listing_disabled_only(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        sp500.is_enabled = False
        sp500.save(update_fields=["is_enabled"])
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = current_listing_indexes_as_of(
            security_listing_id=listing.pk, as_of_date=date(2024, 6, 1), is_enabled=False
        )
        assert qs.filter(pk=m.pk).exists()

    def test_company_indexes_as_of_includes_listing(
        self, db: object, sp500: MarketIndex, company: Company, listing: SecurityListing
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = company_indexes_as_of(
            company_id=company.pk, as_of_date=date(2024, 6, 1), is_enabled=True
        )
        assert qs.filter(pk=m.pk).exists()


class TestMembershipsAsOf:
    def test_all_memberships_as_of(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        from indexes.selectors import memberships_as_of

        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = memberships_as_of(as_of_date=date(2024, 6, 1))
        assert qs.filter(pk=m.pk).exists()

    def test_filter_by_index_code(
        self, db: object, sp500: MarketIndex, nasdaq100: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        from indexes.selectors import memberships_as_of

        m1 = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        IndexMembership.objects.create(
            index=nasdaq100,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = memberships_as_of(as_of_date=date(2024, 6, 1), index_code="SP500")
        assert qs.filter(pk=m1.pk).exists()
        assert qs.count() == 1

    def test_excludes_cancelled(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        from indexes.selectors import memberships_as_of

        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CANCELLED,
            effective_from=date(2024, 1, 1),
        )
        qs = memberships_as_of(as_of_date=date(2024, 6, 1))
        assert not qs.filter(pk=m.pk).exists()

    def test_excludes_corrected(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        from indexes.selectors import memberships_as_of

        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CORRECTED,
            effective_from=date(2024, 1, 1),
        )
        qs = memberships_as_of(as_of_date=date(2024, 6, 1))
        assert not qs.filter(pk=m.pk).exists()


class TestCurrentMemberships:
    def test_enabled_only(self, db: object, sp500: MarketIndex, listing: SecurityListing) -> None:
        del db
        from indexes.selectors import current_memberships

        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = current_memberships(as_of_date=date(2024, 6, 1), is_enabled=True)
        assert qs.filter(pk=m.pk).exists()

    def test_disabled_excluded(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        from indexes.selectors import current_memberships

        sp500.is_enabled = False
        sp500.save(update_fields=["is_enabled"])
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = current_memberships(as_of_date=date(2024, 6, 1), is_enabled=True)
        assert not qs.filter(pk=m.pk).exists()

    def test_disabled_only(self, db: object, sp500: MarketIndex, listing: SecurityListing) -> None:
        del db
        from indexes.selectors import current_memberships

        sp500.is_enabled = False
        sp500.save(update_fields=["is_enabled"])
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = current_memberships(as_of_date=date(2024, 6, 1), is_enabled=False)
        assert qs.filter(pk=m.pk).exists()

    def test_all_mode(
        self, db: object, sp500: MarketIndex, nasdaq100: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        from indexes.selectors import current_memberships

        nasdaq100.is_enabled = False
        nasdaq100.save(update_fields=["is_enabled"])
        m1 = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        m2 = IndexMembership.objects.create(
            index=nasdaq100,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = current_memberships(as_of_date=date(2024, 6, 1), is_enabled=None)
        assert qs.filter(pk=m1.pk).exists()
        assert qs.filter(pk=m2.pk).exists()
        assert qs.count() == 2


class TestListingIndexesAsOf:
    def test_returns_market_index_objects(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        from indexes.selectors import listing_indexes_as_of

        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = listing_indexes_as_of(security_listing_id=listing.pk, as_of_date=date(2024, 6, 1))
        assert qs.filter(pk=sp500.pk).exists()

    def test_enabled_only_excludes_disabled(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        from indexes.selectors import listing_indexes_as_of

        sp500.is_enabled = False
        sp500.save(update_fields=["is_enabled"])
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = listing_indexes_as_of(
            security_listing_id=listing.pk, as_of_date=date(2024, 6, 1), is_enabled=True
        )
        assert not qs.exists()

    def test_disabled_only_includes_disabled(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        from indexes.selectors import listing_indexes_as_of

        sp500.is_enabled = False
        sp500.save(update_fields=["is_enabled"])
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        qs = listing_indexes_as_of(
            security_listing_id=listing.pk, as_of_date=date(2024, 6, 1), is_enabled=False
        )
        assert qs.filter(pk=sp500.pk).exists()
