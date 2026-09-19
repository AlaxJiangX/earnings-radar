# mypy: ignore-errors
"""Service and PostgreSQL concurrency tests for Stage 4.1D promotion."""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

import pytest
from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

from accounts.models import User
from audit.models import (
    AuditRecord,
    DataChange,
    DataSource,
    RawDataObservation,
    RawDataRecord,
    SourceEvidence,
    SyncRun,
)
from earnings.identity import IDENTITY_RULE_VERSION, derive_earnings_identity_key
from earnings.models import (
    EarningsDateChange,
    EarningsEvent,
    EventStatus,
    FiscalCalendarType,
    IdentityStatus,
)
from earnings.services import (
    EARNINGS_CANDIDATE_PROMOTION_RULE_VERSION,
    PROMOTION_IDENTITY_FIELDS,
    EarningsPromotionCollision,
    EarningsPromotionIntegrityError,
    EarningsPromotionServiceError,
    InvalidEarningsPromotion,
    cancel_earnings_event,
    confirm_earnings_event,
    mark_earnings_released,
    promote_earnings_event,
    update_earnings_schedule,
)
from earnings.services import promotion as promotion_module
from tests.earnings.helpers import (
    make_company,
    make_event,
    make_source_evidence,
    make_sync_run,
)

_TARGET_DATE = date(2026, 3, 31)
_SCHEDULE_FIELDS = (
    "estimated_release_at",
    "estimated_release_date",
    "estimated_release_precision",
    "confirmed_release_at",
    "confirmed_release_date",
    "confirmed_release_precision",
    "earnings_release_at",
    "earnings_release_date",
    "earnings_release_precision",
    "conference_call_at",
    "conference_call_date",
    "conference_call_precision",
    "release_session",
)


def _make_candidate(
    *,
    company=None,
    period_end_date: date | None = None,
    period_type: str | None = None,
    **overrides: object,
) -> EarningsEvent:
    company = company or make_company("candidate")
    includes_q4 = overrides.pop("includes_q4", period_type == "FY")
    return EarningsEvent.objects.create(
        company=company,
        period_end_date=period_end_date,
        period_type=period_type,
        includes_q4=includes_q4,
        identity_status=IdentityStatus.CANDIDATE,
        identity_key=None,
        identity_rule_version=None,
        **overrides,
    )


def _promotion_data_changes(event: EarningsEvent):
    return DataChange.objects.filter(
        target_type=DataChange.TargetType.EARNINGS_EVENT,
        target_id=event.pk,
        rule_version=EARNINGS_CANDIDATE_PROMOTION_RULE_VERSION,
    )


def _promotion_data_change_fields(event: EarningsEvent) -> set[str]:
    return set(_promotion_data_changes(event).values_list("field_name", flat=True))


def _promotion_audits(event: EarningsEvent):
    return AuditRecord.objects.filter(
        target_type=AuditRecord.TargetType.EARNINGS_EVENT,
        target_id=event.pk,
    )


def _schedule_snapshot(event: EarningsEvent) -> dict[str, object]:
    event.refresh_from_db()
    return {field_name: getattr(event, field_name) for field_name in _SCHEDULE_FIELDS}


