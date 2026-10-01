# mypy: ignore-errors
"""Deterministic periodic/release matching, classification and replay tests."""

from __future__ import annotations

import http.client
import socket
from datetime import UTC, date, datetime, timedelta

import pytest

from audit.models import AuditRecord, DataChange
from earnings.models import (
    EarningsEvent,
    FilingEarningsDecision,
    FilingEarningsLink,
)
from earnings.services.filing_links import (
    CLASSIFICATION_RULE_VERSION,
    MATCH_RULE_VERSION,
    FilingEarningsIntegrityError,
    InvalidFilingEarningsInput,
    evaluate_filing_earnings_link,
)
from tests.earnings.filing_helpers import (
    add_raw_observation_for_run,
    make_filing_with_evidence,
    make_sec_sync_run,
)
from tests.earnings.helpers import make_company, make_event

pytestmark = pytest.mark.django_db


def _run(filing: object) -> object:
    evidence = filing.source_evidence
    assert evidence is not None
    return evaluate_filing_earnings_link(filing=filing, sync_run=evidence.sync_run)


def _decision_reason(result: object) -> str:
    assert result.decision is not None
    return result.decision.match_factors["filing_earnings"]["reason_code"]


def _details(result: object) -> dict[str, object]:
    assert result.decision is not None
    return result.decision.match_factors["filing_earnings"]["details"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("period_type", "period_end"),
    (("Q1", date(2026, 3, 31)), ("Q2", date(2026, 6, 30)), ("Q3", date(2026, 9, 30))),
)
def test_10q_matches_quarter_period(period_type: str, period_end: date) -> None:
    company = make_company("10q")
    event = make_event(company=company, period_end_date=period_end, period_type=period_type)
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=period_end,
    )

    result = _run(filing)

    assert result.outcome == "matched_periodic_filing"
    assert result.link is not None
    assert result.link.relation_type == "PERIODIC_FILING"
    assert result.link.confidence == "EXACT"
    assert result.link.release_filing_classification is None
    assert result.link.review_status == "auto"
    assert result.decision.decision_type == "matched_periodic_filing"
    assert result.decision.status == "resolved"
    assert result.decision.target_event_id == event.pk
    assert result.decision.classification is None
    assert result.decision.match_rule_version == MATCH_RULE_VERSION


def test_10q_wrong_period_does_not_match() -> None:
    company = make_company("wrong-period")
    make_event(company=company, period_end_date=date(2026, 3, 31), period_type="Q1")
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 6, 30),
    )

    result = _run(filing)

    assert result.outcome == "no_match"
    assert result.link is None
    assert result.decision.status == "rejected"
    assert _decision_reason(result) == "NO_MATCHING_PERIODIC_EVENT"


@pytest.mark.django_db
@pytest.mark.parametrize("form_type", ("10-K", "20-F", "40-F"))
def test_annual_forms_match_fy_period(form_type: str) -> None:
    company = make_company("annual")
    event = make_event(
        company=company,
        period_end_date=date(2026, 12, 31),
        period_type="FY",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type=form_type,
        period_of_report=date(2026, 12, 31),
    )

    result = _run(filing)

    assert result.outcome == "matched_periodic_filing"
    assert result.link is not None
    assert result.link.earnings_event_id == event.pk
    assert result.link.confidence == "EXACT"


def test_annual_form_does_not_match_non_fy_event() -> None:
    company = make_company("annual-mismatch")
    make_event(company=company, period_end_date=date(2026, 12, 31), period_type="Q3")
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-K",
        period_of_report=date(2026, 12, 31),
    )

    result = _run(filing)

    assert result.outcome == "no_match"
    assert result.link is None


def test_annual_form_candidate_only_requires_review() -> None:
    company = make_company("annual-candidate")
    candidate = make_event(
        company=company,
        period_end_date=date(2026, 12, 31),
        period_type="FY",
        identity_status="candidate",
        identity_key=None,
        identity_rule_version=None,
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-K",
        period_of_report=date(2026, 12, 31),
    )

    result = _run(filing)

    assert result.outcome == "review_required"
    assert result.link is None
    assert _decision_reason(result) == "CANDIDATE_ONLY_EVENT"
    candidate.refresh_from_db()
    assert candidate.identity_status == "candidate"
    assert candidate.identity_key is None


