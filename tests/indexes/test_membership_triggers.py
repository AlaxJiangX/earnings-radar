from __future__ import annotations

from datetime import date

import pytest
from django.db import connection, transaction

from companies.models import Company, SecurityListing
from indexes.models import IndexMembership, MarketIndex


@pytest.fixture
def sp500() -> MarketIndex:
    obj, _created = MarketIndex.objects.get_or_create(
        code="SP500",
        defaults={"name": "S&P 500", "index_group": "LARGE", "is_enabled": True},
    )
    return obj


@pytest.fixture
def company() -> Company:
    return Company.objects.create(
        legal_name="Test Corp",
        display_name="Test Corp",
        cik="0000000001",
    )


@pytest.fixture
def listing(company: Company) -> SecurityListing:
    return SecurityListing.objects.create(
        company=company,
        ticker="TEST",
        exchange="NYSE",
        effective_from=date(2020, 1, 1),
    )


@pytest.mark.django_db(transaction=True)
class TestMembershipTrigger:
    def test_effective_from_before_listing_rejected(
        self, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        violation_caught = False
        try:
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status=IndexMembership.Status.ACTIVE,
                    effective_from=date(2019, 1, 1),
                )
        except Exception:
            violation_caught = True
        assert violation_caught, "Expected trigger violation was not raised"

    def test_effective_to_exceeds_listing_rejected(
        self, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        # Create a listing with a bounded effective_to for this test
        listing.effective_to = date(2030, 12, 31)
        listing.save(update_fields=["effective_to"])
        violation_caught = False
        try:
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status=IndexMembership.Status.ACTIVE,
                    effective_from=date(2025, 1, 1),
                    effective_to=date(2035, 1, 1),
                )
        except Exception:
            violation_caught = True
        assert violation_caught, "Expected trigger violation was not raised"

    def test_no_effective_to_when_listing_ended_rejected(
        self, sp500: MarketIndex, company: Company
    ) -> None:
        ended_listing = SecurityListing.objects.create(
            company=company,
            ticker="ENDED",
            exchange="NYSE",
            effective_from=date(2020, 1, 1),
            effective_to=date(2024, 12, 31),
        )
        violation_caught = False
        try:
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=ended_listing,
                    status=IndexMembership.Status.ACTIVE,
                    effective_from=date(2024, 1, 1),
                )
        except Exception:
            violation_caught = True
        assert violation_caught, "Expected trigger violation was not raised"

    def test_corrected_bypasses_trigger(self, sp500: MarketIndex, listing: SecurityListing) -> None:
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CORRECTED,
            effective_from=date(2019, 1, 1),
        )
        assert m.pk is not None

    def test_cancelled_bypasses_trigger(self, sp500: MarketIndex, listing: SecurityListing) -> None:
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CANCELLED,
            effective_from=date(2035, 1, 1),
            effective_to=date(2040, 1, 1),
        )
        assert m.pk is not None


@pytest.mark.django_db(transaction=True)
class TestListingTrigger:
    def test_effective_from_pushed_past_membership_rejected(
        self, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2022, 1, 1),
        )
        violation_caught = False
        try:
            with transaction.atomic():
                listing.effective_from = date(2025, 1, 1)
                listing.save(update_fields=["effective_from"])
        except Exception:
            violation_caught = True
        assert violation_caught, "Expected trigger violation was not raised"

    def test_effective_to_shortened_past_membership_rejected(
        self, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2025, 1, 1),
        )
        violation_caught = False
        try:
            with transaction.atomic():
                listing.effective_to = date(2024, 12, 31)
                listing.save(update_fields=["effective_to"])
        except Exception:
            violation_caught = True
        assert violation_caught, "Expected trigger violation was not raised"

    def test_coordinated_transaction_adjust_membership_then_listing(
        self, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2022, 1, 1),
            effective_to=date(2027, 12, 31),
        )
        with transaction.atomic():
            m.effective_to = date(2024, 6, 30)
            m.save(update_fields=["effective_to"])
            listing.effective_to = date(2024, 12, 31)
            listing.save(update_fields=["effective_to"])

    def test_coordinated_transaction_adjust_listing_then_membership(
        self, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2022, 1, 1),
            effective_to=date(2027, 12, 31),
        )
        with transaction.atomic():
            listing.effective_to = date(2024, 12, 31)
            listing.save(update_fields=["effective_to"])
            m.effective_to = date(2024, 6, 30)
            m.save(update_fields=["effective_to"])

    def test_multiple_updates_checks_final_state(
        self, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2022, 1, 1),
            effective_to=date(2027, 12, 31),
        )
        with transaction.atomic():
            listing.effective_to = date(2023, 12, 31)
            listing.save(update_fields=["effective_to"])
            listing.effective_to = date(2028, 12, 31)
            listing.save(update_fields=["effective_to"])

    def test_triggers_exist_after_migration(
        self, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del sp500, listing
        with connection.cursor() as c:
            c.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_membership_within_listing'"
            )
            assert c.fetchone()[0] >= 1
            c.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname = 'trg_listing_boundaries_memberships'"
            )
            assert c.fetchone()[0] >= 1
