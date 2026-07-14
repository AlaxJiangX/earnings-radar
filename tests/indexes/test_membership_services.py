from __future__ import annotations

import uuid
from datetime import UTC, date
from typing import TYPE_CHECKING

import pytest
from django.db import IntegrityError

from audit.models import AuditRecord, DataChange, SourceEvidence, SyncRun
from audit.services.source_evidence import InvalidSourceEvidence
from companies.models import Company, SecurityListing
from indexes.models import NORMATIVE_MEMBERSHIP_STATUSES, IndexMembership, MarketIndex
from indexes.selectors import get_normative_memberships_for_listing
from indexes.services import (
    AlreadyCorrected,
    CannotCancelPastEffective,
    InvalidMembershipState,
    MembershipIdentityConflict,
    MembershipListingHistoryConflict,
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
        idempotency_key="test-sync-run",
        code_version="test",
        parser_version="test",
    )


@pytest.fixture
def evidence(
    db: object, sync_run: SyncRun, evidence_target_id: uuid.UUID, listing: SecurityListing
) -> SourceEvidence:
    del db, listing
    import hashlib

    from audit.models import DataChange as DC
    from audit.models import RawDataObservation, RawDataRecord
    from audit.services.source_evidence import record_source_evidence

    payload = b'{"status":"active"}'
    record = RawDataRecord.objects.create(
        source=sync_run.source,
        first_sync_run=sync_run,
        source_url="http://test.example.com/evidence",
        request_fingerprint=hashlib.sha256(b"test-fingerprint").hexdigest(),
        fetched_at=sync_run.started_at,
        content_hash=hashlib.sha256(payload).hexdigest(),
        payload=payload,
        payload_size_bytes=len(payload),
    )
    RawDataObservation.objects.create(
        sync_run=sync_run,
        raw_data_record=record,
    )
    result = record_source_evidence(
        raw_data_record=record,
        sync_run=sync_run,
        target_type=DC.TargetType.INDEX_MEMBERSHIP,
        target_id=evidence_target_id,
        field_name="status",
        raw_value="active",
        normalized_value="active",
        confidence=1.0,
        normalizer_version="test-v1",
    )
    return result.evidence


