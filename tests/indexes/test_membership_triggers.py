from __future__ import annotations

from datetime import date

import pytest
from django.db import IntegrityError, connection, transaction

from companies.models import Company, SecurityListing
from indexes.models import NORMATIVE_MEMBERSHIP_STATUSES, IndexMembership, MarketIndex


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
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status=IndexMembership.Status.ACTIVE,
                    effective_from=date(2019, 1, 1),
                )

    def test_effective_to_exceeds_listing_rejected(
        self, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        listing.effective_to = date(2030, 12, 31)
        listing.save(update_fields=["effective_to"])
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status=IndexMembership.Status.ACTIVE,
                    effective_from=date(2025, 1, 1),
                    effective_to=date(2035, 1, 1),
                )

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
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=ended_listing,
                    status=IndexMembership.Status.ACTIVE,
                    effective_from=date(2024, 1, 1),
                )

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
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                listing.effective_from = date(2025, 1, 1)
                listing.save(update_fields=["effective_from"])

    def test_effective_to_shortened_past_membership_rejected(
        self, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2025, 1, 1),
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                listing.effective_to = date(2024, 12, 31)
                listing.save(update_fields=["effective_to"])

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


@pytest.mark.django_db(transaction=True)
class TestMigrationTriggers:
    """Verify that migration 0003→0004→0003→0004 properly creates/removes triggers."""

    def test_trigger_migration_roundtrip(self) -> None:
        from django.core.management import call_command

        # Forward: 0004 should create triggers
        call_command("migrate", "indexes", "0004_constraint_triggers", verbosity=0)

        with connection.cursor() as c:
            c.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_membership_within_listing'"
            )
            assert c.fetchone()[0] >= 1, "Membership trigger should exist after 0004"
            c.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname = 'trg_listing_boundaries_memberships'"
            )
            assert c.fetchone()[0] >= 1, "Listing trigger should exist after 0004"
            c.execute(
                "SELECT count(*) FROM pg_proc WHERE proname = 'check_membership_within_listing'"
            )
            assert c.fetchone()[0] >= 1, "Membership function should exist"
            c.execute(
                "SELECT count(*) FROM pg_proc "
                "WHERE proname = 'check_listing_boundaries_memberships'"
            )
            assert c.fetchone()[0] >= 1, "Listing function should exist"

        # Reverse: 0004→0003 should drop triggers and functions
        call_command("migrate", "indexes", "0003_index_membership", verbosity=0)

        with connection.cursor() as c:
            c.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_membership_within_listing'"
            )
            assert c.fetchone()[0] == 0, "Membership trigger should be removed after reverse"
            c.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname = 'trg_listing_boundaries_memberships'"
            )
            assert c.fetchone()[0] == 0, "Listing trigger should be removed after reverse"
            c.execute(
                "SELECT count(*) FROM pg_proc WHERE proname = 'check_membership_within_listing'"
            )
            assert c.fetchone()[0] == 0, "Membership function should be removed"
            c.execute(
                "SELECT count(*) FROM pg_proc "
                "WHERE proname = 'check_listing_boundaries_memberships'"
            )
            assert c.fetchone()[0] == 0, "Listing function should be removed"

        # Re-apply 0004
        call_command("migrate", "indexes", "0004_constraint_triggers", verbosity=0)

        with connection.cursor() as c:
            c.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_membership_within_listing'"
            )
            assert c.fetchone()[0] >= 1, "Membership trigger should be re-created"
            c.execute(
                "SELECT count(*) FROM pg_proc WHERE proname = 'check_membership_within_listing'"
            )
            assert c.fetchone()[0] >= 1, "Membership function should be re-created"


@pytest.mark.django_db(transaction=True)
class TestCrossTableConcurrency:
    """Membership vs Listing concurrent writes – trigger rejects violation."""

    def test_listing_truncation_rejects_stale_membership(
        self,
        sp500: MarketIndex,
        company: Company,
    ) -> None:
        """B shortens listing; A inserts membership spanning old boundary → rejected."""
        import threading

        from django.db import close_old_connections, connections

        listing = SecurityListing.objects.create(
            company=company,
            ticker="CTEST",
            exchange="NYSE",
            effective_from=date(2020, 1, 1),
        )

        errors: list[Exception] = []
        results: list[bool] = []

        def insert_membership() -> None:
            close_old_connections()
            try:
                IndexMembership.objects.create(
                    index=sp500,
                    security_listing=listing,
                    status=IndexMembership.Status.ACTIVE,
                    effective_from=date(2022, 1, 1),
                    effective_to=date(2027, 12, 31),
                )
                results.append(True)
            except Exception as e:
                errors.append(e)
            finally:
                for conn in connections.all():
                    conn.close()

        def shorten_listing() -> None:
            close_old_connections()
            try:
                obj = SecurityListing.objects.get(pk=listing.pk)
                obj.effective_to = date(2024, 12, 31)
                obj.save(update_fields=["effective_to"])
                results.append(True)
            except Exception as e:
                errors.append(e)
            finally:
                for conn in connections.all():
                    conn.close()

        # B shortens listing first
        t_b = threading.Thread(target=shorten_listing)
        t_b.start()
        t_b.join(timeout=10)

        # Then A tries to insert membership that would span beyond
        t_a = threading.Thread(target=insert_membership)
        t_a.start()
        t_a.join(timeout=10)

        for conn in connections.all():
            conn.close()
        close_old_connections()

        # B must succeed
        listing.refresh_from_db()
        assert listing.effective_to == date(2024, 12, 31), "Listing shortening should succeed"

        # A must fail because membership would exceed listing boundary
        assert len(errors) >= 1, "Membership insert should be rejected"
        assert any(isinstance(e, IntegrityError) for e in errors), (
            f"Expected IntegrityError, got {errors}"
        )

        # No normative membership violates boundaries
        bad = IndexMembership.objects.filter(
            security_listing=listing,
            status__in=NORMATIVE_MEMBERSHIP_STATUSES,
            effective_to__gt=date(2024, 12, 31),
        )
        assert not bad.exists(), "No normative membership should exceed listing boundary"
