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
    """Membership vs Listing concurrent writes with truly overlapping transactions."""

    def test_membership_insert_races_with_listing_shorten(
        self,
        sp500: MarketIndex,
        company: Company,
    ) -> None:
        """Transaction A inserts membership; B shortens listing. A must be rejected.

        Deterministic interleaving via threading.Event:
        1. A inserts membership (uncommitted), signals membership_inserted, waits
        2. B shortens listing, commits, signals listing_committed
        3. A tries to commit → deferred trigger re-reads listing → IntegrityError
        """
        import threading

        from django.db import close_old_connections, connections, transaction

        listing = SecurityListing.objects.create(
            company=company,
            ticker="CTEST",
            exchange="NYSE",
            effective_from=date(2020, 1, 1),
        )

        membership_inserted = threading.Event()
        listing_committed = threading.Event()
        errors_a: list[BaseException] = []
        errors_b: list[BaseException] = []

        def transaction_a() -> None:
            """Insert membership spanning old listing boundary, then wait for B."""
            close_old_connections()
            try:
                with transaction.atomic():
                    from django.db import connection as local_conn

                    with local_conn.cursor() as c:
                        c.execute("SET LOCAL lock_timeout = '5s'")
                    IndexMembership.objects.create(
                        index=sp500,
                        security_listing=listing,
                        status=IndexMembership.Status.ACTIVE,
                        effective_from=date(2022, 1, 1),
                        effective_to=date(2027, 12, 31),
                    )
                    membership_inserted.set()
                    if not listing_committed.wait(timeout=10):
                        raise TimeoutError("Transaction A: timed out waiting for B")
                # COMMIT happens here → trigger fires → should fail
            except BaseException as e:
                errors_a.append(e)
            finally:
                membership_inserted.set()  # ensure B is never stuck
                for conn in connections.all():
                    conn.close()

        def transaction_b() -> None:
            """Shorten listing after A has inserted its membership (uncommitted)."""
            close_old_connections()
            try:
                if not membership_inserted.wait(timeout=10):
                    raise TimeoutError("Transaction B: timed out waiting for A")
                with transaction.atomic():
                    obj = SecurityListing.objects.get(pk=listing.pk)
                    obj.effective_to = date(2024, 12, 31)
                    obj.save(update_fields=["effective_to"])
                listing_committed.set()
            except BaseException as e:
                errors_b.append(e)
            finally:
                listing_committed.set()  # ensure A is never stuck
                for conn in connections.all():
                    conn.close()

        t_a = threading.Thread(target=transaction_a)
        t_b = threading.Thread(target=transaction_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=20)
        t_b.join(timeout=20)

        assert not t_a.is_alive(), "Thread A hung"
        assert not t_b.is_alive(), "Thread B hung"

        for conn in connections.all():
            conn.close()
        close_old_connections()

        # B must succeed with no errors
        assert errors_b == [], f"Transaction B should have no errors, got {errors_b}"
        listing.refresh_from_db()
        assert listing.effective_to == date(2024, 12, 31)

        # A must fail with exactly one IntegrityError
        assert len(errors_a) == 1, f"Expected exactly 1 error in A, got {len(errors_a)}"
        assert isinstance(errors_a[0], IntegrityError), (
            f"Expected IntegrityError, got {type(errors_a[0]).__name__}: {errors_a[0]}"
        )

        # No normative membership violates boundaries
        bad = IndexMembership.objects.filter(
            security_listing=listing,
            status__in=NORMATIVE_MEMBERSHIP_STATUSES,
            effective_to__gt=date(2024, 12, 31),
        )
        assert not bad.exists(), "No normative membership should exceed listing boundary"