@pytest.mark.django_db
class TestCandidateCompletion:
    @pytest.mark.parametrize(
        (
            "candidate_date",
            "candidate_type",
            "input_label",
            "expected_type",
            "expected_includes_q4",
        ),
        (
            (None, None, "Q1", "Q1", False),
            (_TARGET_DATE, None, "Q1", "Q1", False),
            (None, "Q1", "Q1", "Q1", False),
            (_TARGET_DATE, "Q1", "Q1", "Q1", False),
            (None, None, "Q4", "FY", True),
            (None, None, "ANNUAL", "FY", True),
            (_TARGET_DATE, "FY", "Q4", "FY", True),
            (None, None, "OTHER", "OTHER", False),
        ),
    )
    def test_candidate_completion_matrix(
        self,
        candidate_date: date | None,
        candidate_type: str | None,
        input_label: str,
        expected_type: str,
        expected_includes_q4: bool,
    ) -> None:
        company = make_company("completion")
        event = _make_candidate(
            company=company,
            period_end_date=candidate_date,
            period_type=candidate_type,
        )
        original_id = event.pk
        original_created_at = event.created_at

        result = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type=input_label,
            sync_run=make_sync_run("completion"),
        )

        event.refresh_from_db()
        assert result.changed is True
        assert result.earnings_event.pk == original_id
        assert event.pk == original_id
        assert event.created_at == original_created_at
        assert event.company_id == company.pk
        assert event.identity_status == IdentityStatus.CANONICAL
        assert event.period_end_date == _TARGET_DATE
        assert event.period_type == expected_type
        assert event.includes_q4 is expected_includes_q4
        assert event.identity_rule_version == IDENTITY_RULE_VERSION
        assert event.identity_key == derive_earnings_identity_key(
            company_id=company.pk,
            period_end_date=_TARGET_DATE,
            period_type=expected_type,
        )

        expected_fields = {
            "identity_status",
            "identity_key",
            "identity_rule_version",
        }
        if candidate_date is None:
            expected_fields.add("period_end_date")
        if candidate_type is None:
            expected_fields.add("period_type")
        if (candidate_type == "FY") is not expected_includes_q4:
            expected_fields.add("includes_q4")
        assert _promotion_data_change_fields(event) == expected_fields

    def test_existing_fy_input_q4_does_not_rewrite_period_type_or_q4(self) -> None:
        event = _make_candidate(
            period_end_date=_TARGET_DATE,
            period_type="FY",
            includes_q4=True,
        )

        result = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q4",
            sync_run=make_sync_run("existing-fy-q4"),
        )

        assert result.changed is True
        assert _promotion_data_change_fields(event) == {
            "identity_status",
            "identity_key",
            "identity_rule_version",
        }
        audit = _promotion_audits(event).get()
        assert audit.before["period_type"] == "FY"
        assert audit.after["period_type"] == "FY"
        assert audit.before["includes_q4"] is True
        assert audit.after["includes_q4"] is True

    def test_unknown_period_label_is_rejected(self) -> None:
        event = _make_candidate()

        with pytest.raises(InvalidEarningsPromotion, match="cannot be normalized"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q5",
                sync_run=make_sync_run("unknown-label"),
            )

        event.refresh_from_db()
        assert event.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(event).count() == 0
        assert _promotion_audits(event).count() == 0

    def test_existing_date_conflict_fails_closed(self) -> None:
        event = _make_candidate(period_end_date=date(2026, 6, 30))

        with pytest.raises(InvalidEarningsPromotion, match="period_end_date conflicts"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                sync_run=make_sync_run("date-conflict"),
            )

        event.refresh_from_db()
        assert event.period_end_date == date(2026, 6, 30)
        assert event.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(event).count() == 0
        assert _promotion_audits(event).count() == 0

    def test_existing_type_conflict_fails_closed(self) -> None:
        event = _make_candidate(period_type="Q2")

        with pytest.raises(InvalidEarningsPromotion, match="period_type conflicts"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                sync_run=make_sync_run("type-conflict"),
            )

        event.refresh_from_db()
        assert event.period_type == "Q2"
        assert event.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(event).count() == 0
        assert _promotion_audits(event).count() == 0

    def test_company_and_identity_fields_come_from_persisted_row(self) -> None:
        company = make_company("persisted-company")
        other_company = make_company("stale-company")
        event = _make_candidate(company=company)
        stale = EarningsEvent.objects.get(pk=event.pk)
        stale.company_id = other_company.pk

        result = promote_earnings_event(
            earnings_event=stale,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            sync_run=make_sync_run("stale-company"),
        )

        event.refresh_from_db()
        assert result.changed is True
        assert event.company_id == company.pk
        assert event.identity_key == derive_earnings_identity_key(
            company_id=company.pk,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
        )

    def test_naive_changed_at_is_rejected(self) -> None:
        event = _make_candidate()

        with pytest.raises(InvalidEarningsPromotion, match="timezone-aware"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                sync_run=make_sync_run("naive-timestamp"),
                changed_at=datetime(2026, 3, 31, 12, 0),
            )

    def test_unpersisted_event_is_rejected(self) -> None:
        event = EarningsEvent(
            company_id=uuid.uuid4(),
            identity_status=IdentityStatus.CANDIDATE,
        )

        with pytest.raises(InvalidEarningsPromotion, match="must be saved"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                sync_run=make_sync_run("unpersisted-event"),
            )


