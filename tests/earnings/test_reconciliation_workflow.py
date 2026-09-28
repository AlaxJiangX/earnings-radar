# mypy: ignore-errors
"""Stage 4.2E exact reconciliation and manual-authority tests."""

from __future__ import annotations

import threading
import uuid
from datetime import date
from decimal import Decimal
from unittest import mock

import pytest
from django.db import close_old_connections, connections

from audit.models import AuditRecord, RawDataObservation, SourceEvidence
from audit.services import record_source_evidence
from companies.models import Company
from earnings.models import (
    EarningsEvent,
    EarningsReconciliationDecision,
    FiscalCalendarType,
    IdentityStatus,
)
from earnings.services.date_changes import update_earnings_schedule
from earnings.services.promotion import EarningsPromotionCollision
from earnings.services.reconciliation import record_earnings_reconciliation_decision
from earnings.services.reconciliation_workflow import (
    EARNINGS_RECONCILIATION_VERSION,
    EarningsReconciliationOutcome,
    reconcile_earnings_candidate,
    resolve_earnings_reconciliation_manually,
)
from tests.earnings.helpers import (
    make_calendar_observation,
    make_calendar_raw_record,
    make_calendar_source,
    make_company,
    make_event,
    make_sync_run,
    make_user,
)


def _candidate(
    suffix: str,
    *,
    company: Company | None = None,
    event_id: uuid.UUID | None = None,
    source=None,
    provider_event_id: str | None = None,
    period_end_date: date | None = date(2026, 3, 31),
    period_type: str | None = "Q1",
    fiscal_calendar_type: str = FiscalCalendarType.MONTH_BASED,
    period_length_weeks: int | None = None,
    fiscal_year: int | None = 2026,
    estimated_release_date: date | None = date(2026, 4, 22),
    release_session: str = "after_market",
    status: str = "scheduled_estimated",
) -> tuple[EarningsEvent, object, object]:
    company = company or make_company(suffix)
    source = source or make_calendar_source(suffix)
    raw_record = make_calendar_raw_record(source, suffix)
    observation = make_calendar_observation(
        source=source,
        raw_data_record=raw_record,
        provider_event_id=provider_event_id or f"evt-{suffix}-{uuid.uuid4().hex[:8]}",
        cik=company.cik or "",
        period_end_date=period_end_date,
        period_type=period_type,
        fiscal_calendar_type=fiscal_calendar_type,
        period_length_weeks=period_length_weeks,
        fiscal_year=fiscal_year,
        estimated_release_date=estimated_release_date,
        estimated_release_at=None,
        estimated_release_precision=("date_only" if estimated_release_date else "unknown"),
        release_session=release_session,
    )
    sync_run = raw_record.first_sync_run
    RawDataObservation.objects.create(
        sync_run=sync_run,
        raw_data_record=raw_record,
    )
    candidate_id = event_id or uuid.uuid4()
    evidence = record_source_evidence(
        raw_data_record=raw_record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.EARNINGS_EVENT,
        target_id=candidate_id,
        field_name="company_match",
        raw_value={"cik": company.cik or ""},
        normalized_value={"fixture": suffix},
        confidence=Decimal("0.9000"),
        normalizer_version="earnings-company-match-v1",
    ).evidence
    event = EarningsEvent.objects.create(
        id=candidate_id,
        company=company,
        identity_status=IdentityStatus.CANDIDATE,
        period_end_date=period_end_date,
        period_type=period_type,
        includes_q4=period_type == "FY",
        fiscal_calendar_type=fiscal_calendar_type,
        period_length_weeks=period_length_weeks,
        fiscal_year=fiscal_year,
        estimated_release_date=estimated_release_date,
        estimated_release_precision=("date_only" if estimated_release_date else "unknown"),
        release_session=release_session,
        status=status,
        source_evidence=evidence,
    )
    decision = record_earnings_reconciliation_decision(
        observation=observation,
        decision_type="created_candidate",
        status="resolved",
        rule_version="earnings-company-match-v1",
        target_event=event,
        match_factors={"company_match": {"fixture": suffix}},
        reason="Fixture candidate lineage.",
        sync_run=sync_run,
    ).decision
    return event, observation, decision