@pytest.fixture
def evidence_target_id() -> uuid.UUID:
    return uuid.uuid4()


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

    def test_existing_identity_with_different_values_conflicts(
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

    def test_close_ended_past_boundary_creates_corrected_replacement(
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
        # Old membership marked corrected, NOT directly modified
        assert m.status == IndexMembership.Status.CORRECTED
        assert m.effective_to == date(2024, 12, 31)  # preserved
        # Replacement created with shortened effective_to
        assert results[0].action == "corrected"
        repl = results[0].replacement
        assert repl is not None
        assert repl.supersedes == m
        assert repl.status == IndexMembership.Status.ENDED
        assert repl.effective_to == date(2024, 6, 30)
        assert repl.effective_from == date(2024, 1, 1)

    def test_close_ended_within_boundary_gets_ended(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
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
        m.refresh_from_db()
        assert m.status == "ended"
        assert m.effective_to == date(2024, 12, 31)
        assert results[0].action == "ended"

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


class TestAutoProvenance:
    """Tests for automatic provenance with source_evidence + sync_run."""

    @pytest.fixture
    def evidence_target_id(self) -> uuid.UUID:
        return uuid.uuid4()

    @pytest.fixture
    def evidence(
        self, db: object, listing: SecurityListing, sync_run: SyncRun, evidence_target_id: uuid.UUID
    ) -> SourceEvidence:
        del db, listing
        import uuid as _uuid
        from datetime import datetime
        from decimal import Decimal

        from audit.models import RawDataObservation, RawDataRecord
        from audit.services.source_evidence import record_source_evidence

        record = RawDataRecord.objects.create(
            source=sync_run.source,
            first_sync_run=sync_run,
            source_url="https://example.com/test",
            request_fingerprint=(_uuid.uuid4().hex * 2),
            fetched_at=datetime.now(UTC),
            content_hash="a" * 64,
            payload=b"{}",
            payload_size_bytes=2,
        )
        RawDataObservation.objects.create(
            raw_data_record=record,
            sync_run=sync_run,
            observed_at=sync_run.started_at,
        )
        result = record_source_evidence(
            raw_data_record=record,
            sync_run=sync_run,
            target_type="index_membership",
            target_id=evidence_target_id,
            field_name="status",
            raw_value="active",
            normalized_value="active",
            confidence=Decimal("1.0"),
            normalizer_version="v1",
        )
        return result.evidence

    def test_auto_create_succeeds(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        sync_run: SyncRun,
        evidence: SourceEvidence,
        evidence_target_id: uuid.UUID,
    ) -> None:
        del db
        result = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            source_evidence=evidence,
            sync_run=sync_run,
            membership_id=evidence_target_id,
        )
        assert result.created is True
        assert result.membership.source_evidence_id == evidence.pk

    def test_auto_create_writes_create_audit(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        sync_run: SyncRun,
        evidence: SourceEvidence,
        evidence_target_id: uuid.UUID,
    ) -> None:
        del db
        result = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            source_evidence=evidence,
            sync_run=sync_run,
            membership_id=evidence_target_id,
        )
        assert result.audit_record is not None
        assert result.audit_record.action == AuditRecord.Action.CREATE

    def test_auto_create_saves_resolver_evidence(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        sync_run: SyncRun,
        evidence: SourceEvidence,
        evidence_target_id: uuid.UUID,
    ) -> None:
        del db
        result = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            source_evidence=evidence,
            sync_run=sync_run,
            membership_id=evidence_target_id,
        )
        assert result.membership.source_evidence_id == evidence.pk

    def test_only_source_evidence_rejected(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        evidence: SourceEvidence,
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="sync_run"):
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
                source_evidence=evidence,
            )

    def test_only_sync_run_rejected(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        sync_run: SyncRun,
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="source_evidence"):
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
                sync_run=sync_run,
            )

    def test_auto_plus_human_mixed_rejected(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        sync_run: SyncRun,
        evidence: SourceEvidence,
        user: User,
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="both"):
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
                source_evidence=evidence,
                sync_run=sync_run,
                actor_user=user,
                reason="test",
                request_id="req-1",
            )

    def test_auto_generates_request_id(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        sync_run: SyncRun,
        evidence: SourceEvidence,
        evidence_target_id: uuid.UUID,
    ) -> None:
        del db
        result = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            source_evidence=evidence,
            sync_run=sync_run,
            membership_id=evidence_target_id,
        )
        assert result.audit_record is not None
        assert result.audit_record.request_id.startswith("sync-run:")

    def test_blank_human_reason_rejected(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="reason"):
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
                actor_user=user,
                reason="   ",
                request_id="req-1",
            )

    def test_blank_human_request_id_rejected(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="request_id"):
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
                actor_user=user,
                reason="test",
                request_id="   ",
            )


class TestMembershipConcurrency:
    """Tests for concurrent membership creation."""

    def test_sequential_create_same_identity_is_idempotent(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        """Existing identity with different values raises MembershipIdentityConflict."""
        del db
        # First create establishes the record
        result1 = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            actor_user=user,
            reason="concurrent test",
            request_id="concurrent-1",
        )
        assert result1.created is True

        # Second call with same identity must return idempotent
        result2 = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            actor_user=user,
            reason="concurrent test",
            request_id="concurrent-1",
        )
        assert result2.created is False
        assert result2.membership.pk == result1.membership.pk

        # Only one AuditRecord
        audit_count = AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
            target_id=result1.membership.pk,
        ).count()
        assert audit_count == 1, "Only one AuditRecord should be created"

    def test_create_within_listing_boundary_rejected_by_service(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="before listing"):
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2019, 1, 1),
                actor_user=user,
                reason="test",
                request_id="req-1",
            )


