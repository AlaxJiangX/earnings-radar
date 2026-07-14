from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

import pytest

from audit.models import AuditRecord, SyncRun
from companies.models import Company, SecurityListing
from indexes.models import NORMATIVE_MEMBERSHIP_STATUSES, IndexMembership, MarketIndex
from indexes.selectors import get_normative_memberships_for_listing
from indexes.services import (
    AlreadyCorrected,
    CannotCancelPastEffective,
    InvalidMembershipState,
    MembershipIdentityConflict,
    MembershipServiceError,
    cancel_membership,
    close_memberships_for_listing,
    correct_membership,
    create_index_membership,
    derive_status,
    end_membership,
)

if TYPE_CHECKING:
    from accounts.models import User


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


@pytest.fixture
def sync_run(db: object) -> SyncRun:
    del db
    from audit.models import DataSource

    source = DataSource.objects.create(
        key="test-source",
        name="Test Source",
        source_type="manual",
    )
    return SyncRun.objects.create(
        job_type="test",
        source=source,
        status="running",
    )


class TestDeriveStatus:
    def test_future_effective_is_announced(self) -> None:
        assert derive_status(date(2024, 6, 1), None, date(2024, 1, 1)) == "announced"

    def test_current_no_end_is_active(self) -> None:
        assert derive_status(date(2024, 1, 1), None, date(2024, 6, 1)) == "active"

    def test_past_end_is_ended(self) -> None:
        assert derive_status(date(2024, 1, 1), date(2024, 6, 1), date(2024, 7, 1)) == "ended"

    def test_exact_effective_from_is_active(self) -> None:
        assert derive_status(date(2024, 6, 1), None, date(2024, 6, 1)) == "active"

    def test_exact_effective_to_is_ended(self) -> None:
        assert derive_status(date(2024, 1, 1), date(2024, 6, 1), date(2024, 6, 1)) == "ended"


class TestCreateIndexMembership:
    def test_create_announced(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        result = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2024, 6, 1),
            actor_user=user,
            reason="Test create",
            request_id=str(uuid.uuid4()),
        )
        assert result.created is True
        assert result.membership.status == "announced"
        assert result.audit_record is not None
        assert result.audit_record.action == AuditRecord.Action.CREATE
        assert result.data_changes == ()

    def test_no_initial_data_change_on_create(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        result = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            actor_user=user,
            reason="Test create",
            request_id=str(uuid.uuid4()),
        )
        assert result.data_changes == ()

    def test_idempotent_create(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        req_id = str(uuid.uuid4())
        result1 = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2024, 6, 1),
            actor_user=user,
            reason="Test create",
            request_id=req_id,
        )
        result2 = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2024, 6, 1),
            actor_user=user,
            reason="Test create",
            request_id=req_id,
        )
        assert result2.created is False
        assert result2.membership.pk == result1.membership.pk
        assert result2.audit_record is None

    def test_concurrent_identity_conflict(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2024, 6, 1),
            actor_user=user,
            reason="Test create",
            request_id=str(uuid.uuid4()),
        )
        with pytest.raises(MembershipIdentityConflict):
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 6, 1),
                actor_user=user,
                reason="Different values",
                request_id=str(uuid.uuid4()),
            )

    def test_requires_provenance(
        self, db: object, sp500: MarketIndex, listing: SecurityListing
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="Must provide"):
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ANNOUNCED,
                effective_from=date(2024, 6, 1),
            )


class TestEndMembership:
    def test_end_active_membership(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        end_membership(
            membership=m,
            effective_to=date(2024, 6, 1),
            actor_user=user,
            reason="Test end",
            request_id=str(uuid.uuid4()),
        )
        m.refresh_from_db()
        assert m.status == "ended"
        assert m.effective_to == date(2024, 6, 1)

    def test_end_writes_data_change(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        result = end_membership(
            membership=m,
            effective_to=date(2024, 12, 31),
            actor_user=user,
            reason="Test end",
            request_id=str(uuid.uuid4()),
        )
        assert len(result.data_changes) >= 1
        assert result.audit_record is not None

    def test_cannot_end_ended(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ENDED,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 6, 1),
        )
        with pytest.raises(InvalidMembershipState):
            end_membership(
                membership=m,
                effective_to=date(2024, 12, 31),
                actor_user=user,
                reason="Test end",
                request_id=str(uuid.uuid4()),
            )

    def test_cannot_end_with_invalid_date(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 6, 1),
        )
        with pytest.raises(MembershipServiceError):
            end_membership(
                membership=m,
                effective_to=date(2024, 1, 1),
                actor_user=user,
                reason="Test end",
                request_id=str(uuid.uuid4()),
            )


class TestCancelMembership:
    def test_cancel_future_announced(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2025, 1, 1),
        )
        cancel_membership(
            membership=m,
            as_of_date=date(2024, 6, 1),
            actor_user=user,
            reason="Test cancel",
            request_id=str(uuid.uuid4()),
        )
        m.refresh_from_db()
        assert m.status == "cancelled"

    def test_cannot_cancel_past_effective(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2024, 1, 1),
        )
        with pytest.raises(CannotCancelPastEffective):
            cancel_membership(
                membership=m,
                as_of_date=date(2024, 6, 1),
                actor_user=user,
                reason="Test cancel",
                request_id=str(uuid.uuid4()),
            )

    def test_cannot_cancel_non_announced(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        with pytest.raises(InvalidMembershipState):
            cancel_membership(
                membership=m,
                as_of_date=date(2024, 6, 1),
                actor_user=user,
                reason="Test cancel",
                request_id=str(uuid.uuid4()),
            )

    def test_cancelled_never_in_normative_selector(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2025, 6, 1),
        )
        cancel_membership(
            membership=m,
            as_of_date=date(2024, 1, 1),
            actor_user=user,
            reason="Test cancel",
            request_id=str(uuid.uuid4()),
        )
        qs = get_normative_memberships_for_listing(
            security_listing_id=listing.pk,
            as_of_date=date(2025, 7, 1),
        )
        assert not qs.filter(pk=m.pk).exists()