@pytest.mark.django_db
class TestAutomaticReconciliation:
    def test_single_complete_candidate_promotes_and_replays_deterministically(self) -> None:
        candidate, _observation, _lineage = _candidate("single")
        sync_run = make_sync_run("reconcile-single")

        first = reconcile_earnings_candidate(
            earnings_event=candidate,
            sync_run=sync_run,
        )
        second = reconcile_earnings_candidate(
            earnings_event=candidate,
            sync_run=make_sync_run("reconcile-single-replay"),
        )

        assert first.outcome is EarningsReconciliationOutcome.NOT_DUPLICATE
        assert first.promoted is True
        assert first.subject.identity_status == IdentityStatus.CANONICAL
        assert first.decision.decision_type == "matched_canonical"
        assert first.decision.match_factors["earnings_reconciliation"][
            "canonical_target_id"
        ] == str(candidate.pk)
        assert first.reconciliation_input_revision == second.reconciliation_input_revision
        assert first.reconciliation_execution_key == second.reconciliation_execution_key
        assert second.decision.pk == first.decision.pk
        assert second.decision_created is False
        assert (
            EarningsReconciliationDecision.objects.filter(
                observation=first.observation,
                rule_version=EARNINGS_RECONCILIATION_VERSION,
            ).count()
            == 1
        )
        assert (
            AuditRecord.objects.filter(
                target_type=AuditRecord.TargetType.EARNINGS_RECONCILIATION_DECISION,
                target_id=first.decision.pk,
            ).count()
            == 1
        )
        assert (
            SourceEvidence.objects.filter(
                target_type=SourceEvidence.TargetType.EARNINGS_EVENT,
                target_id=candidate.pk,
            ).count()
            == 1
        )

    def test_same_company_different_periods_do_not_collapse(self) -> None:
        company = make_company("different-periods")
        first, _first_observation, _ = _candidate(
            "different-q1",
            company=company,
            period_end_date=date(2026, 3, 31),
            period_type="Q1",
        )
        second, _second_observation, _ = _candidate(
            "different-q2",
            company=company,
            period_end_date=date(2026, 6, 30),
            period_type="Q2",
        )

        first_result = reconcile_earnings_candidate(
            earnings_event=first,
            sync_run=make_sync_run("different-q1"),
        )
        second_result = reconcile_earnings_candidate(
            earnings_event=second,
            sync_run=make_sync_run("different-q2"),
        )

        assert first_result.outcome is EarningsReconciliationOutcome.NOT_DUPLICATE
        assert second_result.outcome is EarningsReconciliationOutcome.NOT_DUPLICATE
        assert first_result.winner.pk != second_result.winner.pk
        assert EarningsEvent.objects.filter(identity_status=IdentityStatus.CANONICAL).count() == 2

    def test_exact_duplicates_use_completeness_then_preserve_loser(self) -> None:
        company = make_company("completeness")
        less_complete, less_observation, _ = _candidate(
            "less-complete",
            company=company,
            fiscal_calendar_type=FiscalCalendarType.UNKNOWN,
            fiscal_year=None,
            estimated_release_date=None,
            release_session="unknown",
        )
        more_complete, _more_observation, _ = _candidate(
            "more-complete",
            company=company,
        )

        loser_result = reconcile_earnings_candidate(
            earnings_event=less_complete,
            sync_run=make_sync_run("completeness-loser"),
        )
        winner_result = reconcile_earnings_candidate(
            earnings_event=more_complete,
            sync_run=make_sync_run("completeness-winner"),
        )

        assert loser_result.outcome is EarningsReconciliationOutcome.DEFINITE_DUPLICATE
        assert loser_result.winner.pk == more_complete.pk
        assert loser_result.decision.decision_type == "duplicate_of"
        assert winner_result.promoted is True
        less_complete.refresh_from_db()
        assert less_complete.identity_status == IdentityStatus.CANDIDATE
        assert EarningsReconciliationDecision.objects.filter(
            observation=less_observation,
            decision_type="created_candidate",
        ).exists()
        assert less_complete.source_evidence_id is not None

    def test_equal_completeness_uses_uuid_tiebreaker_not_creation_order(self) -> None:
        company = make_company("uuid-order")
        high_id = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
        low_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
        high, _high_observation, _ = _candidate(
            "uuid-high",
            company=company,
            event_id=high_id,
        )
        low, _low_observation, _ = _candidate(
            "uuid-low",
            company=company,
            event_id=low_id,
        )

        result = reconcile_earnings_candidate(
            earnings_event=high,
            sync_run=make_sync_run("uuid-tiebreaker"),
        )

        assert result.winner.pk == low.pk
        assert result.decision.decision_type == "duplicate_of"

    def test_known_schedule_conflict_enters_review_and_blocks_promotion(self) -> None:
        company = make_company("schedule-conflict")
        first, _first_observation, _ = _candidate(
            "schedule-first",
            company=company,
            estimated_release_date=date(2026, 4, 22),
        )
        second, _second_observation, _ = _candidate(
            "schedule-second",
            company=company,
            estimated_release_date=date(2026, 4, 23),
        )

        result = reconcile_earnings_candidate(
            earnings_event=first,
            sync_run=make_sync_run("schedule-conflict"),
        )

        assert result.outcome is EarningsReconciliationOutcome.REVIEW_REQUIRED
        assert result.decision.status == "open"
        assert result.decision.target_event_id is None
        assert "ESTIMATED_RELEASE_CONFLICT" in result.conflict_codes
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.identity_status == second.identity_status == IdentityStatus.CANDIDATE

    def test_known_fiscal_conflict_enters_review(self) -> None:
        company = make_company("fiscal-conflict")
        first, _first_observation, _ = _candidate(
            "fiscal-month",
            company=company,
            fiscal_calendar_type=FiscalCalendarType.MONTH_BASED,
        )
        _second, _second_observation, _ = _candidate(
            "fiscal-week",
            company=company,
            fiscal_calendar_type=FiscalCalendarType.WEEK_BASED_52_53,
            period_length_weeks=52,
        )

        result = reconcile_earnings_candidate(
            earnings_event=first,
            sync_run=make_sync_run("fiscal-conflict"),
        )

        assert result.outcome is EarningsReconciliationOutcome.REVIEW_REQUIRED
        assert "FISCAL_CALENDAR_CONFLICT" in result.conflict_codes
        assert result.promoted is False

    def test_promotion_collision_fails_closed_as_open_review(self) -> None:
        candidate, _observation, _ = _candidate("automatic-collision")
        existing_canonical_id = uuid.uuid4()
        collision = EarningsPromotionCollision(
            candidate_id=candidate.pk,
            existing_canonical_id=existing_canonical_id,
            derived_identity_key="fixture-canonical-collision",
        )

        with mock.patch(
            "earnings.services.reconciliation_workflow.promote_earnings_event",
            side_effect=collision,
        ):
            result = reconcile_earnings_candidate(
                earnings_event=candidate,
                sync_run=make_sync_run("automatic-collision"),
            )

        candidate.refresh_from_db()
        assert result.outcome is EarningsReconciliationOutcome.REVIEW_REQUIRED
        assert result.decision.decision_type == "collision"
        assert result.decision.status == "open"
        assert result.decision.target_event_id is None
        assert result.decision.match_factors["earnings_reconciliation"][
            "canonical_collision_event_id"
        ] == str(existing_canonical_id)
        assert candidate.identity_status == IdentityStatus.CANDIDATE

    def test_open_review_requires_manual_resolution_before_automation_continues(self) -> None:
        company = make_company("open-review-block")
        first, observation, _lineage = _candidate(
            "open-review-block-a",
            company=company,
            estimated_release_date=date(2026, 4, 22),
        )
        second, _second_observation, _ = _candidate(
            "open-review-block-b",
            company=company,
            estimated_release_date=date(2026, 4, 23),
        )

        opened = reconcile_earnings_candidate(
            earnings_event=first,
            sync_run=make_sync_run("open-review-block-open"),
        )
        assert opened.outcome is EarningsReconciliationOutcome.REVIEW_REQUIRED
        assert opened.decision.status == "open"

        update_earnings_schedule(
            earnings_event=second,
            changes={"estimated_release": date(2026, 4, 22)},
            sync_run=make_sync_run("open-review-block-fix"),
        )

        blocked = reconcile_earnings_candidate(
            earnings_event=first,
            sync_run=make_sync_run("open-review-block-blocked"),
        )

        assert blocked.outcome is EarningsReconciliationOutcome.REVIEW_REQUIRED
        assert blocked.decision.decision_type == "review_required"
        assert blocked.decision.status == "open"
        assert blocked.decision.target_event_id is None
        assert blocked.decision.supersedes_id == opened.decision.pk
        assert blocked.conflict_codes == ("OPEN_REVIEW",)
        assert blocked.promoted is False
        first.refresh_from_db()
        assert first.identity_status == IdentityStatus.CANDIDATE

        replay = reconcile_earnings_candidate(
            earnings_event=first,
            sync_run=make_sync_run("open-review-block-replay"),
        )
        assert replay.decision.pk == blocked.decision.pk
        assert replay.decision_created is False

        actor = make_user("open-review-block")
        resolved = resolve_earnings_reconciliation_manually(
            observation=observation,
            decision_type="matched_candidate",
            target_event=second,
            actor_user=actor,
            reason="Manual review binds the two observations to one exact event.",
            request_id="open-review-block-manual",
        )
        assert resolved.decision.supersedes_id == blocked.decision.pk

        after_manual = reconcile_earnings_candidate(
            earnings_event=first,
            sync_run=make_sync_run("open-review-block-after-manual"),
        )
        assert after_manual.blocked_by_manual_authority is True
        assert after_manual.decision.pk == resolved.decision.pk

    def test_unknown_and_known_are_compatible_and_known_fact_is_retained(self) -> None:
        company = make_company("unknown-compatible")
        unknown, _unknown_observation, _ = _candidate(
            "unknown-compatible-a",
            company=company,
            estimated_release_date=None,
            release_session="unknown",
            fiscal_calendar_type=FiscalCalendarType.UNKNOWN,
        )
        known, _known_observation, _ = _candidate(
            "unknown-compatible-b",
            company=company,
            estimated_release_date=date(2026, 4, 24),
            release_session="after_market",
            fiscal_calendar_type=FiscalCalendarType.MONTH_BASED,
        )

        result = reconcile_earnings_candidate(
            earnings_event=known,
            sync_run=make_sync_run("unknown-known"),
        )

        assert result.outcome is EarningsReconciliationOutcome.DEFINITE_DUPLICATE
        assert result.promoted is True
        result.winner.refresh_from_db()
        assert result.winner.estimated_release_date == date(2026, 4, 24)
        assert result.winner.release_session == "after_market"
        unknown.refresh_from_db()
        assert unknown.identity_status == IdentityStatus.CANDIDATE

    def test_existing_canonical_wins_without_destructive_merge(self) -> None:
        company = make_company("canonical-wins")
        canonical = make_event(company=company)
        candidate, observation, _ = _candidate(
            "canonical-loser",
            company=company,
        )

        result = reconcile_earnings_candidate(
            earnings_event=candidate,
            sync_run=make_sync_run("canonical-wins"),
        )

        assert result.outcome is EarningsReconciliationOutcome.DEFINITE_DUPLICATE
        assert result.winner.pk == canonical.pk
        assert result.decision.decision_type == "duplicate_of"
        candidate.refresh_from_db()
        assert candidate.identity_status == IdentityStatus.CANDIDATE
        assert EarningsReconciliationDecision.objects.filter(
            observation=observation,
            decision_type="created_candidate",
        ).exists()

    def test_incomplete_identity_requires_review(self) -> None:
        candidate, _observation, _ = _candidate(
            "incomplete",
            period_end_date=None,
            period_type=None,
        )

        result = reconcile_earnings_candidate(
            earnings_event=candidate,
            sync_run=make_sync_run("incomplete"),
        )

        assert result.outcome is EarningsReconciliationOutcome.REVIEW_REQUIRED
        assert "IDENTITY_INCOMPLETE" in result.conflict_codes
        candidate.refresh_from_db()
        assert candidate.identity_status == IdentityStatus.CANDIDATE

    def test_same_source_external_id_mapped_to_different_identity_requires_review(self) -> None:
        source = make_calendar_source("external-conflict")
        provider_event_id = "shared-provider-event"
        first, _first_observation, _ = _candidate(
            "external-first",
            source=source,
            provider_event_id=provider_event_id,
        )
        _second, _second_observation, _ = _candidate(
            "external-second",
            company=make_company("external-other-company"),
            source=source,
            provider_event_id=provider_event_id,
        )

        result = reconcile_earnings_candidate(
            earnings_event=first,
            sync_run=make_sync_run("external-conflict"),
        )

        assert result.outcome is EarningsReconciliationOutcome.REVIEW_REQUIRED
        assert "SOURCE_MAPPING_CONFLICT" in result.conflict_codes


