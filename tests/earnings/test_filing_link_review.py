# mypy: ignore-errors
"""Manual authority, rule-version upgrade and concurrency tests for 4.5A."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from threading import Barrier

import pytest
from django.db import close_old_connections, connections

from audit.models import AuditRecord, DataChange
from earnings.models import FilingEarningsDecision, FilingEarningsLink
from earnings.services import filing_links
from earnings.services.filing_links import (
    FilingEarningsReviewIntegrityError,
    InvalidFilingEarningsReview,
    confirm_filing_earnings_link,
    evaluate_filing_earnings_link,
    reject_filing_earnings_link,
)
from tests.earnings.filing_helpers import make_filing_with_evidence
from tests.earnings.helpers import make_company, make_event, make_user

pytestmark = pytest.mark.django_db

RELEASE_WINDOW_FACTS = {
    "estimated_release_date": date(2026, 6, 1),
    "estimated_release_precision": "date_only",
}


def _run(filing: object, sync_run: object | None = None) -> object:
    evidence = filing.source_evidence
    assert evidence is not None
    return evaluate_filing_earnings_link(
        filing=filing,
        sync_run=sync_run or evidence.sync_run,
    )


def _release_setup(*, reported_items: str = "2.02", document_types=("EX-99.1",)):
    company = make_company("review")
    event = make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        **RELEASE_WINDOW_FACTS,
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        reported_items=reported_items,
        document_types=document_types,
    )
    return company, event, filing


def _periodic_setup():
    company = make_company("periodic-review")
    event = make_event(
        company=company,
        period_end_date=date(2026, 3, 31),
        period_type="Q1",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 3, 31),
    )
    return company, event, filing


def _leaf(filing: object, relation_type: str = "RELEASE_FILING") -> object:
    decisions = list(
        FilingEarningsDecision.objects.filter(
            filing=filing,
            relation_type=relation_type,
        )
    )
    superseded = {item.supersedes_id for item in decisions if item.supersedes_id is not None}
    leaves = [item for item in decisions if item.pk not in superseded]
    assert len(leaves) == 1
    return leaves[0]


def test_manual_confirm_updates_projection_and_writes_audit() -> None:
    _, event, filing = _release_setup()
    auto = _run(filing)
    actor = make_user("confirm")
    audit_before = AuditRecord.objects.count()

    result = confirm_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        target_event=event,
        actor_user=actor,
        reason="Verified with the company IR release.",
        request_id="confirm-1",
    )

    assert result.decision_created is True
    assert result.link_updated is True
    decision = result.decision
    assert decision.decision_type == "manual_confirmed"
    assert decision.status == "resolved"
    assert decision.decision_source == "manual"
    assert decision.classification == "YES"
    assert decision.confidence == "MANUAL"
    assert decision.supersedes_id == auto.decision.pk
    assert decision.target_event_id == event.pk
    link = FilingEarningsLink.objects.get(pk=result.link.pk)
    assert link.review_status == "confirmed"
    assert link.reviewed_by_id == actor.pk
    assert link.reviewed_at is not None
    assert link.review_reason == "Verified with the company IR release."
    assert link.confidence == "MANUAL"
    assert link.current_decision_id == decision.pk
    assert link.release_filing_classification == "YES"
    assert AuditRecord.objects.filter(
        target_type="filing_earnings_decision",
        target_id=decision.pk,
        actor_user=actor,
        action="manual_correction",
    ).exists()
    assert AuditRecord.objects.count() > audit_before
    assert DataChange.objects.filter(target_id=link.pk).exists()
    assert _leaf(filing).pk == decision.pk


def test_manual_confirm_replay_with_same_request_is_idempotent() -> None:
    _, event, filing = _release_setup()
    _run(filing)
    actor = make_user("confirm-replay")
    first = confirm_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        target_event=event,
        actor_user=actor,
        reason="Verified.",
        request_id="confirm-replay",
    )
    decision_count = FilingEarningsDecision.objects.count()
    audit_count = AuditRecord.objects.count()
    change_count = DataChange.objects.count()

    second = confirm_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        target_event=event,
        actor_user=actor,
        reason="Verified.",
        request_id="confirm-replay",
    )

    assert second.decision_created is False
    assert second.link_created is False
    assert second.link_updated is False
    assert second.decision.pk == first.decision.pk
    assert FilingEarningsDecision.objects.count() == decision_count
    assert AuditRecord.objects.count() == audit_count
    assert DataChange.objects.count() == change_count


def test_manual_request_replay_with_different_reason_fails_closed() -> None:
    _, event, filing = _release_setup()
    _run(filing)
    actor = make_user("confirm-conflict")
    confirm_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        target_event=event,
        actor_user=actor,
        reason="Verified.",
        request_id="confirm-conflict",
    )

    with pytest.raises(InvalidFilingEarningsReview):
        confirm_filing_earnings_link(
            filing=filing,
            relation_type="RELEASE_FILING",
            target_event=event,
            actor_user=actor,
            reason="Different reason.",
            request_id="confirm-conflict",
        )


def test_manual_confirm_escalates_review_required_classification() -> None:
    _, event, filing = _release_setup(reported_items="", document_types=("EX-99.1",))
    auto = _run(filing)
    assert auto.link.release_filing_classification == "REVIEW_REQUIRED"
    actor = make_user("escalate")

    result = confirm_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        target_event=event,
        actor_user=actor,
        reason="Reviewer inspected the filing metadata.",
        request_id="escalate-1",
    )

    assert result.link.release_filing_classification == "YES"
    assert result.link.classification_reason == "MANUAL_CONFIRMED"
    assert DataChange.objects.filter(
        target_id=result.link.pk,
        field_name="release_filing_classification",
    ).exists()


def test_manual_confirm_periodic_link() -> None:
    _, event, filing = _periodic_setup()
    auto = _run(filing)
    assert auto.link.review_status == "auto"
    actor = make_user("periodic-confirm")

    result = confirm_filing_earnings_link(
        filing=filing,
        relation_type="PERIODIC_FILING",
        target_event=event,
        actor_user=actor,
        reason="Confirmed against the filing period.",
        request_id="periodic-confirm-1",
    )

    assert result.link.review_status == "confirmed"
    assert result.link.release_filing_classification is None
    assert result.link.confidence == "MANUAL"
    assert result.decision.decision_type == "manual_confirmed"
    assert result.decision.classification is None
    assert result.decision.classification_rule_version == ""


def test_manual_reject_existing_link_and_blocks_automation() -> None:
    _, _, filing = _release_setup()
    auto = _run(filing)
    actor = make_user("reject")

    result = reject_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        actor_user=actor,
        reason="The exhibit is not this quarter's release.",
        request_id="reject-1",
    )

    link = FilingEarningsLink.objects.get(pk=auto.link.pk)
    assert link.review_status == "rejected"
    assert link.reviewed_by_id == actor.pk
    assert link.current_decision_id == result.decision.pk
    assert result.decision.decision_type == "manual_rejected"
    assert result.decision.status == "rejected"
    decision_count = FilingEarningsDecision.objects.count()

    blocked = _run(filing)

    assert blocked.blocked_by_manual_authority is True
    assert blocked.decision_created is False
    assert FilingEarningsDecision.objects.count() == decision_count
    link.refresh_from_db()
    assert link.review_status == "rejected"


def test_manual_reject_without_link_blocks_future_link_creation() -> None:
    company = make_company("reject-no-link")
    make_event(company=company, period_end_date=date(2026, 6, 30), period_type="Q2")
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    no_match = _run(filing)
    assert no_match.link is None
    actor = make_user("reject-no-link")

    rejection = reject_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        actor_user=actor,
        reason="No release filing is expected.",
        request_id="reject-no-link-1",
    )
    decision_count = FilingEarningsDecision.objects.count()

    blocked = _run(filing)

    assert rejection.link is None
    assert blocked.blocked_by_manual_authority is True
    assert blocked.link is None
    assert FilingEarningsDecision.objects.count() == decision_count


def test_automatic_after_manual_confirm_is_blocked() -> None:
    _, event, filing = _release_setup()
    _run(filing)
    actor = make_user("blocked-auto")
    confirm_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        target_event=event,
        actor_user=actor,
        reason="Confirmed.",
        request_id="blocked-auto-1",
    )
    decision_count = FilingEarningsDecision.objects.count()

    blocked = _run(filing)

    assert blocked.blocked_by_manual_authority is True
    assert FilingEarningsDecision.objects.count() == decision_count


def test_new_manual_decision_supersedes_previous_leaf() -> None:
    _, event, filing = _release_setup()
    _run(filing)
    actor = make_user("supersede")
    first = confirm_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        target_event=event,
        actor_user=actor,
        reason="Confirmed.",
        request_id="supersede-1",
    )
    second = reject_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        actor_user=actor,
        reason="Corrected decision.",
        request_id="supersede-2",
    )
    third = confirm_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        target_event=event,
        actor_user=actor,
        reason="Reconfirmed.",
        request_id="supersede-3",
    )

    assert second.decision.supersedes_id == first.decision.pk
    assert third.decision.supersedes_id == second.decision.pk
    leaf = _leaf(filing)
    assert leaf.pk == third.decision.pk
    link = FilingEarningsLink.objects.get()
    assert link.review_status == "confirmed"
    assert link.current_decision_id == third.decision.pk


def test_rule_upgrade_appends_successor_and_writes_data_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, filing = _release_setup()
    first = _run(filing)
    assert first.link.classification_rule_version == "filing-release-classification-v1"
    monkeypatch.setattr(
        filing_links,
        "CLASSIFICATION_RULE_VERSION",
        "filing-release-classification-v2",
    )

    second = _run(filing)

    assert second.decision_created is True
    assert second.decision.supersedes_id == first.decision.pk
    link = FilingEarningsLink.objects.get(pk=first.link.pk)
    assert link.classification_rule_version == "filing-release-classification-v2"
    assert link.current_decision_id == second.decision.pk
    assert DataChange.objects.filter(
        target_id=link.pk,
        field_name="classification_rule_version",
        old_value="filing-release-classification-v1",
        new_value="filing-release-classification-v2",
    ).exists()
    first.decision.refresh_from_db()
    assert first.decision.classification_rule_version == ("filing-release-classification-v1")


def test_rule_upgrade_without_projection_change_appends_decision_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = make_company("rule-upgrade")
    make_event(company=company, period_end_date=date(2026, 6, 30), period_type="Q2")
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    first = _run(filing)
    assert first.outcome == "no_match"
    audit_count = AuditRecord.objects.count()
    monkeypatch.setattr(filing_links, "MATCH_RULE_VERSION", "filing-earnings-match-v2")

    second = _run(filing)

    assert second.decision_created is True
    assert second.link is None
    assert second.decision.supersedes_id == first.decision.pk
    assert FilingEarningsLink.objects.count() == 0
    assert DataChange.objects.count() == 0
    assert AuditRecord.objects.count() == audit_count + 1


def test_manual_leaf_blocks_rule_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    _, event, filing = _release_setup()
    _run(filing)
    actor = make_user("manual-leaf")
    confirmation = confirm_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        target_event=event,
        actor_user=actor,
        reason="Confirmed.",
        request_id="manual-leaf-1",
    )
    decision_count = FilingEarningsDecision.objects.count()
    monkeypatch.setattr(filing_links, "MATCH_RULE_VERSION", "filing-earnings-match-v2")

    blocked = _run(filing)

    assert blocked.blocked_by_manual_authority is True
    assert FilingEarningsDecision.objects.count() == decision_count
    link = FilingEarningsLink.objects.get()
    assert link.current_decision_id == confirmation.decision.pk


def test_manual_confirmation_cannot_repoint_a_link() -> None:
    company, event, filing = _release_setup()
    _run(filing)
    other_event = make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
    )
    actor = make_user("repoint")
    decision_count = FilingEarningsDecision.objects.count()

    with pytest.raises(FilingEarningsReviewIntegrityError):
        confirm_filing_earnings_link(
            filing=filing,
            relation_type="RELEASE_FILING",
            target_event=other_event,
            actor_user=actor,
            reason="Wrong event.",
            request_id="repoint-1",
        )

    assert FilingEarningsDecision.objects.count() == decision_count
    assert FilingEarningsLink.objects.get().earnings_event_id == event.pk


def test_manual_confirm_requires_canonical_event() -> None:
    company, _, filing = _release_setup()
    candidate = make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        identity_status="candidate",
        identity_key=None,
        identity_rule_version=None,
    )
    actor = make_user("candidate-confirm")

    with pytest.raises(InvalidFilingEarningsReview):
        confirm_filing_earnings_link(
            filing=filing,
            relation_type="RELEASE_FILING",
            target_event=candidate,
            actor_user=actor,
            reason="Candidate is not canonical.",
            request_id="candidate-confirm-1",
        )


def test_failed_manual_audit_write_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, event, filing = _release_setup()
    auto = _run(filing)
    actor = make_user("rollback")
    decision_count = FilingEarningsDecision.objects.count()
    link = FilingEarningsLink.objects.get()

    def fail_audit(**kwargs: object) -> object:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(filing_links, "record_user_action", fail_audit)
    with pytest.raises(RuntimeError, match="audit write failed"):
        confirm_filing_earnings_link(
            filing=filing,
            relation_type="RELEASE_FILING",
            target_event=event,
            actor_user=actor,
            reason="This must roll back.",
            request_id="rollback-1",
        )

    assert FilingEarningsDecision.objects.count() == decision_count
    link.refresh_from_db()
    assert link.review_status == "auto"
    assert link.current_decision_id == auto.decision.pk


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_automatic_evaluation_is_idempotent() -> None:
    _, _, filing = _release_setup()
    barrier = Barrier(2, timeout=10)

    def evaluate() -> object:
        close_old_connections()
        try:
            barrier.wait()
            return _run(filing)
        finally:
            for current_connection in connections.all():
                current_connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: evaluate(), range(2)))

    assert FilingEarningsDecision.objects.count() == 1
    assert FilingEarningsLink.objects.count() == 1
    assert sorted(result.decision_created for result in results) == [False, True]


@pytest.mark.django_db(transaction=True)
def test_concurrent_manual_confirmation_same_request_is_idempotent() -> None:
    _, event, filing = _release_setup()
    _run(filing)
    actor = make_user("concurrent-manual")
    barrier = Barrier(2, timeout=10)

    def confirm() -> object:
        close_old_connections()
        try:
            barrier.wait()
            return confirm_filing_earnings_link(
                filing=filing,
                relation_type="RELEASE_FILING",
                target_event=event,
                actor_user=actor,
                reason="Concurrent confirmation.",
                request_id="concurrent-manual-1",
            )
        finally:
            for current_connection in connections.all():
                current_connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: confirm(), range(2)))

    manual_decisions = FilingEarningsDecision.objects.filter(decision_source="manual")
    assert manual_decisions.count() == 1
    assert sorted(result.decision_created for result in results) == [False, True]
    assert FilingEarningsLink.objects.get().review_status == "confirmed"


@pytest.mark.django_db(transaction=True)
def test_manual_and_automatic_race_keeps_manual_authority() -> None:
    _, event, filing = _release_setup()
    actor = make_user("race")
    barrier = Barrier(2, timeout=10)

    def evaluate() -> object:
        close_old_connections()
        try:
            barrier.wait()
            return _run(filing)
        finally:
            for current_connection in connections.all():
                current_connection.close()

    def confirm() -> object:
        close_old_connections()
        try:
            barrier.wait()
            return confirm_filing_earnings_link(
                filing=filing,
                relation_type="RELEASE_FILING",
                target_event=event,
                actor_user=actor,
                reason="Manual wins.",
                request_id="race-1",
            )
        finally:
            for current_connection in connections.all():
                current_connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        automatic_future = executor.submit(evaluate)
        manual_future = executor.submit(confirm)
        automatic_future.result(timeout=15)
        manual_future.result(timeout=15)

    leaves = _leaf(filing)
    assert leaves.decision_type == "manual_confirmed"
    link = FilingEarningsLink.objects.get()
    assert link.review_status == "confirmed"
    assert link.current_decision_id == leaves.pk