@pytest.mark.django_db
class TestPromotionAuditContract:
    def test_full_completion_writes_exact_data_change_and_audit_shape(self) -> None:
        event = _make_candidate()
        sync_run = make_sync_run("audit-shape")

        result = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            sync_run=sync_run,
        )

        expected_key = derive_earnings_identity_key(
            company_id=event.company_id,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
        )
        assert result.changed is True
        assert len(result.data_changes) == 5
        changes = {item.change.field_name: item.change for item in result.data_changes}
        assert set(changes) == {
            "period_end_date",
            "period_type",
            "identity_status",
            "identity_key",
            "identity_rule_version",
        }
        assert changes["period_end_date"].old_value is None
        assert changes["period_end_date"].new_value == "2026-03-31"
        assert changes["period_type"].old_value is None
        assert changes["period_type"].new_value == "Q1"
        assert changes["identity_status"].old_value == "candidate"
        assert changes["identity_status"].new_value == "canonical"
        assert changes["identity_key"].old_value is None
        assert changes["identity_key"].new_value == expected_key
        assert changes["identity_rule_version"].old_value is None
        assert changes["identity_rule_version"].new_value == IDENTITY_RULE_VERSION
        for change in changes.values():
            assert change.rule_version == EARNINGS_CANDIDATE_PROMOTION_RULE_VERSION
            assert change.sync_run_id == sync_run.pk
            assert change.source_evidence_id is None

        audit = _promotion_audits(event).get()
        assert audit.pk == result.audit_record.pk
        assert audit.action == AuditRecord.Action.UPDATE
        assert audit.sync_run_id == sync_run.pk
        assert set(audit.before) == set(PROMOTION_IDENTITY_FIELDS)
        assert set(audit.after) == set(PROMOTION_IDENTITY_FIELDS)
        assert audit.before["period_end_date"] is None
        assert audit.after["period_end_date"] == "2026-03-31"
        assert audit.before["period_type"] is None
        assert audit.after["period_type"] == "Q1"
        assert audit.before["includes_q4"] is False
        assert audit.after["includes_q4"] is False
        assert audit.before["identity_status"] == "candidate"
        assert audit.after["identity_status"] == "canonical"
        assert audit.before["identity_key"] is None
        assert audit.after["identity_key"] == expected_key
        assert audit.before["identity_rule_version"] is None
        assert audit.after["identity_rule_version"] == IDENTITY_RULE_VERSION

    def test_existing_identity_facts_do_not_write_noop_data_changes(self) -> None:
        event = _make_candidate(
            period_end_date=_TARGET_DATE,
            period_type="Q1",
        )

        promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            sync_run=make_sync_run("noop-existing-facts"),
        )

        assert _promotion_data_change_fields(event) == {
            "identity_status",
            "identity_key",
            "identity_rule_version",
        }
        audit = _promotion_audits(event).get()
        assert audit.before["period_end_date"] == "2026-03-31"
        assert audit.after["period_end_date"] == "2026-03-31"
        assert audit.before["period_type"] == "Q1"
        assert audit.after["period_type"] == "Q1"
        assert audit.before["includes_q4"] is False
        assert audit.after["includes_q4"] is False

    def test_q4_normalization_records_real_includes_q4_change(self) -> None:
        event = _make_candidate()

        promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q4",
            sync_run=make_sync_run("q4-includes-change"),
        )

        changes = {change.field_name: change for change in _promotion_data_changes(event)}
        assert changes["period_type"].old_value is None
        assert changes["period_type"].new_value == "FY"
        assert changes["includes_q4"].old_value is False
        assert changes["includes_q4"].new_value is True

    def test_replay_after_success_returns_noop_without_history(self) -> None:
        event = _make_candidate()
        sync_run = make_sync_run("auto-replay")
        first = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            sync_run=sync_run,
        )
        first_fields = _promotion_data_change_fields(event)
        first_audit_count = _promotion_audits(event).count()

        second = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            sync_run=sync_run,
        )

        assert first.changed is True
        assert second.changed is False
        assert second.data_changes == ()
        assert second.audit_record is None
        assert _promotion_data_change_fields(event) == first_fields
        assert _promotion_audits(event).count() == first_audit_count

    def test_stale_pre_promotion_object_replay_returns_noop(self) -> None:
        event = _make_candidate()
        stale = EarningsEvent.objects.get(pk=event.pk)
        stale.period_end_date = date(2026, 6, 30)
        stale.period_type = "Q2"
        stale.company_id = make_company("stale-replay-other").pk
        sync_run = make_sync_run("stale-replay")
        promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            sync_run=sync_run,
        )

        result = promote_earnings_event(
            earnings_event=stale,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            sync_run=sync_run,
        )

        event.refresh_from_db()
        assert result.changed is False
        assert event.identity_status == IdentityStatus.CANONICAL
        assert _promotion_data_changes(event).count() == 5
        assert _promotion_audits(event).count() == 1

    def test_canonical_event_with_different_date_fails_closed(self) -> None:
        event = make_event(period_end_date=_TARGET_DATE, period_type="Q1")
        before_counts = (
            _promotion_data_changes(event).count(),
            _promotion_audits(event).count(),
        )

        with pytest.raises(InvalidEarningsPromotion, match="Canonical"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=date(2026, 6, 30),
                period_type="Q1",
                sync_run=make_sync_run("canonical-different-date"),
            )

        assert (
            _promotion_data_changes(event).count(),
            _promotion_audits(event).count(),
        ) == before_counts

    def test_canonical_event_with_different_identity_fails_closed(self) -> None:
        event = make_event(period_end_date=_TARGET_DATE, period_type="Q1")
        before_counts = (
            _promotion_data_changes(event).count(),
            _promotion_audits(event).count(),
        )

        with pytest.raises(InvalidEarningsPromotion, match="Canonical"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q2",
                sync_run=make_sync_run("canonical-different-type"),
            )

        assert (
            _promotion_data_changes(event).count(),
            _promotion_audits(event).count(),
        ) == before_counts

    def test_canonical_event_with_corrupt_identity_metadata_fails_closed(self) -> None:
        event = make_event(period_end_date=_TARGET_DATE, period_type="Q1")
        EarningsEvent.objects.filter(pk=event.pk).update(
            identity_key=hashlib.sha256(b"corrupt-identity-key").hexdigest()
        )

        with pytest.raises(EarningsPromotionIntegrityError):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                sync_run=make_sync_run("corrupt-canonical"),
            )