class TestCorrectMembership:
    def test_correct_active_membership(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        result = correct_membership(
            membership=old,
            replacement_values={
                "effective_to": date(2024, 12, 31),
                "status": IndexMembership.Status.ENDED,
            },
            actor_user=user,
            reason="Wrong dates",
            request_id=str(uuid.uuid4()),
        )
        old.refresh_from_db()
        assert old.status == "corrected"
        assert result.replacement.status == "ended"
        assert result.replacement.supersedes == old
        assert result.replacement.effective_to == date(2024, 12, 31)

    def test_correct_ended_membership(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ENDED,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 6, 1),
        )
        result = correct_membership(
            membership=old,
            replacement_values={"effective_to": date(2024, 12, 31)},
            actor_user=user,
            reason="Wrong end date",
            request_id=str(uuid.uuid4()),
        )
        old.refresh_from_db()
        assert old.status == "corrected"
        assert result.replacement.effective_to == date(2024, 12, 31)

    def test_cannot_correct_already_corrected(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        correct_membership(
            membership=old,
            replacement_values={"effective_to": date(2024, 6, 1)},
            actor_user=user,
            reason="Fix",
            request_id=str(uuid.uuid4()),
        )
        with pytest.raises(AlreadyCorrected):
            correct_membership(
                membership=old,
                replacement_values={"effective_to": date(2024, 12, 31)},
                actor_user=user,
                reason="Fix again",
                request_id=str(uuid.uuid4()),
            )

    def test_cannot_correct_cancelled(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CANCELLED,
            effective_from=date(2024, 1, 1),
        )
        with pytest.raises(InvalidMembershipState):
            correct_membership(
                membership=m,
                replacement_values={},
                actor_user=user,
                reason="Fix",
                request_id=str(uuid.uuid4()),
            )

    def test_correction_chain(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m1 = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        r1 = correct_membership(
            membership=m1,
            replacement_values={"effective_to": date(2024, 6, 30)},
            actor_user=user,
            reason="Fix 1",
            request_id=str(uuid.uuid4()),
        )
        r2 = correct_membership(
            membership=r1.replacement,
            replacement_values={"effective_to": date(2024, 12, 31)},
            actor_user=user,
            reason="Fix 2",
            request_id=str(uuid.uuid4()),
        )
        # Chain: m1 → r1.replacement (M2) → r2.replacement (M3)
        assert r1.replacement.supersedes == m1
        assert r2.replacement.supersedes == r1.replacement


class TestCloseMembershipsForListing:
    def test_close_future_announced_gets_cancelled(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2025, 6, 1),
        )
        results = close_memberships_for_listing(
            listing=listing,
            new_effective_to=date(2024, 12, 31),
            as_of_date=date(2024, 6, 1),
            actor_user=user,
            reason="Listing closed",
            request_id=str(uuid.uuid4()),
        )
        m.refresh_from_db()
        assert m.status == "cancelled"
        assert results[0].action == "cancelled"

    def test_close_active_gets_ended(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        close_memberships_for_listing(
            listing=listing,
            new_effective_to=date(2024, 6, 30),
            as_of_date=date(2024, 12, 1),
            actor_user=user,
            reason="Listing closed",
            request_id=str(uuid.uuid4()),
        )
        m.refresh_from_db()
        assert m.status == "ended"
        assert m.effective_to == date(2024, 6, 30)

    def test_close_ended_past_boundary_gets_corrected(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ENDED,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 12, 31),
        )
        results = close_memberships_for_listing(
            listing=listing,
            new_effective_to=date(2024, 6, 30),
            as_of_date=date(2025, 1, 1),
            actor_user=user,
            reason="Listing shortened",
            request_id=str(uuid.uuid4()),
        )
        m.refresh_from_db()
        assert m.status == "corrected"
        replacement = results[0].replacement
        assert replacement is not None
        assert replacement.effective_to == date(2024, 6, 30)
        assert replacement.supersedes == m

    def test_close_ended_within_boundary_skipped(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ENDED,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 6, 30),
        )
        results = close_memberships_for_listing(
            listing=listing,
            new_effective_to=date(2024, 12, 31),
            as_of_date=date(2025, 1, 1),
            actor_user=user,
            reason="Listing extended",
            request_id=str(uuid.uuid4()),
        )
        assert results[0].action == "skipped"

    def test_close_skips_cancelled(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CANCELLED,
            effective_from=date(2025, 1, 1),
        )
        results = close_memberships_for_listing(
            listing=listing,
            new_effective_to=date(2024, 12, 31),
            as_of_date=date(2024, 6, 1),
            actor_user=user,
            reason="Listing closed",
            request_id=str(uuid.uuid4()),
        )
        # cancelled should not appear in results
        result_ids = [r.membership.pk for r in results]
        assert m.pk not in result_ids

    def test_first_enter_exit_reenter_produces_two_normative(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        # Enter
        m1 = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 6, 30),
        )
        end_membership(
            membership=m1,
            effective_to=date(2024, 6, 30),
            actor_user=user,
            reason="Exit",
            request_id=str(uuid.uuid4()),
        )
        # Re-enter (new effective_from after old effective_to)
        create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 7, 1),
            actor_user=user,
            reason="Re-enter",
            request_id=str(uuid.uuid4()),
        )
        normative = IndexMembership.objects.filter(
            security_listing=listing,
            index=sp500,
            status__in=NORMATIVE_MEMBERSHIP_STATUSES,
        )
        assert normative.count() == 2
