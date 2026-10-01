# mypy: ignore-errors
"""IR authority / confirmation / release / cancellation / conflict tests (4.5B)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from audit.models import AuditRecord, DataChange, SourceEvidence, SyncRun
from earnings.models import (
    EarningsEvent,
    InvestorRelationsDecision,
    InvestorRelationsObservation,
)
from earnings.services import (
    InvalidInvestorRelationsDecision,
    evaluate_investor_relations_observation,
    record_investor_relations_decision,
    resolve_investor_relations_observation_manually,
    update_earnings_schedule,
)
from tests.earnings.filing_helpers import make_filing_with_evidence
from tests.earnings.helpers import (
    make_event,
    make_reconciliation_decision,
    make_sync_run,
    make_user,
)
from tests.earnings.ir_helpers import (
    ingest_ir,
    ir_item,
    ir_payload,
    make_ir_company,
    make_ir_source,
    make_ir_sync_run,
)

pytestmark = pytest.mark.django_db


def _context(suffix: str, **event_overrides: object) -> tuple[object, object, EarningsEvent]:
    company = make_ir_company(suffix)
    source = make_ir_source(suffix)
    event = make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        **event_overrides,
    )
    return company, source, event


def _evaluate(
    *,
    company: object,
    source: object,
    item: dict[str, object],
    run: SyncRun | None = None,
) -> tuple[object, InvestorRelationsObservation, SyncRun]:
    current_run = (
        run
        or make_ir_sync_run(
            company=company,  # type: ignore[arg-type]
            source=source,  # type: ignore[arg-type]
            request_id=f"ir-{uuid.uuid4().hex[:8]}",
        )
    )
    ingestion = ingest_ir(
        sync_run=current_run,
        payload=ir_payload([item]),
        company=company,  # type: ignore[arg-type]
    )
    assert len(ingestion.observations) == 1
    observation = ingestion.observations[0]
    evaluation = evaluate_investor_relations_observation(
        observation=observation,
        sync_run=current_run,
    )
    return evaluation, observation, current_run


def test_official_confirmation_writes_confirmed_release_and_status() -> None:
    company, source, event = _context("confirm")

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )

    event.refresh_from_db()
    assert event.status == "scheduled_confirmed"
    assert event.confirmed_release_date == date(2026, 10, 20)
    assert event.confirmed_release_at is None
    assert event.confirmed_release_precision == "date_only"
    assert evaluation.decision.decision_type == "confirmed_schedule"
    assert evaluation.decision.status == "resolved"
    assert (
        DataChange.objects.filter(
            target_type="earnings_event",
            target_id=event.pk,
            field_name="confirmed_release",
        ).count()
        == 1
    )
    assert (
        DataChange.objects.filter(
            target_type="earnings_event",
            target_id=event.pk,
            field_name="status",
        ).count()
        == 1
    )
    assert SourceEvidence.objects.filter(
        target_type="earnings_event",
        target_id=event.pk,
        field_name="confirmed_release",
    ).exists()
    assert (
        AuditRecord.objects.filter(
            target_type="earnings_event",
            target_id=event.pk,
        ).count()
        >= 2
    )


def test_exact_datetime_confirmation_stays_timezone_aware() -> None:
    company, source, event = _context("datetime")

    _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            confirmed_release={
                "value": "2026-10-20T20:30:00Z",
                "precision": "exact_datetime",
            },
        ),
    )

    event.refresh_from_db()
    assert event.confirmed_release_date is None
    assert event.confirmed_release_at == datetime(2026, 10, 20, 20, 30, tzinfo=UTC)
    assert event.confirmed_release_precision == "exact_datetime"


def test_conference_call_only_does_not_confirm_release() -> None:
    company, source, event = _context("call-only")

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(company, item_type="call_notice", conference_call="2026-10-20"),
    )

    event.refresh_from_db()
    assert event.status == "scheduled_estimated"
    assert event.confirmed_release_precision == "unknown"
    assert event.confirmed_release_date is None
    assert event.conference_call_date == date(2026, 10, 20)
    assert evaluation.decision.decision_type == "updated_conference_call"


def test_call_notice_with_explicit_release_date_confirms() -> None:
    company, source, event = _context("call-confirm")

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            item_type="call_notice",
            conference_call="2026-10-20",
            confirmed_release="2026-10-20",
        ),
    )

    event.refresh_from_db()
    assert event.status == "scheduled_confirmed"
    assert event.confirmed_release_date == date(2026, 10, 20)
    assert event.conference_call_date == date(2026, 10, 20)
    assert evaluation.decision.decision_type == "confirmed_schedule"
    assert SourceEvidence.objects.filter(
        target_type="earnings_event",
        target_id=event.pk,
        field_name="confirmed_release",
    ).exists()
    assert SourceEvidence.objects.filter(
        target_type="earnings_event",
        target_id=event.pk,
        field_name="conference_call",
    ).exists()


def test_explicit_results_release_marks_released() -> None:
    company, source, event = _context("released", status="scheduled_confirmed")

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            item_type="results_release",
            earnings_release={"value": "2026-10-20", "precision": "date_only"},
        ),
    )

    event.refresh_from_db()
    assert event.status == "released"
    assert event.earnings_release_date == date(2026, 10, 20)
    assert evaluation.decision.decision_type == "released"


def test_sec_filing_presence_does_not_release_event() -> None:
    company, source, event = _context("sec-boundary", status="scheduled_confirmed")
    make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 10, 20, 20, 30, tzinfo=UTC),
        reported_items="2.02",
        document_types=("EX-99.1",),
    )

    _evaluate(
        company=company,
        source=source,
        item=ir_item(company, item_type="call_notice", conference_call="2026-10-20"),
    )

    event.refresh_from_db()
    assert event.status == "scheduled_confirmed"
    assert event.earnings_release_precision == "unknown"


def test_explicit_official_cancellation_cancels_event() -> None:
    company, source, event = _context("cancel", status="scheduled_confirmed")

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            item_type="cancellation",
            cancellation={"scope": "event", "reason_code": "COMPANY_CANCELLED"},
        ),
    )

    event.refresh_from_db()
    assert event.status == "cancelled"
    assert evaluation.decision.decision_type == "cancelled"
    assert evaluation.decision.status == "resolved"


def test_conference_call_cancellation_does_not_cancel_event() -> None:
    company, source, event = _context("call-cancel", status="scheduled_confirmed")

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            item_type="cancellation",
            cancellation={"scope": "conference_call", "reason_code": "CALL_CANCELLED"},
        ),
    )

    event.refresh_from_db()
    assert event.status == "scheduled_confirmed"
    assert evaluation.decision.decision_type == "ignored"
    assert evaluation.decision.status == "rejected"
    assert evaluation.decision.target_event_id == event.pk


def test_source_absence_does_not_cancel_or_downgrade() -> None:
    company, source, event = _context("absence")
    _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )
    event.refresh_from_db()
    decision_count = InvestorRelationsDecision.objects.count()

    run = make_ir_sync_run(
        company=company,
        source=source,
        request_id=f"ir-{uuid.uuid4().hex[:8]}",
    )
    ingestion = ingest_ir(sync_run=run, payload=ir_payload([]), company=company)

    assert ingestion.observations == ()
    event.refresh_from_db()
    assert event.status == "scheduled_confirmed"
    assert event.confirmed_release_date == date(2026, 10, 20)
    assert InvestorRelationsDecision.objects.count() == decision_count


def test_same_authority_ir_conflict_requires_review_without_overwrite() -> None:
    company, source, event = _context("conflict")
    _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            source_event_identity="source-a",
            confirmed_release="2026-10-20",
        ),
    )
    event.refresh_from_db()

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            source_event_identity="source-b",
            confirmed_release="2026-10-21",
        ),
    )

    event.refresh_from_db()
    assert evaluation.decision.decision_type == "conflict"
    assert evaluation.decision.status == "open"
    assert evaluation.decision.reason == "IR_SAME_AUTHORITY_CONFLICT"
    assert event.confirmed_release_date == date(2026, 10, 20)
    assert (
        DataChange.objects.filter(
            target_type="earnings_event",
            target_id=event.pk,
            field_name="confirmed_release",
        ).count()
        == 1
    )
    assert InvestorRelationsObservation.objects.count() == 2


def test_manual_ir_decision_blocks_automatic_confirmation() -> None:
    company, source, event = _context("manual-block")
    run = make_ir_sync_run(
        company=company,
        source=source,
        request_id=f"ir-{uuid.uuid4().hex[:8]}",
    )
    ingestion = ingest_ir(
        sync_run=run,
        payload=ir_payload([ir_item(company, confirmed_release="2026-10-20")]),
        company=company,
    )
    observation = ingestion.observations[0]
    actor = make_user("ir-manual")
    record_investor_relations_decision(
        observation=observation,
        decision_type="confirmed_schedule",
        status="resolved",
        covered_fields=("confirmed_release",),
        match_factors={"manual": True},
        reason="Operator confirmed manually.",
        target_event=event,
        actor_user=actor,
        request_id="manual-block-1",
    )

    evaluation = evaluate_investor_relations_observation(
        observation=observation,
        sync_run=run,
    )

    event.refresh_from_db()
    assert evaluation.decision.decision_type == "ignored"
    assert evaluation.decision.reason == "MANUAL_AUTHORITY_PRESENT"
    assert evaluation.blocked_by_manual_authority is True
    assert event.status == "scheduled_estimated"
    assert event.confirmed_release_precision == "unknown"
    assert (
        DataChange.objects.filter(
            target_type="earnings_event",
            target_id=event.pk,
            field_name="confirmed_release",
        ).count()
        == 0
    )


def test_manual_reconciliation_authority_blocks_tentative_ir_estimate() -> None:
    company, source, event = _context("manual-estimate")
    actor = make_user("reconciliation-manual")
    make_reconciliation_decision(
        target_event=event,
        covered_fields=["estimated_release"],
        actor_user=actor,
        sync_run=None,
        reason="Manual estimate authority.",
        request_id="manual-estimate-1",
    )

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            estimated_release="2026-10-20",
        ),
    )

    event.refresh_from_db()
    assert evaluation.decision.decision_type == "ignored"
    assert evaluation.blocked_by_manual_authority is True
    assert event.estimated_release_precision == "unknown"


def test_precision_regression_is_blocked() -> None:
    company, source, event = _context("regression")
    _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            confirmed_release={
                "value": "2026-10-20T20:30:00Z",
                "precision": "exact_datetime",
            },
        ),
    )
    event.refresh_from_db()

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )

    event.refresh_from_db()
    assert evaluation.decision.decision_type == "conflict"
    assert evaluation.decision.reason == "PRECISION_REGRESSION_BLOCKED"
    assert event.confirmed_release_precision == "exact_datetime"
    assert event.confirmed_release_at == datetime(2026, 10, 20, 20, 30, tzinfo=UTC)


def test_precision_refinement_is_allowed() -> None:
    company, source, event = _context("refinement")
    _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )
    event.refresh_from_db()

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            confirmed_release={
                "value": "2026-10-20T20:30:00Z",
                "precision": "exact_datetime",
            },
        ),
    )

    event.refresh_from_db()
    assert evaluation.decision.decision_type == "confirmed_schedule"
    assert event.confirmed_release_precision == "exact_datetime"
    assert event.confirmed_release_at == datetime(2026, 10, 20, 20, 30, tzinfo=UTC)


def test_calendar_estimate_is_overridden_by_ir_confirmation() -> None:
    company, source, event = _context("override")
    calendar_run = make_sync_run("calendar-estimate")
    update_earnings_schedule(
        earnings_event=event,
        changes={"estimated_release": {"precision": "date_only", "value": date(2026, 10, 15)}},
        sync_run=calendar_run,
    )

    _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )

    event.refresh_from_db()
    assert event.estimated_release_date == date(2026, 10, 15)
    assert event.confirmed_release_date == date(2026, 10, 20)
    assert event.status == "scheduled_confirmed"


def test_candidate_only_event_goes_to_review_without_write() -> None:
    company = make_ir_company("candidate")
    source = make_ir_source("candidate")
    make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        identity_status="candidate",
        identity_key=None,
        identity_rule_version=None,
    )

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )

    assert evaluation.decision.decision_type == "conflict"
    assert evaluation.decision.status == "open"
    assert evaluation.decision.reason == "CANDIDATE_ONLY_EVENT"
    assert evaluation.event is None


def test_missing_canonical_event_is_no_match() -> None:
    company = make_ir_company("no-match")
    source = make_ir_source("no-match")

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )

    assert evaluation.decision.decision_type == "no_match"
    assert evaluation.decision.status == "rejected"
    assert evaluation.decision.reason == "CANONICAL_EVENT_NOT_FOUND"
    assert evaluation.decision.target_event_id is None


def test_repeated_evaluation_reuses_decision_and_history() -> None:
    company, source, event = _context("replay-idempotent")
    evaluation, observation, run = _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )

    second = evaluate_investor_relations_observation(observation=observation, sync_run=run)

    assert second.decision.pk == evaluation.decision.pk
    assert second.decision_created is False
    assert InvestorRelationsDecision.objects.count() == 1
    assert (
        DataChange.objects.filter(
            target_type="earnings_event",
            target_id=event.pk,
            field_name="confirmed_release",
        ).count()
        == 1
    )


def test_manual_resolution_applies_with_actor_authority() -> None:
    company, source, event = _context("manual-apply")
    run = make_ir_sync_run(
        company=company,
        source=source,
        request_id=f"ir-{uuid.uuid4().hex[:8]}",
    )
    ingestion = ingest_ir(
        sync_run=run,
        payload=ir_payload([ir_item(company, confirmed_release="2026-10-20")]),
        company=company,
    )
    observation = ingestion.observations[0]
    actor = make_user("ir-operator")

    evaluation = resolve_investor_relations_observation_manually(
        observation=observation,
        actor_user=actor,
        reason="Operator decision.",
        request_id="manual-apply-1",
        target_event=event,
    )

    event.refresh_from_db()
    assert event.status == "scheduled_confirmed"
    assert event.confirmed_release_date == date(2026, 10, 20)
    assert evaluation.decision.actor_user_id == actor.pk
    assert evaluation.decision.request_id == "manual-apply-1"
    assert AuditRecord.objects.filter(
        target_type="investor_relations_decision",
        target_id=evaluation.decision.pk,
        actor_user=actor,
    ).exists()


def test_later_automatic_observation_is_blocked_by_manual_leaf() -> None:
    company, source, event = _context("manual-leaf")
    run = make_ir_sync_run(
        company=company,
        source=source,
        request_id=f"ir-{uuid.uuid4().hex[:8]}",
    )
    first = ingest_ir(
        sync_run=run,
        payload=ir_payload(
            [ir_item(company, source_event_identity="a", confirmed_release="2026-10-20")]
        ),
        company=company,
    ).observations[0]
    actor = make_user("manual-leaf")
    resolve_investor_relations_observation_manually(
        observation=first,
        actor_user=actor,
        reason="Manual decision.",
        request_id="manual-leaf-1",
        target_event=event,
    )

    second = ingest_ir(
        sync_run=run,
        payload=ir_payload(
            [ir_item(company, source_event_identity="b", confirmed_release="2026-10-21")]
        ),
        company=company,
    ).observations[0]
    evaluation = evaluate_investor_relations_observation(observation=second, sync_run=run)

    event.refresh_from_db()
    assert evaluation.decision.decision_type == "ignored"
    assert evaluation.blocked_by_manual_authority is True
    assert event.confirmed_release_date == date(2026, 10, 20)


def test_automatic_decision_audit_targets_decision_with_sync_run() -> None:
    company, source, event = _context("decision-audit")

    evaluation, _observation, run = _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )

    audit = AuditRecord.objects.get(
        target_type="investor_relations_decision",
        target_id=evaluation.decision.pk,
    )
    assert audit.sync_run_id == run.pk
    assert audit.actor_user_id is None
    assert audit.after["decision_type"] == "confirmed_schedule"
    assert event.pk == evaluation.decision.target_event_id


def test_decision_primitive_rejects_invalid_provenance_and_context() -> None:
    company, source, event = _context("provenance")
    run = make_ir_sync_run(
        company=company,
        source=source,
        request_id=f"ir-{uuid.uuid4().hex[:8]}",
    )
    observation = ingest_ir(
        sync_run=run,
        payload=ir_payload([ir_item(company, confirmed_release="2026-10-20")]),
        company=company,
    ).observations[0]

    with pytest.raises(InvalidInvestorRelationsDecision):
        record_investor_relations_decision(
            observation=observation,
            decision_type="confirmed_schedule",
            status="resolved",
            target_event=event,
            sync_run=None,
        )

    other_company = make_ir_company("provenance-other")
    other_source = make_ir_source("provenance-other")
    make_event(
        company=other_company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
    )
    other_run = make_ir_sync_run(
        company=other_company,
        source=other_source,
        request_id=f"ir-{uuid.uuid4().hex[:8]}",
    )
    other_observation = ingest_ir(
        sync_run=other_run,
        payload=ir_payload([ir_item(other_company, confirmed_release="2026-10-20")]),
        company=other_company,
    ).observations[0]

    with pytest.raises(InvalidInvestorRelationsDecision):
        record_investor_relations_decision(
            observation=observation,
            decision_type="conflict",
            status="open",
            reason="FIXTURE",
            match_factors={},
            source_raw_data_record=other_observation.raw_data_record,
            sync_run=run,
        )


def test_past_calendar_date_and_call_start_do_not_release_event() -> None:
    company, source, event = _context("no-auto-release")
    calendar_run = make_sync_run("past-calendar")
    update_earnings_schedule(
        earnings_event=event,
        changes={"estimated_release": {"precision": "date_only", "value": date(2020, 1, 1)}},
        sync_run=calendar_run,
    )
    _evaluate(
        company=company,
        source=source,
        item=ir_item(
            company,
            item_type="call_notice",
            conference_call="2020-01-01",
        ),
    )

    event.refresh_from_db()
    assert event.status == "scheduled_estimated"
    assert event.earnings_release_precision == "unknown"


def test_rule_version_upgrade_appends_decision_without_rewriting_history() -> None:
    company, source, _event = _context("rule-upgrade")
    evaluation, observation, run = _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )

    upgraded = record_investor_relations_decision(
        observation=observation,
        decision_type="conflict",
        status="open",
        rule_version="ir-confirmation-decision-v2",
        match_factors={"rule_upgrade": True},
        reason="RULE_UPGRADE",
        sync_run=run,
    )

    assert upgraded.created is True
    assert upgraded.decision.supersedes_id == evaluation.decision.pk
    assert InvestorRelationsDecision.objects.count() == 2
    original = InvestorRelationsDecision.objects.get(pk=evaluation.decision.pk)
    assert original.decision_type == "confirmed_schedule"


def test_manual_schedule_change_blocks_automatic_ir_confirmation() -> None:
    company, source, event = _context("manual-schedule")
    actor = make_user("manual-schedule")
    update_earnings_schedule(
        earnings_event=event,
        changes={
            "confirmed_release": {
                "precision": "date_only",
                "value": date(2026, 10, 19),
            }
        },
        actor_user=actor,
        reason="Operator confirmed manually.",
        request_id="manual-schedule-1",
    )

    evaluation, _observation, _run = _evaluate(
        company=company,
        source=source,
        item=ir_item(company, confirmed_release="2026-10-20"),
    )

    event.refresh_from_db()
    assert evaluation.decision.decision_type == "ignored"
    assert evaluation.blocked_by_manual_authority is True
    assert event.confirmed_release_date == date(2026, 10, 19)