@pytest.mark.django_db
class TestManualPromotionContext:
    def test_manual_promotion_requires_actor_reason_and_request_id(self) -> None:
        event = _make_candidate()
        actor = User.objects.create_user(
            email="manual-promotion@example.com",
            password="test-password-only",
        )

        with pytest.raises(InvalidEarningsPromotion, match="provenance"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
            )
        with pytest.raises(InvalidEarningsPromotion, match="reason"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                actor_user=actor,
                request_id="manual-promotion-1",
            )
        with pytest.raises(InvalidEarningsPromotion, match="request_id"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                actor_user=actor,
                reason="Confirmed identity facts.",
            )

        event.refresh_from_db()
        assert event.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(event).count() == 0
        assert _promotion_audits(event).count() == 0

    def test_manual_promotion_writes_actor_origin_and_replays_without_history(self) -> None:
        event = _make_candidate()
        actor = User.objects.create_user(
            email="manual-promotion-success@example.com",
            password="test-password-only",
        )
        request_id = "manual-promotion-success-1"

        first = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            actor_user=actor,
            reason="Confirmed identity facts.",
            request_id=request_id,
        )
        first_counts = (
            _promotion_data_changes(event).count(),
            _promotion_audits(event).count(),
        )
        second = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            actor_user=actor,
            reason="Confirmed identity facts.",
            request_id=request_id,
        )

        assert first.changed is True
        assert first.audit_record.actor_user_id == actor.pk
        assert first.audit_record.reason == "Confirmed identity facts."
        for item in first.data_changes:
            assert item.change.actor_user_id == actor.pk
            assert item.change.origin_key == request_id
        assert second.changed is False
        assert (
            _promotion_data_changes(event).count(),
            _promotion_audits(event).count(),
        ) == first_counts