def test_6k_never_creates_periodic_link() -> None:
    company = make_company("6k-periodic")
    make_event(company=company, period_end_date=date(2026, 3, 31), period_type="Q1")
    filing = make_filing_with_evidence(
        company=company,
        form_type="6-K",
        period_of_report=date(2026, 3, 31),
    )

    result = _run(filing)

    assert result.outcome == "no_match"
    assert result.link is None
    assert result.decision.relation_type == "RELEASE_FILING"
    assert _decision_reason(result) == "RELEASE_FACT_MISSING"


def test_periodic_zero_candidate_no_match() -> None:
    company = make_company("periodic-zero")
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 3, 31),
    )

    result = _run(filing)

    assert result.outcome == "no_match"
    assert result.decision.status == "rejected"
    assert result.link is None


def test_periodic_multiple_candidates_requires_review() -> None:
    company = make_company("periodic-multiple")
    make_event(company=company, period_end_date=date(2026, 3, 31), period_type="Q1")
    make_event(company=company, period_end_date=date(2026, 3, 31), period_type="Q2")
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 3, 31),
    )

    result = _run(filing)

    assert result.outcome == "review_required"
    assert result.link is None
    assert result.decision.status == "open"
    assert _decision_reason(result) == "MULTIPLE_CANONICAL_EVENTS"


def test_periodic_cancelled_canonical_requires_review() -> None:
    company = make_company("periodic-cancelled")
    event = make_event(
        company=company,
        period_end_date=date(2026, 3, 31),
        period_type="Q1",
        status="cancelled",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 3, 31),
    )

    result = _run(filing)

    assert result.outcome == "review_required"
    assert result.link is None
    event.refresh_from_db()
    assert event.status == "cancelled"


@pytest.mark.django_db
@pytest.mark.parametrize("offset", (-1, 0, 1))
def test_release_matches_bounded_window(offset: int) -> None:
    company = make_company("release-window")
    reference = date(2026, 6, 1) + timedelta(days=offset)
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        estimated_release_date=reference,
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        reported_items="2.02",
        document_types=("EX-99.1",),
    )

    result = _run(filing)

    assert result.outcome == "matched_release_filing"
    assert result.link is not None
    assert result.link.confidence == "BOUNDED_WINDOW"
    assert result.link.release_filing_classification == "YES"
    assert result.link.classification_reason == "ITEM_202_WITH_EARNINGS_EXHIBIT"
    assert result.link.classification_rule_version == CLASSIFICATION_RULE_VERSION


@pytest.mark.django_db
@pytest.mark.parametrize("offset", (-2, 2))
def test_release_outside_window_no_match(offset: int) -> None:
    company = make_company("release-outside")
    reference = date(2026, 6, 1) + timedelta(days=offset)
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        estimated_release_date=reference,
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    result = _run(filing)

    assert result.outcome == "no_match"
    assert result.link is None
    assert _decision_reason(result) == "NO_MATCHING_RELEASE_WINDOW"


def test_release_exact_datetime_converts_to_eastern_date() -> None:
    company = make_company("utc-boundary")
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        earnings_release_at=datetime(2026, 6, 1, 2, 0, tzinfo=UTC),
        earnings_release_precision="exact_datetime",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    result = _run(filing)

    assert result.outcome == "matched_release_filing"
    assert _details(result)["matched_reference_date"] == "2026-05-31"


def test_release_window_covers_dst_transition() -> None:
    company = make_company("dst")
    make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        earnings_release_at=datetime(2026, 11, 2, 5, 30, tzinfo=UTC),
        earnings_release_precision="exact_datetime",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
    )

    result = _run(filing)

    assert result.outcome == "matched_release_filing"
    details = _details(result)
    assert details["filing_accepted_date_et"] == "2026-11-01"
    assert details["window_end_et"] == "2026-11-02"
    assert details["matched_reference_date"] == "2026-11-02"


@pytest.mark.django_db
def test_release_reference_fact_precedence() -> None:
    company = make_company("precedence")
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        earnings_release_date=date(2026, 6, 1),
        earnings_release_precision="date_only",
        confirmed_release_date=date(2026, 8, 1),
        confirmed_release_precision="date_only",
        estimated_release_date=date(2026, 9, 1),
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    result = _run(filing)

    assert result.outcome == "matched_release_filing"
    assert _details(result)["matched_reference_field"] == "earnings_release"