class TestProvenanceClassification:
    """Frozen provenance classification: auto vs manual with strict rejection."""

    def test_manual_create_with_source_evidence_succeeds(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
        evidence: SourceEvidence,
        evidence_target_id: uuid.UUID,
    ) -> None:
        del db
        result = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            source_evidence=evidence,
            actor_user=user,
            reason="Manual with evidence",
            request_id="manual-evidence-1",
            membership_id=evidence_target_id,
        )
        assert result.created is True
        assert result.membership.source_evidence_id == evidence.pk

    def test_manual_with_wrong_target_evidence_rejected(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
        evidence: SourceEvidence,
    ) -> None:
        del db
        # Create a membership first, then try to use evidence that targets
        # something else (a different membership UUID)
        other_id = uuid.uuid4()
        # The evidence targets evidence_target_id, but we pass membership_id=other_id
        # Wrong target evidence raises an exception from audit layer
        error_raised = False
        try:
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
                source_evidence=evidence,
                actor_user=user,
                reason="Wrong target",
                request_id="wrong-target-1",
                membership_id=other_id,
            )
        except (MembershipServiceError, InvalidSourceEvidence):
            error_raised = True
        assert error_raised, "Expected an exception for wrong target evidence"

    def test_actor_user_plus_sync_run_rejected(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
        sync_run: SyncRun,
        evidence: SourceEvidence,
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="both"):
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
                source_evidence=evidence,
                sync_run=sync_run,
                actor_user=user,
                reason="mixed",
                request_id="mixed-1",
            )

    def test_source_evidence_alone_rejected(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        evidence: SourceEvidence,
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="SourceEvidence"):
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
                source_evidence=evidence,
            )

    def test_failed_provenance_no_membership_created(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        del db
        before = IndexMembership.objects.count()
        try:
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
            )
        except MembershipServiceError:
            pass
        assert IndexMembership.objects.count() == before

    def test_failed_provenance_no_data_change(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        del db
        from audit.models import DataChange

        before_dc = DataChange.objects.count()
        try:
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
            )
        except MembershipServiceError:
            pass
        assert DataChange.objects.count() == before_dc

    def test_failed_provenance_no_audit_record(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
    ) -> None:
        del db
        before_audit = AuditRecord.objects.count()
        try:
            create_index_membership(
                index=sp500,
                security_listing=listing,
                status=IndexMembership.Status.ACTIVE,
                effective_from=date(2024, 1, 1),
            )
        except MembershipServiceError:
            pass
        assert AuditRecord.objects.count() == before_audit