@pytest.mark.django_db
class TestSourceEvidenceProvenance:
    def test_record_level_evidence_is_reused_for_all_data_changes(self) -> None:
        event = _make_candidate()
        sync_run, evidence = make_source_evidence(
            event=event,
            field_name="",
            normalized_value={"identity": "record-level"},
            suffix="record-level",
        )

        result = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            source_evidence=evidence,
        )

        assert result.changed is True
        assert len(result.data_changes) == 5
        for item in result.data_changes:
            assert item.change.source_evidence_id == evidence.pk
            assert item.change.sync_run_id == sync_run.pk
        assert result.audit_record.sync_run_id == sync_run.pk

    def test_sync_run_only_path_is_supported(self) -> None:
        event = _make_candidate()
        sync_run = make_sync_run("sync-run-only")

        result = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            sync_run=sync_run,
        )

        assert result.changed is True
        for item in result.data_changes:
            assert item.change.source_evidence_id is None
            assert item.change.sync_run_id == sync_run.pk
        assert result.audit_record.sync_run_id == sync_run.pk

    def test_existing_event_source_evidence_is_preserved(self) -> None:
        event = _make_candidate()
        _, existing_evidence = make_source_evidence(
            event=event,
            field_name="",
            normalized_value={"origin": "existing"},
            suffix="existing-event-evidence",
        )
        event.source_evidence = existing_evidence
        event.save(update_fields=("source_evidence",))
        _, promotion_evidence = make_source_evidence(
            event=event,
            field_name="",
            normalized_value={"origin": "promotion"},
            suffix="promotion-event-evidence",
        )

        result = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            source_evidence=promotion_evidence,
        )

        result.earnings_event.refresh_from_db()
        assert result.earnings_event.source_evidence_id == existing_evidence.pk

    def test_wrong_evidence_target_type_is_rejected(self) -> None:
        event = _make_candidate()
        other_event = make_event(period_end_date=date(2026, 6, 30), period_type="Q2")
        _, evidence = make_source_evidence(
            event=other_event,
            field_name="",
            normalized_value={"identity": "wrong-type"},
            suffix="wrong-type",
        )
        SourceEvidence.objects.filter(pk=evidence.pk).update(
            target_type=SourceEvidence.TargetType.COMPANY,
            target_id=event.company_id,
        )

        with pytest.raises(InvalidEarningsPromotion, match="same domain target"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                source_evidence=SourceEvidence.objects.get(pk=evidence.pk),
            )

        event.refresh_from_db()
        assert event.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(event).count() == 0
        assert _promotion_audits(event).count() == 0

    def test_wrong_evidence_target_id_is_rejected(self) -> None:
        event = _make_candidate()
        other_event = make_event(period_end_date=date(2026, 6, 30), period_type="Q2")
        _, evidence = make_source_evidence(
            event=other_event,
            field_name="",
            normalized_value={"identity": "wrong-id"},
            suffix="wrong-id",
        )

        with pytest.raises(InvalidEarningsPromotion, match="same domain target"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                source_evidence=evidence,
            )

    def test_wrong_evidence_field_is_rejected(self) -> None:
        event = _make_candidate()
        _, evidence = make_source_evidence(
            event=event,
            field_name="estimated_release",
            normalized_value={"kind": "date", "precision": "date_only", "value": "2026-10-24"},
            suffix="wrong-promotion-field",
        )

        with pytest.raises(InvalidEarningsPromotion, match="changed domain field"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                source_evidence=evidence,
            )

    def test_evidence_for_unchanged_identity_field_is_rejected(self) -> None:
        event = _make_candidate(
            period_end_date=_TARGET_DATE,
            period_type="Q1",
        )
        _, evidence = make_source_evidence(
            event=event,
            field_name="period_end_date",
            normalized_value="2026-03-31",
            suffix="unchanged-promotion-field",
        )

        with pytest.raises(InvalidEarningsPromotion, match="changed domain field"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                source_evidence=evidence,
            )

    def test_unsaved_evidence_is_rejected(self) -> None:
        event = _make_candidate()
        unsaved = SourceEvidence(
            id=uuid.uuid4(),
            raw_data_record_id=uuid.uuid4(),
            sync_run_id=uuid.uuid4(),
            target_type=SourceEvidence.TargetType.EARNINGS_EVENT,
            target_id=event.pk,
            field_name="",
            raw_value={"identity": "unsaved"},
            normalized_value={"identity": "unsaved"},
            is_official=False,
            confidence=Decimal("0.5000"),
            observed_at=timezone.now(),
            normalizer_version="fixture-v1",
            evidence_key=uuid.uuid4().hex * 2,
        )

        with pytest.raises(InvalidEarningsPromotion, match="must be saved"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                source_evidence=unsaved,
            )

    def test_in_memory_evidence_mutation_does_not_override_persisted_chain(self) -> None:
        event = _make_candidate()
        sync_run, evidence = make_source_evidence(
            event=event,
            field_name="",
            normalized_value={"identity": "persisted"},
            suffix="forged-memory",
        )
        forged = SourceEvidence.objects.get(pk=evidence.pk)
        forged.target_id = uuid.uuid4()
        forged.sync_run_id = uuid.uuid4()

        result = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            source_evidence=forged,
        )

        assert result.changed is True
        for item in result.data_changes:
            assert item.change.source_evidence_id == evidence.pk
            assert item.change.sync_run_id == sync_run.pk

    def test_invalid_source_observation_chain_is_rejected(self) -> None:
        event = _make_candidate()
        source_a = DataSource.objects.create(
            key=f"invalid-chain-a-{uuid.uuid4().hex[:8]}",
            name="Invalid chain A",
            source_type=DataSource.SourceType.MANUAL,
            base_url="https://example.test/a",
        )
        run_a = SyncRun.objects.create(
            job_type="fixture.invalid-chain",
            source=source_a,
            idempotency_key=f"invalid-chain-a:{uuid.uuid4()}",
        )
        raw_record = RawDataRecord.objects.create(
            source=source_a,
            first_sync_run=run_a,
            source_url="https://example.test/a/raw",
            request_fingerprint=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            fetched_at=timezone.now(),
            http_status=200,
            content_type="application/json",
            encoding="utf-8",
            content_hash=hashlib.sha256(b"invalid-chain").hexdigest(),
            payload=b"invalid-chain",
            payload_size_bytes=len(b"invalid-chain"),
        )
        RawDataObservation.objects.create(
            sync_run=run_a,
            raw_data_record=raw_record,
            observed_at=timezone.now(),
        )
        source_b = DataSource.objects.create(
            key=f"invalid-chain-b-{uuid.uuid4().hex[:8]}",
            name="Invalid chain B",
            source_type=DataSource.SourceType.MANUAL,
            base_url="https://example.test/b",
        )
        run_b = SyncRun.objects.create(
            job_type="fixture.invalid-chain",
            source=source_b,
            idempotency_key=f"invalid-chain-b:{uuid.uuid4()}",
        )
        RawDataObservation.objects.create(
            sync_run=run_b,
            raw_data_record=raw_record,
            observed_at=timezone.now(),
        )
        evidence = SourceEvidence.objects.create(
            raw_data_record=raw_record,
            sync_run=run_b,
            target_type=SourceEvidence.TargetType.EARNINGS_EVENT,
            target_id=event.pk,
            field_name="",
            raw_value={"identity": "bad-chain"},
            normalized_value={"identity": "bad-chain"},
            is_official=False,
            confidence=Decimal("0.9000"),
            observed_at=timezone.now(),
            normalizer_version="fixture-v1",
            evidence_key=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        )

        with pytest.raises(InvalidEarningsPromotion, match="same DataSource"):
            promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                source_evidence=evidence,
            )

        event.refresh_from_db()
        assert event.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(event).count() == 0
        assert _promotion_audits(event).count() == 0


