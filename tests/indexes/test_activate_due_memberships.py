from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from django.utils import timezone

from audit.models import AuditRecord, DataChange, SyncRun
from audit.services import (
    AuditRecordWriteResult,
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    record_user_action,
)
from indexes.models import IndexMembership, MarketIndex
from indexes.services import (
    MembershipActivationResult,
    MembershipServiceError,
    activate_due_memberships,
    cancel_membership,
    correct_membership,
    create_index_membership,
    end_membership,
)

if TYPE_CHECKING:
    from accounts.models import User
    from audit.models import SourceEvidence
    from companies.models import Company, SecurityListing


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
    from companies.models import Company as C

    return C.objects.create(
        legal_name="Activate Test Corp",
        display_name="Activate Test Corp",
        cik="0000000002",
    )


@pytest.fixture
def listing(db: object, company: Company) -> SecurityListing:
    del db
    from companies.models import SecurityListing as SL

    return SL.objects.create(
        company=company,
        ticker="ATEST",
        exchange="NYSE",
        effective_from=date(2020, 1, 1),
    )


@pytest.fixture
def user(db: object) -> User:
    del db
    from accounts.models import User as U

    return U.objects.create_user(email="activate-test@example.test", password="test")


# ---- helpers ----------------------------------------------------------

_REQ_ID = "activate-due-test-req"


def _make_sync_run(*, status: str = SyncRun.Status.RUNNING) -> SyncRun:
    from audit.models import DataSource

    source = DataSource.objects.create(
        key=f"sync-source-{uuid.uuid4().hex[:8]}",
        name="Sync Source",
        source_type="index",
    )
    timestamp_fields = {}
    if status == SyncRun.Status.SKIPPED:
        # There is no public skipped transition service; create a valid terminal test run.
        timestamp = timezone.now()
        timestamp_fields = {
            "started_at": timestamp,
            "heartbeat_at": timestamp,
            "finished_at": timestamp,
        }
    elif status != SyncRun.Status.RUNNING:
        raise ValueError(f"Unsupported direct SyncRun status {status!r}.")

    return SyncRun.objects.create(
        job_type="activate-test",
        source=source,
        status=status,
        idempotency_key=f"activate-sync-{uuid.uuid4().hex}",
        code_version="test",
        parser_version="test",
        **timestamp_fields,
    )


def _make_terminal_sync_run(status: str) -> SyncRun:
    if status == SyncRun.Status.SKIPPED:
        return _make_sync_run(status=status)

    sync_run = _make_sync_run()
    if status == SyncRun.Status.SUCCEEDED:
        return mark_sync_run_succeeded(sync_run.pk)
    if status == SyncRun.Status.PARTIAL:
        return mark_sync_run_partial(sync_run.pk, error_summary="partial fixture run")
    if status == SyncRun.Status.FAILED:
        return mark_sync_run_failed(sync_run.pk, error_summary="failed fixture run")
    raise ValueError(f"Unsupported terminal SyncRun status {status!r}.")


def _make_evidence(
    *,
    target_id: uuid.UUID,
    sync_run: SyncRun,
) -> SourceEvidence:
    import hashlib

    from audit.models import DataChange as DC
    from audit.models import RawDataObservation, RawDataRecord
    from audit.services.source_evidence import record_source_evidence

    payload = b'{"status":"announced"}'
    record = RawDataRecord.objects.create(
        source=sync_run.source,
        first_sync_run=sync_run,
        source_url=f"http://evidence.test/{uuid.uuid4().hex}",
        request_fingerprint=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
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
        target_id=target_id,
        field_name="status",
        raw_value="announced",
        normalized_value="announced",
        confidence=1.0,
        normalizer_version="test-v1",
    )
    return result.evidence


def _setup_announced_with_evidence(
    *,
    index: MarketIndex,
    listing: SecurityListing,
    effective_from: date,
    user: User,
    sync_run: SyncRun,
) -> IndexMembership:
    """Create via manual path, then attach evidence from *sync_run*.

    This simulates what a sync command would produce: membership created
    with evidence belonging to that sync run.
    """
    mid = uuid.uuid4()
    m = create_index_membership(
        index=index,
        security_listing=listing,
        status="announced",
        effective_from=effective_from,
        actor_user=user,
        reason="setup with evidence",
        request_id=_REQ_ID,
        membership_id=mid,
    ).membership
    evidence = _make_evidence(target_id=m.pk, sync_run=sync_run)
    IndexMembership.objects.filter(pk=m.pk).update(source_evidence=evidence)
    m.refresh_from_db()
    return m