@pytest.mark.django_db
class TestManualReconciliationAuthority:
    def test_manual_decision_is_append_only_idempotent_and_blocks_automation(self) -> None:
        candidate, observation, lineage = _candidate(
            "manual-block",
            period_end_date=None,
            period_type=None,
        )
        open_result = reconcile_earnings_candidate(
            earnings_event=candidate,
            sync_run=make_sync_run("manual-open"),
        )
        actor = make_user("manual-block")

        first = resolve_earnings_reconciliation_manually(
            observation=observation,
            decision_type="no_match",
            actor_user=actor,
            reason="The provider row is not this earnings event.",
            request_id="manual-block-request",
        )
        second = resolve_earnings_reconciliation_manually(
            observation=observation,
            decision_type="no_match",
            actor_user=actor,
            reason="The provider row is not this earnings event.",
            request_id="manual-block-request",
        )
        automated = reconcile_earnings_candidate(
            earnings_event=candidate,
            sync_run=make_sync_run("manual-block-auto"),
        )

        assert first.decision_created is True
        assert second.decision_created is False
        assert second.decision.pk == first.decision.pk
        assert first.decision.supersedes_id == open_result.decision.pk
        assert EarningsReconciliationDecision.objects.filter(pk=lineage.pk).exists()
        assert EarningsReconciliationDecision.objects.filter(pk=open_result.decision.pk).exists()
        assert automated.blocked_by_manual_authority is True
        assert automated.decision.pk == first.decision.pk

    def test_new_manual_request_supersedes_previous_leaf(self) -> None:
        candidate, observation, _ = _candidate("manual-supersede")
        actor = make_user("manual-supersede")
        first = resolve_earnings_reconciliation_manually(
            observation=observation,
            decision_type="no_match",
            actor_user=actor,
            reason="Keep separate for the first review.",
            request_id="manual-supersede-1",
        )
        second = resolve_earnings_reconciliation_manually(
            observation=observation,
            decision_type="matched_candidate",
            target_event=candidate,
            actor_user=actor,
            reason="A second review selects the existing candidate.",
            request_id="manual-supersede-2",
        )

        assert second.decision.supersedes_id == first.decision.pk
        assert first.decision.pk != second.decision.pk
        assert EarningsReconciliationDecision.objects.filter(pk=first.decision.pk).exists()
        assert AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.EARNINGS_RECONCILIATION_DECISION,
            target_id=second.decision.pk,
            actor_user=actor,
        ).exists()

    def test_manual_schedule_authority_uses_schedule_service(self) -> None:
        candidate, observation, _ = _candidate(
            "manual-schedule",
            estimated_release_date=None,
            release_session="unknown",
        )
        actor = make_user("manual-schedule")

        result = resolve_earnings_reconciliation_manually(
            observation=observation,
            decision_type="matched_candidate",
            target_event=candidate,
            actor_user=actor,
            reason="Reviewed against the synthetic source.",
            request_id="manual-schedule-1",
            covered_fields=("estimated_release", "release_session"),
            schedule_changes={
                "estimated_release": date(2026, 4, 25),
                "release_session": "pre_market",
            },
        )

        candidate.refresh_from_db()
        assert candidate.estimated_release_date == date(2026, 4, 25)
        assert candidate.release_session == "pre_market"
        assert result.decision.covered_fields == ["estimated_release", "release_session"]
        assert result.decision.match_factors["manual_resolution"]["schedule_changes"] == {
            "estimated_release": "2026-04-25",
            "release_session": "pre_market",
        }

    def test_manual_authority_can_promote_eligible_winner(self) -> None:
        candidate, observation, _ = _candidate("manual-promotion")
        actor = make_user("manual-promotion")

        result = resolve_earnings_reconciliation_manually(
            observation=observation,
            decision_type="matched_canonical",
            target_event=candidate,
            actor_user=actor,
            reason="Reviewed exact identity and authorized promotion.",
            request_id="manual-promotion-request",
            promote=True,
        )

        candidate.refresh_from_db()
        assert result.promoted is True
        assert candidate.identity_status == IdentityStatus.CANONICAL
        assert result.decision.target_event_id == candidate.pk
        assert result.decision.match_factors["earnings_reconciliation"][
            "canonical_target_id"
        ] == str(candidate.pk)

    def test_manual_promotion_collision_replays_same_request(self) -> None:
        candidate, observation, _ = _candidate("manual-collision")
        canonical = make_event(
            company=candidate.company,
            period_end_date=candidate.period_end_date,
            period_type=candidate.period_type,
        )
        actor = make_user("manual-collision")

        first = resolve_earnings_reconciliation_manually(
            observation=observation,
            decision_type="matched_canonical",
            target_event=candidate,
            actor_user=actor,
            reason="Attempt the reviewed promotion and retain its collision.",
            request_id="manual-collision-request",
            promote=True,
        )
        second = resolve_earnings_reconciliation_manually(
            observation=observation,
            decision_type="matched_canonical",
            target_event=candidate,
            actor_user=actor,
            reason="Attempt the reviewed promotion and retain its collision.",
            request_id="manual-collision-request",
            promote=True,
        )

        assert first.decision_created is True
        assert second.decision_created is False
        assert second.decision.pk == first.decision.pk
        assert first.decision.decision_type == "collision"
        assert first.decision.status == "open"
        assert first.decision.target_event_id is None
        assert first.decision.match_factors["earnings_reconciliation"][
            "canonical_collision_event_id"
        ] == str(canonical.pk)
        assert (
            AuditRecord.objects.filter(
                target_type=AuditRecord.TargetType.EARNINGS_RECONCILIATION_DECISION,
                target_id=first.decision.pk,
            ).count()
            == 1
        )