@pytest.mark.django_db
class TestPromotionCollision:
    def test_existing_canonical_collision_fails_closed(self) -> None:
        company = make_company("collision")
        canonical = make_event(
            company=company,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
        )
        candidate = _make_candidate(company=company)

        with pytest.raises(EarningsPromotionCollision) as exc_info:
            promote_earnings_event(
                earnings_event=candidate,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                sync_run=make_sync_run("collision"),
            )

        error = exc_info.value
        assert error.candidate_id == candidate.pk
        assert error.existing_canonical_id == canonical.pk
        assert error.derived_identity_key == canonical.identity_key
        candidate.refresh_from_db()
        canonical.refresh_from_db()
        assert candidate.identity_status == IdentityStatus.CANDIDATE
        assert canonical.identity_key == error.derived_identity_key
        assert _promotion_data_changes(candidate).count() == 0
        assert _promotion_audits(candidate).count() == 0

    def test_business_tuple_collision_with_mismatched_key_fails_closed(self) -> None:
        company = make_company("tuple-collision")
        canonical = make_event(
            company=company,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
        )
        mismatched_key = hashlib.sha256(b"mismatched-canonical-key").hexdigest()
        EarningsEvent.objects.filter(pk=canonical.pk).update(identity_key=mismatched_key)
        canonical.refresh_from_db()
        candidate = _make_candidate(company=company)

        with pytest.raises(EarningsPromotionCollision) as exc_info:
            promote_earnings_event(
                earnings_event=candidate,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                sync_run=make_sync_run("tuple-collision"),
            )

        expected_key = derive_earnings_identity_key(
            company_id=company.pk,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
        )
        assert exc_info.value.existing_canonical_id == canonical.pk
        assert exc_info.value.derived_identity_key == expected_key
        assert mismatched_key != expected_key
        assert _promotion_data_changes(candidate).count() == 0
        assert _promotion_audits(candidate).count() == 0

    def test_identity_unique_violation_is_classified_after_savepoint_rollback(self) -> None:
        company = make_company("identity-race")
        canonical = make_event(
            company=company,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
        )
        candidate = _make_candidate(company=company)

        with mock.patch(
            "earnings.services.promotion._precheck_canonical_owner",
            return_value=None,
        ):
            with pytest.raises(EarningsPromotionCollision) as exc_info:
                promote_earnings_event(
                    earnings_event=candidate,
                    period_end_date=_TARGET_DATE,
                    period_type="Q1",
                    sync_run=make_sync_run("identity-race"),
                )

        assert exc_info.value.existing_canonical_id == canonical.pk
        candidate.refresh_from_db()
        assert candidate.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(candidate).count() == 0
        assert _promotion_audits(candidate).count() == 0

    def test_business_tuple_violation_is_classified_after_savepoint_rollback(self) -> None:
        company = make_company("tuple-race")
        canonical = make_event(
            company=company,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
        )
        EarningsEvent.objects.filter(pk=canonical.pk).update(
            identity_key=hashlib.sha256(b"tuple-race-canonical").hexdigest()
        )
        candidate = _make_candidate(company=company)

        with mock.patch(
            "earnings.services.promotion._precheck_canonical_owner",
            return_value=None,
        ):
            with pytest.raises(EarningsPromotionCollision) as exc_info:
                promote_earnings_event(
                    earnings_event=candidate,
                    period_end_date=_TARGET_DATE,
                    period_type="Q1",
                    sync_run=make_sync_run("tuple-race"),
                )

        assert exc_info.value.existing_canonical_id == canonical.pk
        candidate.refresh_from_db()
        assert candidate.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(candidate).count() == 0
        assert _promotion_audits(candidate).count() == 0

    def test_unknown_integrity_error_is_reraised_and_rolled_back(self) -> None:
        event = _make_candidate()

        with mock.patch.object(
            EarningsEvent,
            "save",
            side_effect=IntegrityError("unexpected promotion integrity failure"),
        ):
            with pytest.raises(IntegrityError, match="unexpected promotion integrity failure"):
                promote_earnings_event(
                    earnings_event=event,
                    period_end_date=_TARGET_DATE,
                    period_type="Q1",
                    sync_run=make_sync_run("unknown-integrity"),
                )

        event.refresh_from_db()
        assert event.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(event).count() == 0
        assert _promotion_audits(event).count() == 0

    def test_partial_data_change_failure_rolls_back_all_writes(self) -> None:
        event = _make_candidate()
        original_record_data_change = promotion_module.record_data_change
        calls = {"count": 0}

        def flaky_record_data_change(**kwargs: object):
            calls["count"] += 1
            if calls["count"] == 2:
                raise IntegrityError("second identity DataChange failed")
            return original_record_data_change(**kwargs)

        with mock.patch(
            "earnings.services.promotion.record_data_change",
            side_effect=flaky_record_data_change,
        ):
            with pytest.raises(IntegrityError, match="second identity DataChange failed"):
                promote_earnings_event(
                    earnings_event=event,
                    period_end_date=_TARGET_DATE,
                    period_type="Q1",
                    sync_run=make_sync_run("partial-data-change"),
                )

        event.refresh_from_db()
        assert event.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(event).count() == 0
        assert _promotion_audits(event).count() == 0

    def test_audit_failure_rolls_back_identity_and_data_changes(self) -> None:
        event = _make_candidate()

        with mock.patch(
            "earnings.services.promotion.record_system_action",
            side_effect=IntegrityError("promotion audit failed"),
        ):
            with pytest.raises(IntegrityError, match="promotion audit failed"):
                promote_earnings_event(
                    earnings_event=event,
                    period_end_date=_TARGET_DATE,
                    period_type="Q1",
                    sync_run=make_sync_run("audit-rollback"),
                )

        event.refresh_from_db()
        assert event.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(event).count() == 0
        assert _promotion_audits(event).count() == 0