def _activate_manual(
    *,
    as_of_date: date | None = None,
    actor_user: User | None = None,
    reason: str = "Date-based activation",
    request_id: str = _REQ_ID,
) -> MembershipActivationResult:
    if actor_user is None:
        pytest.fail("Manual provenance requires an actor_user.")
    return activate_due_memberships(
        as_of_date=as_of_date,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
    )


# ---- Tests ------------------------------------------------------------


class TestActivateDueMemberships:
    def test_activates_past_effective_manual(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 6, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        assert m.status == "announced"

        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert result.activated_count == 1
        m.refresh_from_db()
        assert m.status == "active"

    def test_activates_on_effective_date_manual(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        effective_date = date(2026, 7, 14)
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=effective_date,
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        result = _activate_manual(as_of_date=effective_date, actor_user=user)
        assert result.activated_count == 1
        m.refresh_from_db()
        assert m.status == "active"

    def test_skips_future(self, sp500: MarketIndex, listing: SecurityListing, user: User) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2099, 12, 31),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert result.activated_count == 0
        m.refresh_from_db()
        assert m.status == "announced"

    def test_skips_active(self, sp500: MarketIndex, listing: SecurityListing, user: User) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="active",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert result.activated_count == 0
        m.refresh_from_db()
        assert m.status == "active"

    def test_skips_ended(self, sp500: MarketIndex, listing: SecurityListing, user: User) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        end_membership(
            membership=m,
            effective_to=date(2026, 6, 30),
            actor_user=user,
            reason="ended early",
            request_id=_REQ_ID,
        )
        m.refresh_from_db()
        assert m.status == "ended"
        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert result.activated_count == 0

    def test_skips_cancelled(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2099, 12, 31),
            announcement_date=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        cancel_membership(
            membership=m,
            as_of_date=date(2026, 7, 1),
            actor_user=user,
            reason="cancelled",
            request_id=_REQ_ID,
        )
        m.refresh_from_db()
        assert m.status == "cancelled"
        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert result.activated_count == 0

    def test_skips_corrected(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        corr_result = correct_membership(
            membership=m,
            replacement_values={"status": "active"},
            actor_user=user,
            reason="correct to active",
            request_id=_REQ_ID,
        )
        corr_result.old_membership.refresh_from_db()
        assert corr_result.old_membership.status == "corrected"
        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert result.activated_count == 0


class TestActivateDefaultDate:
    def test_uses_timezone_localdate(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        fake_today = date(2099, 1, 1)
        with mock.patch("django.utils.timezone.localdate", return_value=fake_today):
            result = activate_due_memberships(
                actor_user=user,
                reason="activate by default date",
                request_id=_REQ_ID,
            )
        assert result.activated_count == 1
        m.refresh_from_db()
        assert m.status == "active"

    def test_explicit_as_of_date_overrides_default(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2099, 12, 31),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        )
        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert result.activated_count == 0


class TestActivateAudit:
    def test_data_change_manual(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert result.activated_count == 1
        assert len(result.data_changes) == 1
        dc = result.data_changes[0]
        assert dc.created is True
        c = dc.change
        assert c is not None
        assert c.target_type == DataChange.TargetType.INDEX_MEMBERSHIP
        assert c.target_id == m.pk
        assert c.field_name == "status"
        assert c.old_value == "announced"
        assert c.new_value == "active"
        assert c.source_evidence_id is None
        assert c.actor_user_id == user.pk

    def test_audit_record_manual(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert len(result.audit_records) == 1
        ar = result.audit_records[0]
        assert ar is not None
        assert ar.action == AuditRecord.Action.UPDATE
        assert ar.target_type == AuditRecord.TargetType.INDEX_MEMBERSHIP
        assert ar.target_id == m.pk
        assert ar.actor_user_id == user.pk
        assert ar.sync_run_id is None

    def test_source_evidence_unchanged(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        original_se_id = m.source_evidence_id
        _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        m.refresh_from_db()
        assert m.source_evidence_id == original_se_id

    def test_last_verified_at_unchanged(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        assert m.last_verified_at is None
        _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        m.refresh_from_db()
        assert m.last_verified_at is None


class TestActivateAutoAcrossSyncRuns:
    """Activation with evidence from one SyncRun, activation from another."""

    def test_evidence_from_sync_a_activated_by_sync_b(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        sync_a = _make_sync_run()  # e.g. creation sync
        m = _setup_announced_with_evidence(
            index=sp500,
            listing=listing,
            effective_from=date(2026, 1, 1),
            user=user,
            sync_run=sync_a,
        )
        original_se_id = m.source_evidence_id
        assert original_se_id is not None

        sync_b = _make_sync_run()  # e.g. activation sync
        result = activate_due_memberships(
            as_of_date=date(2026, 7, 1),
            sync_run=sync_b,
        )
        assert result.activated_count == 1
        m.refresh_from_db()
        assert m.status == "active"
        # Evidence unchanged — still from sync_a
        assert m.source_evidence_id == original_se_id
        # DataChange attributed to sync_b, no source_evidence
        dc = result.data_changes[0].change
        assert dc is not None
        assert dc.sync_run_id == sync_b.pk
        assert dc.source_evidence_id is None
        assert dc.actor_user_id is None
        # AuditRecord attributed to sync_b
        ar = result.audit_records[0]
        assert ar is not None
        assert ar.sync_run_id == sync_b.pk
        assert ar.actor_user_id is None

    def test_multiple_evidences_from_different_syncs(
        self,
        sp500: MarketIndex,
        nasdaq100: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        sync_a = _make_sync_run()
        sync_c = _make_sync_run()
        m1 = _setup_announced_with_evidence(
            index=sp500,
            listing=listing,
            effective_from=date(2026, 1, 1),
            user=user,
            sync_run=sync_a,
        )
        m2 = _setup_announced_with_evidence(
            index=nasdaq100,
            listing=listing,
            effective_from=date(2026, 1, 1),
            user=user,
            sync_run=sync_c,
        )
        assert m1.source_evidence_id != m2.source_evidence_id

        sync_b = _make_sync_run()
        result = activate_due_memberships(
            as_of_date=date(2026, 7, 1),
            sync_run=sync_b,
        )
        # Both activated — no silent skip
        assert result.activated_count == 2
        m1.refresh_from_db()
        m2.refresh_from_db()
        assert m1.status == "active"
        assert m2.status == "active"
        # Both DataChanges attributed to sync_b
        for dc_result in result.data_changes:
            c = dc_result.change
            assert c is not None
            assert c.sync_run_id == sync_b.pk
            assert c.source_evidence_id is None

    def test_no_evidence_still_activated_by_auto(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        """Membership created manually (no evidence) CAN be auto-activated."""
        m = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="manual setup",
            request_id=_REQ_ID,
        ).membership
        assert m.source_evidence_id is None

        sync_run = _make_sync_run()
        result = activate_due_memberships(
            as_of_date=date(2026, 7, 1),
            sync_run=sync_run,
        )
        assert result.activated_count == 1
        m.refresh_from_db()
        assert m.status == "active"
        dc = result.data_changes[0].change
        assert dc is not None
        assert dc.sync_run_id == sync_run.pk
        assert dc.source_evidence_id is None


class TestActivateAutoSyncRunValidation:
    @pytest.mark.parametrize(
        "terminal_status",
        [
            SyncRun.Status.SUCCEEDED,
            SyncRun.Status.PARTIAL,
            SyncRun.Status.FAILED,
            SyncRun.Status.SKIPPED,
        ],
    )
    def test_rejects_terminal_sync_run(
        self,
        terminal_status: str,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        membership = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        terminal_run = _make_terminal_sync_run(terminal_status)
        data_change_count = DataChange.objects.filter(
            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
            target_id=membership.pk,
        ).count()
        audit_record_count = AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
            target_id=membership.pk,
        ).count()

        with pytest.raises(
            MembershipServiceError,
            match="only running runs may activate memberships",
        ):
            activate_due_memberships(
                as_of_date=date(2026, 7, 1),
                sync_run=terminal_run,
            )

        membership.refresh_from_db()
        assert membership.status == IndexMembership.Status.ANNOUNCED
        assert (
            DataChange.objects.filter(
                target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
                target_id=membership.pk,
            ).count()
            == data_change_count
        )
        assert (
            AuditRecord.objects.filter(
                target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
                target_id=membership.pk,
            ).count()
            == audit_record_count
        )

    def test_rejects_stale_running_sync_run_object(
        self,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        membership = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        stale_sync_run = _make_sync_run()
        current_sync_run = mark_sync_run_succeeded(stale_sync_run.pk)
        assert stale_sync_run.status == SyncRun.Status.RUNNING
        assert current_sync_run.status == SyncRun.Status.SUCCEEDED

        with pytest.raises(
            MembershipServiceError,
            match="only running runs may activate memberships",
        ):
            activate_due_memberships(
                as_of_date=date(2026, 7, 1),
                sync_run=stale_sync_run,
            )

        membership.refresh_from_db()
        assert membership.status == IndexMembership.Status.ANNOUNCED

    def test_rejects_sync_run_deleted_after_loading(
        self,
        sp500: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        membership = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        ).membership
        deleted_sync_run = _make_sync_run()
        deleted_sync_run_id = deleted_sync_run.pk
        SyncRun.objects.filter(pk=deleted_sync_run_id).delete()

        with pytest.raises(
            MembershipServiceError,
            match=rf"SyncRun {deleted_sync_run_id} does not exist",
        ):
            activate_due_memberships(
                as_of_date=date(2026, 7, 1),
                sync_run=deleted_sync_run,
            )

        membership.refresh_from_db()
        assert membership.status == IndexMembership.Status.ANNOUNCED


class TestActivateBatch:
    def test_activates_multiple(
        self,
        sp500: MarketIndex,
        nasdaq100: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        m1 = create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 6, 1),
            actor_user=user,
            reason="setup 1",
            request_id=f"{_REQ_ID}-1",
        ).membership
        m2 = create_index_membership(
            index=nasdaq100,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup 2",
            request_id=f"{_REQ_ID}-2",
        ).membership
        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert result.activated_count == 2
        m1.refresh_from_db()
        m2.refresh_from_db()
        assert m1.status == "active"
        assert m2.status == "active"


class TestActivateIdempotency:
    def test_manual_second_run_noop(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        )
        r1 = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert r1.activated_count == 1
        dc_count = DataChange.objects.count()
        ar_count = AuditRecord.objects.count()
        r2 = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert r2.activated_count == 0
        assert len(r2.data_changes) == 0
        assert len(r2.audit_records) == 0
        assert DataChange.objects.count() == dc_count
        assert AuditRecord.objects.count() == ar_count

    def test_manual_skips_already_active(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        )
        _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        r2 = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert r2.activated_count == 0
        assert IndexMembership.objects.filter(status="active").count() == 1

    def test_auto_second_run_noop(
        self, sp500: MarketIndex, listing: SecurityListing, user: User
    ) -> None:
        create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup",
            request_id=_REQ_ID,
        )
        sr = _make_sync_run()
        r1 = activate_due_memberships(
            as_of_date=date(2026, 7, 1),
            sync_run=sr,
        )
        assert r1.activated_count == 1
        dc_count = DataChange.objects.count()
        ar_count = AuditRecord.objects.count()
        r2 = activate_due_memberships(
            as_of_date=date(2026, 7, 1),
            sync_run=sr,
        )
        assert r2.activated_count == 0
        assert DataChange.objects.count() == dc_count
        assert AuditRecord.objects.count() == ar_count


class TestActivateRejectsBothSources:
    def test_rejects_both_actor_and_sync(self, user: User) -> None:
        sync_run = _make_sync_run()
        with pytest.raises(
            MembershipServiceError,
            match="Cannot provide both actor_user and sync_run",
        ):
            activate_due_memberships(
                as_of_date=date(2026, 7, 1),
                sync_run=sync_run,
                actor_user=user,
                reason="test",
                request_id="test",
            )


class TestActivateTransactionIntegrity:
    def test_activates_multiple_memberships_in_one_batch(
        self,
        sp500: MarketIndex,
        nasdaq100: MarketIndex,
        listing: SecurityListing,
        user: User,
    ) -> None:
        create_index_membership(
            index=sp500,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup 1",
            request_id=f"{_REQ_ID}-1",
        )
        m2 = create_index_membership(
            index=nasdaq100,
            security_listing=listing,
            status="announced",
            effective_from=date(2026, 2, 1),
            actor_user=user,
            reason="setup 2",
            request_id=f"{_REQ_ID}-2",
        ).membership
        result = _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)
        assert result.activated_count == 2
        m2.refresh_from_db()
        assert m2.status == "active"

    def test_rolls_back_entire_batch_when_second_audit_fails(
        self,
        sp500: MarketIndex,
        nasdaq100: MarketIndex,
        listing: SecurityListing,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first_membership = create_index_membership(
            index=sp500,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 1, 1),
            actor_user=user,
            reason="setup 1",
            request_id=f"{_REQ_ID}-1",
        ).membership
        second_membership = create_index_membership(
            index=nasdaq100,
            security_listing=listing,
            status=IndexMembership.Status.ANNOUNCED,
            effective_from=date(2026, 2, 1),
            actor_user=user,
            reason="setup 2",
            request_id=f"{_REQ_ID}-2",
        ).membership
        data_change_count = DataChange.objects.count()
        audit_record_count = AuditRecord.objects.count()
        call_count = 0

        def fail_on_second(**kwargs: Any) -> AuditRecordWriteResult:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("forced second membership failure")
            return record_user_action(**kwargs)

        monkeypatch.setattr("indexes.services.record_user_action", fail_on_second)

        with pytest.raises(RuntimeError, match="forced second membership failure"):
            _activate_manual(as_of_date=date(2026, 7, 1), actor_user=user)

        first_membership.refresh_from_db()
        second_membership.refresh_from_db()
        assert first_membership.status == IndexMembership.Status.ANNOUNCED
        assert second_membership.status == IndexMembership.Status.ANNOUNCED
        assert DataChange.objects.count() == data_change_count
        assert AuditRecord.objects.count() == audit_record_count