@pytest.mark.django_db(transaction=True)
def test_conflicting_concurrent_manual_decisions_form_one_serial_chain() -> None:
    candidate, observation, lineage = _candidate("manual-concurrent")
    actor = make_user("manual-concurrent")
    barrier = threading.Barrier(2, timeout=10)
    results: list[object] = []
    errors: list[BaseException] = []

    def worker(*, decision_type: str, request_id: str, bind_candidate: bool) -> None:
        close_old_connections()
        try:
            barrier.wait()
            results.append(
                resolve_earnings_reconciliation_manually(
                    observation=observation,
                    decision_type=decision_type,
                    target_event=candidate if bind_candidate else None,
                    actor_user=actor,
                    reason=f"Conflicting concurrent review {request_id}.",
                    request_id=request_id,
                )
            )
        except BaseException as error:
            errors.append(error)
        finally:
            for current_connection in connections.all():
                current_connection.close()

    threads = [
        threading.Thread(
            target=worker,
            kwargs={
                "decision_type": "no_match",
                "request_id": "manual-concurrent-no-match",
                "bind_candidate": False,
            },
        ),
        threading.Thread(
            target=worker,
            kwargs={
                "decision_type": "matched_candidate",
                "request_id": "manual-concurrent-match",
                "bind_candidate": True,
            },
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    for thread in threads:
        assert not thread.is_alive(), "Concurrent manual reconciliation thread hung"
    assert errors == []
    assert len(results) == 2

    manual_decisions = list(
        EarningsReconciliationDecision.objects.filter(
            observation=observation,
            actor_user=actor,
        ).order_by("id")
    )
    assert len(manual_decisions) == 2
    manual_ids = {decision.pk for decision in manual_decisions}
    first_in_chain = next(
        decision for decision in manual_decisions if decision.supersedes_id == lineage.pk
    )
    final_leaf = next(
        decision for decision in manual_decisions if decision.supersedes_id == first_in_chain.pk
    )
    superseded_ids = {
        decision.supersedes_id
        for decision in EarningsReconciliationDecision.objects.filter(observation=observation)
        if decision.supersedes_id is not None
    }
    leaf_ids = {
        decision.pk
        for decision in EarningsReconciliationDecision.objects.filter(observation=observation)
        if decision.pk not in superseded_ids
    }
    assert final_leaf.pk in manual_ids
    assert leaf_ids == {final_leaf.pk}