@pytest.mark.django_db
class TestStatusSchedulePreservation:
    @pytest.mark.parametrize(
        "target_status",
        (
            EventStatus.SCHEDULED_ESTIMATED,
            EventStatus.SCHEDULED_CONFIRMED,
            EventStatus.RELEASED,
            EventStatus.CANCELLED,
        ),
    )
    def test_promotion_preserves_status_schedule_history_and_fiscal_metadata(
        self,
        target_status: str,
    ) -> None:
        event = _make_candidate(
            status=EventStatus.SCHEDULED_ESTIMATED,
            fiscal_year=2026,
            fiscal_calendar_type=FiscalCalendarType.WEEK_BASED_52_53,
            period_length_weeks=53,
            estimated_release_date=date(2026, 10, 24),
            estimated_release_precision="date_only",
            release_session="after_market",
        )
        history_run = make_sync_run("preserve-history")
        update_earnings_schedule(
            earnings_event=event,
            changes={"conference_call": date(2026, 10, 25)},
            sync_run=history_run,
        )
        if target_status == EventStatus.SCHEDULED_CONFIRMED:
            confirm_earnings_event(earnings_event=event, sync_run=history_run)
        elif target_status == EventStatus.RELEASED:
            mark_earnings_released(earnings_event=event, sync_run=history_run)
        elif target_status == EventStatus.CANCELLED:
            cancel_earnings_event(
                earnings_event=event,
                affirmative_cancellation=True,
                sync_run=history_run,
            )

        event.refresh_from_db()
        before_status = event.status
        before_schedule = _schedule_snapshot(event)
        before_date_changes = EarningsDateChange.objects.filter(earnings_event=event).count()
        before_status_changes = DataChange.objects.filter(
            target_id=event.pk,
            field_name="status",
        ).count()
        before_audits = _promotion_audits(event).count()

        result = promote_earnings_event(
            earnings_event=event,
            period_end_date=_TARGET_DATE,
            period_type="Q1",
            sync_run=make_sync_run("preserve-promotion"),
        )

        event.refresh_from_db()
        assert result.changed is True
        assert before_status == target_status
        assert event.status == before_status
        assert _schedule_snapshot(event) == before_schedule
        assert (
            EarningsDateChange.objects.filter(earnings_event=event).count() == before_date_changes
        )
        assert (
            DataChange.objects.filter(target_id=event.pk, field_name="status").count()
            == before_status_changes
        )
        assert _promotion_audits(event).count() == before_audits + 1
        assert event.fiscal_year == 2026
        assert event.fiscal_calendar_type == FiscalCalendarType.WEEK_BASED_52_53
        assert event.period_length_weeks == 53
        assert not EarningsDateChange.objects.filter(
            earnings_event=event,
            data_change__rule_version=EARNINGS_CANDIDATE_PROMOTION_RULE_VERSION,
        ).exists()