def test_release_reference_falls_back_to_confirmed_then_estimated() -> None:
    company = make_company("fallback")
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        confirmed_release_date=date(2026, 6, 1),
        confirmed_release_precision="date_only",
        estimated_release_date=date(2026, 9, 1),
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    confirmed = _run(filing)
    assert confirmed.outcome == "matched_release_filing"
    assert _details(confirmed)["matched_reference_field"] == "confirmed_release"

    second_company = make_company("fallback-estimated")
    make_event(
        company=second_company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        estimated_release_date=date(2026, 6, 1),
        estimated_release_precision="date_only",
    )
    second_filing = make_filing_with_evidence(
        company=second_company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    estimated = _run(second_filing)
    assert estimated.outcome == "matched_release_filing"
    assert _details(estimated)["matched_reference_field"] == "estimated_release"


def test_release_fact_missing() -> None:
    company = make_company("no-fact")
    make_event(company=company, period_end_date=date(2026, 6, 30), period_type="Q2")
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    result = _run(filing)

    assert result.outcome == "no_match"
    assert _decision_reason(result) == "RELEASE_FACT_MISSING"


def test_release_multiple_candidates_requires_review() -> None:
    company = make_company("release-multiple")
    for period_type in ("Q1", "Q2"):
        make_event(
            company=company,
            period_end_date=date(2026, 3, 31) if period_type == "Q1" else date(2026, 6, 30),
            period_type=period_type,
            estimated_release_date=date(2026, 6, 1),
            estimated_release_precision="date_only",
        )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    result = _run(filing)

    assert result.outcome == "review_required"
    assert result.link is None
    assert _decision_reason(result) == "MULTIPLE_CANONICAL_EVENTS"


def test_8k_period_of_report_is_not_fiscal_identity() -> None:
    company = make_company("8k-period")
    make_event(
        company=company,
        period_end_date=date(2026, 12, 31),
        period_type="FY",
        estimated_release_date=date(2026, 6, 1),
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        period_of_report=date(2026, 3, 31),
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    result = _run(filing)

    assert result.outcome == "matched_release_filing"
    assert result.link.relation_type == "RELEASE_FILING"
    assert _details(result)["strategy"] == "release_bounded_window"


def test_6k_bounded_match_is_always_review_required() -> None:
    company = make_company("6k")
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        estimated_release_date=date(2026, 6, 1),
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="6-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    result = _run(filing)

    assert result.outcome == "matched_release_filing"
    assert result.link is not None
    assert result.link.release_filing_classification == "REVIEW_REQUIRED"
    assert result.link.classification_reason == "SIX_K_REQUIRES_REVIEW"
    assert result.link.confidence == "BOUNDED_WINDOW"


def test_8k_with_missing_items_creates_review_required_link() -> None:
    company = make_company("missing-items")
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        estimated_release_date=date(2026, 6, 1),
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        reported_items="",
        document_types=("EX-99.1",),
    )

    result = _run(filing)

    assert result.outcome == "matched_release_filing"
    assert result.link.release_filing_classification == "REVIEW_REQUIRED"
    assert result.link.classification_reason == "ITEMS_METADATA_MISSING"


def test_8k_without_item_202_classifies_no() -> None:
    company = make_company("no-202")
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        estimated_release_date=date(2026, 6, 1),
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        reported_items="9.01",
    )

    result = _run(filing)

    assert result.link.release_filing_classification == "NO"
    assert result.link.classification_reason == "NO_ITEM_202"


def test_cancelled_release_candidate_requires_review() -> None:
    company = make_company("release-cancelled")
    event = make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        status="cancelled",
        estimated_release_date=date(2026, 6, 1),
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    result = _run(filing)

    assert result.outcome == "review_required"
    assert result.link is None
    event.refresh_from_db()
    assert event.status == "cancelled"


def test_candidate_only_release_requires_review() -> None:
    company = make_company("release-candidate")
    candidate = make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        identity_status="candidate",
        identity_key=None,
        identity_rule_version=None,
        estimated_release_date=date(2026, 6, 1),
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    result = _run(filing)

    assert result.outcome == "review_required"
    assert result.link is None
    candidate.refresh_from_db()
    assert candidate.identity_status == "candidate"