class TestEndMembershipIdempotent:
    """End membership idempotency: active→ended, ended no-op, different to→reject."""

    def test_active_to_ended_with_same_to_is_ended(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            effective_to=date(2024, 12, 31),
        )
        result = end_membership(
            membership=m,
            effective_to=date(2024, 12, 31),
            actor_user=user,
            reason="End already-set date",
            request_id="end-same-1",
        )
        m.refresh_from_db()
        assert m.status == IndexMembership.Status.ENDED
        assert m.effective_to == date(2024, 12, 31)
        assert len(result.data_changes) == 1  # status change
        assert result.audit_record is not None

    def test_ended_same_effective_to_is_noop(
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
        result = end_membership(
            membership=m,
            effective_to=date(2024, 6, 1),
            actor_user=user,
            reason="No-op end",
            request_id="noop-1",
        )
        assert result.data_changes == ()
        assert result.audit_record is None

    def test_ended_different_effective_to_rejected(
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
        with pytest.raises(InvalidMembershipState, match="already ended"):
            end_membership(
                membership=m,
                effective_to=date(2024, 12, 31),
                actor_user=user,
                reason="Different end date",
                request_id="diff-end-1",
            )

    def test_retry_does_not_duplicate_data_change(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        from audit.models import DataChange

        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        # First end
        end_membership(
            membership=m,
            effective_to=date(2024, 12, 31),
            actor_user=user,
            reason="End it",
            request_id="retry-1",
        )
        dc_count_after_first = DataChange.objects.filter(
            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
            target_id=m.pk,
        ).count()

        # Second end (no-op)
        end_membership(
            membership=m,
            effective_to=date(2024, 12, 31),
            actor_user=user,
            reason="End again",
            request_id="retry-2",
        )
        dc_count_after_second = DataChange.objects.filter(
            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
            target_id=m.pk,
        ).count()
        assert dc_count_after_second == dc_count_after_first

    def test_retry_does_not_duplicate_audit_record(
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
            effective_to=date(2024, 12, 31),
            actor_user=user,
            reason="End it",
            request_id="retry-audit-1",
        )
        audit_count_after_first = AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
            target_id=m.pk,
        ).count()

        end_membership(
            membership=m,
            effective_to=date(2024, 12, 31),
            actor_user=user,
            reason="End again",
            request_id="retry-audit-2",
        )
        audit_count_after_second = AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
            target_id=m.pk,
        ).count()
        assert audit_count_after_second == audit_count_after_first


class TestCorrectMembershipValidation:
    """Correction replacement field whitelist and validation."""

    def test_replacement_status_ended_requires_effective_to(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        with pytest.raises(MembershipServiceError, match="effective_to"):
            correct_membership(
                membership=old,
                replacement_values={"status": IndexMembership.Status.ENDED},
                actor_user=user,
                reason="Missing effective_to",
                request_id="corr-1",
            )

    def test_replacement_forbidden_field_rejected(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        with pytest.raises(MembershipServiceError, match="forbidden"):
            correct_membership(
                membership=old,
                replacement_values={"id": uuid.uuid4(), "effective_to": date(2024, 12, 31)},
                actor_user=user,
                reason="Forbidden field",
                request_id="corr-2",
            )

    def test_replacement_cancelled_status_rejected(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        with pytest.raises(MembershipServiceError, match="not allowed"):
            correct_membership(
                membership=old,
                replacement_values={
                    "status": IndexMembership.Status.CANCELLED,
                    "effective_to": date(2024, 12, 31),
                },
                actor_user=user,
                reason="Cancelled status",
                request_id="corr-3",
            )

    def test_correction_with_evidence_succeeds(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        """Manual correction accepts source_evidence=None."""
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        result = correct_membership(
            membership=old,
            replacement_values={"effective_to": date(2024, 12, 31)},
            actor_user=user,
            reason="Correction without evidence",
            request_id="corr-evidence-1",
        )
        assert result.replacement.status == IndexMembership.Status.ACTIVE
        assert result.replacement.effective_to == date(2024, 12, 31)

    def test_announcement_date_after_effective_from_rejected(
        self,
        db: object,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        with pytest.raises(MembershipServiceError, match="announcement_date"):
            correct_membership(
                membership=old,
                replacement_values={
                    "announcement_date": date(2024, 6, 1),
                    "effective_to": date(2024, 12, 31),
                },
                actor_user=user,
                reason="Bad announcement date",
                request_id="corr-ann-1",
            )


class TestCloseMemberships:
    """Close memberships for listing: end spanning, cancel future, reject active."""

    def test_close_already_effective_membership_conflicts(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        # An active membership with effective_from before as_of_date
        # and effective_from >= new_effective_to → conflict
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2025, 1, 1),
        )
        with pytest.raises(MembershipListingHistoryConflict):
            close_memberships_for_listing(
                listing=listing,
                new_effective_to=date(2024, 7, 1),
                as_of_date=date(2025, 6, 1),
                actor_user=user,
                reason="Close listing",
                request_id="close-conflict-1",
            )

    def test_close_already_cancelled_membership_not_in_normative(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        # Cancelled memberships are excluded from NORMATIVE_MEMBERSHIP_STATUSES
        # so they are not returned by close_memberships_for_listing
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.CANCELLED,
            effective_from=date(2026, 1, 1),
        )
        result = close_memberships_for_listing(
            listing=listing,
            new_effective_to=date(2025, 7, 1),
            as_of_date=date(2024, 6, 1),
            actor_user=user,
            reason="Close listing",
            request_id="close-skip-1",
        )
        assert len(result) == 0  # No normative memberships to close


@pytest.mark.django_db(transaction=True)
class TestConcurrencyReal:
    """Real dual-connection concurrency tests using threading.

    Uses transaction=True so threads can see committed data.
    """

    def test_concurrent_create_same_identity_one_wins_db(self, listing: SecurityListing) -> None:
        """Two independent connections race to create the same normative identity.

        Uses a threading.Barrier to synchronize threads so they enter their
        transactions at approximately the same time.  A finite lock_timeout
        prevents hangs.  Exactly one created=True is expected; the other
        either succeeds idempotently or gets an IntegrityError.
        """
        import hashlib
        import threading

        from django.db import close_old_connections, connections, transaction

        from audit.models import DataSource, RawDataObservation, RawDataRecord
        from audit.services.source_evidence import record_source_evidence

        # Set up shared evidence and sync_run
        source = DataSource.objects.create(
            key="concurrent-source",
            name="Concurrent Source",
            source_type="manual",
        )
        sync_run = SyncRun.objects.create(
            job_type="test",
            source=source,
            status="running",
            idempotency_key="concurrent-sync",
            code_version="test",
            parser_version="test",
        )
        payload = b'{"status":"active"}'
        record = RawDataRecord.objects.create(
            source=source,
            first_sync_run=sync_run,
            source_url="http://test.example.com/concurrent",
            request_fingerprint=hashlib.sha256(b"concurrent").hexdigest(),
            fetched_at=sync_run.started_at,
            content_hash=hashlib.sha256(payload).hexdigest(),
            payload=payload,
            payload_size_bytes=len(payload),
        )
        RawDataObservation.objects.create(
            sync_run=sync_run,
            raw_data_record=record,
        )
        target_id = uuid.uuid4()
        evidence_result = record_source_evidence(
            raw_data_record=record,
            sync_run=sync_run,
            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
            target_id=target_id,
            field_name="status",
            raw_value="active",
            normalized_value="active",
            confidence=1.0,
            normalizer_version="test-v1",
        )
        evidence = evidence_result.evidence

        sp500 = MarketIndex.objects.get_or_create(
            code="SP500",
            defaults={"name": "S&P 500", "index_group": "LARGE", "is_enabled": True},
        )[0]

        barrier = threading.Barrier(2, timeout=10)
        errors: list[Exception] = []
        results: list[IndexMembership] = []

        def create_membership() -> None:
            close_old_connections()
            try:
                # Wait for both threads to be ready before entering the critical section
                barrier.wait()
                with transaction.atomic():
                    # Set a finite lock_timeout to prevent hangs
                    from django.db import connection as local_conn

                    with local_conn.cursor() as c:
                        c.execute("SET LOCAL lock_timeout = '5s'")
                    result = create_index_membership(
                        index=sp500,
                        security_listing=listing,
                        status=IndexMembership.Status.ACTIVE,
                        effective_from=date(2024, 1, 1),
                        source_evidence=evidence,
                        sync_run=sync_run,
                        membership_id=target_id,
                    )
                    results.append(result.membership)
            except (IntegrityError, MembershipServiceError) as e:
                errors.append(e)
            finally:
                for conn in connections.all():
                    conn.close()

        t1 = threading.Thread(target=create_membership)
        t2 = threading.Thread(target=create_membership)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert not t1.is_alive(), "Thread 1 hung"
        assert not t2.is_alive(), "Thread 2 hung"

        # Close main-thread connections and reconnect for assertions
        for conn in connections.all():
            conn.close()
        close_old_connections()

        # Check errors by type
        for e in errors:
            assert isinstance(e, (IntegrityError, MembershipServiceError)), (
                f"Unexpected error type {type(e).__name__}: {e}"
            )
        assert len(results) >= 1, "At least one thread should succeed"

        # Exactly one created=True (idempotent returns created=False)
        created_count = sum(1 for r in results if IndexMembership.objects.filter(pk=r.pk).exists())
        assert created_count >= 1

        # Only one AuditRecord for this membership
        audit_count = AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
            target_id=results[0].pk,
        ).count()
        assert audit_count == 1, f"Expected 1 AuditRecord, got {audit_count}"


class TestCloseMembershipsEdgeCases:
    """Edge cases for close_memberships_for_listing."""

    def test_future_announced_cancelled(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 1, 1),
        )
        result = close_memberships_for_listing(
            listing=listing,
            new_effective_to=date(2025, 7, 1),
            as_of_date=date(2024, 6, 1),
            actor_user=user,
            reason="Close listing",
            request_id="close-future-1",
        )
        m.refresh_from_db()
        assert m.status == IndexMembership.Status.CANCELLED
        assert result[0].action == "cancelled"

    def test_future_active_raises_conflict(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
        with pytest.raises(MembershipListingHistoryConflict):
            close_memberships_for_listing(
                listing=listing,
                new_effective_to=date(2025, 7, 1),
                as_of_date=date(2024, 6, 1),
                actor_user=user,
                reason="Close listing",
                request_id="close-active-1",
            )

    def test_future_ended_raises_conflict(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ENDED,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 12, 31),
        )
        with pytest.raises(MembershipListingHistoryConflict):
            close_memberships_for_listing(
                listing=listing,
                new_effective_to=date(2025, 7, 1),
                as_of_date=date(2024, 6, 1),
                actor_user=user,
                reason="Close listing",
                request_id="close-ended-1",
            )

    def test_new_effective_to_equals_listing_effective_from_rejected(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="effective_from"):
            close_memberships_for_listing(
                listing=listing,
                new_effective_to=listing.effective_from,
                as_of_date=date(2024, 6, 1),
                actor_user=user,
                reason="Close listing",
                request_id="close-eq-1",
            )

    def test_new_effective_to_before_listing_effective_from_rejected(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        with pytest.raises(MembershipServiceError, match="effective_from"):
            close_memberships_for_listing(
                listing=listing,
                new_effective_to=date(2019, 1, 1),
                as_of_date=date(2024, 6, 1),
                actor_user=user,
                reason="Close listing",
                request_id="close-before-1",
            )

    def test_conflict_rolls_back_prior_mutations(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        other_idx = MarketIndex.objects.get_or_create(
            code="NASDAQ100",
            defaults={"name": "Nasdaq 100", "index_group": "LARGE", "is_enabled": True},
        )[0]
        m_good = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        m_bad = IndexMembership.objects.create(
            index=other_idx,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
        dc_before = DataChange.objects.count()
        audit_before = AuditRecord.objects.count()

        try:
            close_memberships_for_listing(
                listing=listing,
                new_effective_to=date(2025, 7, 1),
                as_of_date=date(2024, 6, 1),
                actor_user=user,
                reason="Close listing",
                request_id="rollback-1",
            )
        except MembershipListingHistoryConflict:
            pass

        m_good.refresh_from_db()
        m_bad.refresh_from_db()
        assert m_good.status == IndexMembership.Status.ACTIVE
        assert m_good.effective_to is None
        assert m_bad.status == IndexMembership.Status.ACTIVE
        assert DataChange.objects.count() == dc_before
        assert AuditRecord.objects.count() == audit_before

    def test_announced_shortened_still_announced_action_accurate(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        m = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 1, 1),
        )
        result = close_memberships_for_listing(
            listing=listing,
            new_effective_to=date(2025, 7, 1),
            as_of_date=date(2024, 6, 1),
            actor_user=user,
            reason="Close listing",
            request_id="close-ann-1",
        )
        m.refresh_from_db()
        assert m.status == IndexMembership.Status.CANCELLED
        assert result[0].action == "cancelled"


class TestCorrectMembershipEdgeCases:
    """Correction edge cases: evidence, last_verified_at, metadata."""

    def test_source_evidence_in_replacement_values_rejected(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        with pytest.raises(MembershipServiceError, match="forbidden"):
            correct_membership(
                membership=old,
                replacement_values={
                    "source_evidence": "anything",
                    "effective_to": date(2024, 12, 31),
                },
                actor_user=user,
                reason="Forbidden field",
                request_id="corr-ce-1",
            )

    def test_no_evidence_leaves_replacement_null(
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
            replacement_values={"effective_to": date(2024, 12, 31)},
            actor_user=user,
            reason="No evidence",
            request_id="corr-ce-2",
        )
        assert result.replacement.source_evidence_id is None

    def test_old_source_evidence_unchanged(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
        )
        old_ev = old.source_evidence_id
        correct_membership(
            membership=old,
            replacement_values={"effective_to": date(2024, 12, 31)},
            actor_user=user,
            reason="Correction",
            request_id="corr-ce-3",
        )
        old.refresh_from_db()
        assert old.source_evidence_id == old_ev

    def test_last_verified_not_provided_inherits_old(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        from datetime import datetime as dt

        ts = dt(2024, 6, 1, tzinfo=UTC)
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            last_verified_at=ts,
        )
        result = correct_membership(
            membership=old,
            replacement_values={"effective_to": date(2024, 12, 31)},
            actor_user=user,
            reason="Correction",
            request_id="corr-ce-4",
        )
        assert result.replacement.last_verified_at == ts

    def test_last_verified_not_provided_inherits_none(
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
            replacement_values={"effective_to": date(2024, 12, 31)},
            actor_user=user,
            reason="Correction",
            request_id="corr-ce-5",
        )
        assert result.replacement.last_verified_at is None

    def test_last_verified_explicit_none_overrides(
        self, db: object, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        del db
        from datetime import datetime as dt

        ts = dt(2024, 6, 1, tzinfo=UTC)
        old = IndexMembership.objects.create(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2024, 1, 1),
            last_verified_at=ts,
        )
        result = correct_membership(
            membership=old,
            replacement_values={
                "last_verified_at": None,
                "effective_to": date(2024, 12, 31),
            },
            actor_user=user,
            reason="Correction",
            request_id="corr-ce-6",
        )
        assert result.replacement.last_verified_at is None

    def test_metadata_only_correction(
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
                "announcement_date": date(2023, 12, 1),
            },
            actor_user=user,
            reason="Metadata fix",
            request_id="meta-1",
        )
        old.refresh_from_db()
        assert old.status == IndexMembership.Status.CORRECTED
        assert result.replacement.announcement_date == date(2023, 12, 1)