@pytest.mark.django_db(transaction=True)
class TestPromotionConcurrency:
    def _run_concurrently(
        self,
        workers: tuple[Callable[[], object], Callable[[], object]],
    ) -> tuple[list[object], list[BaseException]]:
        from django.db import close_old_connections, connections

        barrier = threading.Barrier(2, timeout=10)
        results: list[object] = []
        errors: list[BaseException] = []

        def run(worker: Callable[[], object]) -> None:
            close_old_connections()
            try:
                barrier.wait()
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '5s'")
                    results.append(worker())
            except (
                EarningsPromotionServiceError,
                IntegrityError,
                OperationalError,
            ) as error:
                errors.append(error)
            finally:
                for conn in connections.all():
                    conn.close()

        threads = [threading.Thread(target=run, args=(worker,)) for worker in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        for thread in threads:
            assert not thread.is_alive(), "Concurrent promotion thread hung"

        return results, errors

    @staticmethod
    def _worker(
        *,
        event_id: uuid.UUID,
        sync_run: SyncRun,
    ) -> Callable[[], object]:
        def run() -> object:
            event = EarningsEvent.objects.get(pk=event_id)
            return promote_earnings_event(
                earnings_event=event,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
                sync_run=sync_run,
            )

        return run

    def test_same_candidate_race_produces_one_mutation_and_one_noop(self) -> None:
        event = _make_candidate()
        sync_run = make_sync_run("concurrent-same-candidate")
        worker = self._worker(event_id=event.pk, sync_run=sync_run)

        results, errors = self._run_concurrently((worker, worker))

        assert errors == []
        assert len(results) == 2
        assert sorted(result.changed for result in results) == [False, True]
        event.refresh_from_db()
        assert event.identity_status == IdentityStatus.CANONICAL
        assert _promotion_data_changes(event).count() == 5
        assert _promotion_audits(event).count() == 1

    def test_two_candidates_same_identity_race_produces_one_collision(self) -> None:
        company = make_company("concurrent-two-candidates")
        candidate_a = _make_candidate(company=company)
        candidate_b = _make_candidate(company=company)
        sync_run = make_sync_run("concurrent-two-candidates")
        worker_a = self._worker(event_id=candidate_a.pk, sync_run=sync_run)
        worker_b = self._worker(event_id=candidate_b.pk, sync_run=sync_run)

        results, errors = self._run_concurrently((worker_a, worker_b))

        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], EarningsPromotionCollision)
        winner_id = results[0].earnings_event.pk
        loser = candidate_b if winner_id == candidate_a.pk else candidate_a
        winner = candidate_a if winner_id == candidate_a.pk else candidate_b
        loser.refresh_from_db()
        winner.refresh_from_db()
        assert winner.identity_status == IdentityStatus.CANONICAL
        assert loser.identity_status == IdentityStatus.CANDIDATE
        assert _promotion_data_changes(loser).count() == 0
        assert _promotion_audits(loser).count() == 0
        assert (
            EarningsEvent.objects.filter(
                identity_status=IdentityStatus.CANONICAL,
                company=company,
                period_end_date=_TARGET_DATE,
                period_type="Q1",
            ).count()
            == 1
        )