@pytest.mark.django_db
def test_matching_never_uses_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_connection(*args: object, **kwargs: object) -> object:
        raise AssertionError("matching must not open network connections")

    monkeypatch.setattr(socket, "create_connection", fail_connection)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", fail_connection)
    company = make_company("offline")
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        estimated_release_date=date(2026, 6, 1),
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        reported_items="2.02",
        document_types=("EX-99.1",),
    )

    result = _run(filing)

    assert result.link.release_filing_classification == "YES"


@pytest.mark.django_db
def test_same_input_twice_reuses_decision_link_and_audit() -> None:
    company = make_company("replay")
    make_event(company=company, period_end_date=date(2026, 3, 31), period_type="Q1")
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 3, 31),
    )

    first = _run(filing)
    decision_count = FilingEarningsDecision.objects.count()
    audit_count = AuditRecord.objects.count()
    second = _run(filing)

    assert first.decision_created is True
    assert first.link_created is True
    assert second.decision_created is False
    assert second.link_created is False
    assert second.link_updated is False
    assert FilingEarningsLink.objects.count() == 1
    assert FilingEarningsDecision.objects.count() == decision_count
    assert AuditRecord.objects.count() == audit_count
    assert DataChange.objects.count() == 0


@pytest.mark.django_db
def test_replay_in_a_later_run_reuses_the_authenticated_decision() -> None:
    company = make_company("later-run")
    make_event(company=company, period_end_date=date(2026, 3, 31), period_type="Q1")
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 3, 31),
    )
    first = _run(filing)
    evidence = filing.source_evidence
    assert evidence is not None
    later_run = make_sec_sync_run(source=evidence.sync_run.source, suffix="later")
    add_raw_observation_for_run(filing=filing, sync_run=later_run)

    replay = evaluate_filing_earnings_link(filing=filing, sync_run=later_run)

    assert replay.decision_created is False
    assert replay.decision.pk == first.decision.pk
    assert FilingEarningsDecision.objects.count() == 1
    assert FilingEarningsLink.objects.count() == 1


@pytest.mark.django_db
def test_service_reloads_persisted_facts_and_ignores_memory_mutation() -> None:
    company = make_company("reload")
    make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        estimated_release_date=date(2026, 6, 1),
        estimated_release_precision="date_only",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        reported_items="2.02",
        document_types=("EX-99.1",),
    )
    filing.reported_items = "9.01"
    filing.form_type = "10-Q"

    result = _run(filing)

    assert result.link.release_filing_classification == "YES"


@pytest.mark.django_db
def test_missing_source_evidence_fails_closed() -> None:
    company = make_company("missing-evidence")
    filing = make_filing_with_evidence(company=company, form_type="10-Q")
    evidence = filing.source_evidence
    assert evidence is not None
    sync_run = evidence.sync_run
    filing.source_evidence = None
    filing.save(update_fields=("source_evidence", "updated_at"))

    with pytest.raises(FilingEarningsIntegrityError):
        evaluate_filing_earnings_link(filing=filing, sync_run=sync_run)


@pytest.mark.django_db
def test_run_from_another_source_fails_closed() -> None:
    company = make_company("wrong-source")
    filing = make_filing_with_evidence(company=company, form_type="10-Q")
    other_run = make_sec_sync_run(suffix="other-source")

    with pytest.raises(InvalidFilingEarningsInput):
        evaluate_filing_earnings_link(filing=filing, sync_run=other_run)


@pytest.mark.django_db
def test_run_that_never_observed_evidence_fails_closed() -> None:
    company = make_company("unobserved")
    filing = make_filing_with_evidence(company=company, form_type="10-Q")
    evidence = filing.source_evidence
    assert evidence is not None
    same_source_run = make_sec_sync_run(
        source=evidence.sync_run.source,
        suffix="unobserved",
    )

    with pytest.raises(InvalidFilingEarningsInput):
        evaluate_filing_earnings_link(filing=filing, sync_run=same_source_run)


@pytest.mark.django_db
def test_event_status_is_never_modified_by_matching() -> None:
    company = make_company("status-independent")
    event = make_event(
        company=company,
        period_end_date=date(2026, 3, 31),
        period_type="Q1",
        status="scheduled_confirmed",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 3, 31),
    )

    _run(filing)

    event.refresh_from_db()
    assert event.status == "scheduled_confirmed"
    assert EarningsEvent.objects.count() == 1
