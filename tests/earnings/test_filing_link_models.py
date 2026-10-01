# mypy: ignore-errors
"""Model and DB constraint tests for FilingEarningsDecision / FilingEarningsLink."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError

from audit.models import AppendOnlyRecordError
from earnings.models import FilingEarningsDecision, FilingEarningsLink
from tests.earnings.filing_helpers import (
    make_filing_earnings_link,
    make_filing_link_decision,
    make_filing_with_evidence,
    make_sec_sync_run,
)
from tests.earnings.helpers import make_company, make_event, make_user


def _constraint_name(error: IntegrityError) -> str | None:
    cause = error.__cause__
    return getattr(getattr(cause, "diag", None), "constraint_name", None)


def _release_pair() -> tuple[object, object, FilingEarningsDecision, FilingEarningsLink]:
    company = make_company("link")
    event = make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
        reported_items="2.02",
        document_types=("EX-99.1",),
    )
    decision = make_filing_link_decision(
        filing=filing,
        relation_type="RELEASE_FILING",
        decision_type="matched_release_filing",
        status="resolved",
        target_event=event,
        classification="YES",
        confidence="BOUNDED_WINDOW",
        classification_rule_version="fixture-classification-v1",
    )
    link = make_filing_earnings_link(
        filing=filing,
        earnings_event=event,
        current_decision=decision,
    )
    return filing, event, decision, link


@pytest.mark.django_db
class TestFilingEarningsDecisionModel:
    def test_minimal_review_required_decision(self) -> None:
        filing = make_filing_with_evidence(company=make_company("decision"))
        decision = make_filing_link_decision(filing=filing)

        assert decision.decision_type == "review_required"
        assert decision.status == "open"
        assert decision.target_event_id is None
        assert decision.decision_source == "automatic"
        assert decision.sync_run_id is not None
        assert str(decision).startswith(f"{filing.pk}:RELEASE_FILING:")

    def test_manual_decision_requires_actor_reason_request(self) -> None:
        filing = make_filing_with_evidence(company=make_company("manual"))
        actor = make_user("manual")
        decision = make_filing_link_decision(
            filing=filing,
            decision_source="manual",
            actor_user=actor,
            sync_run=None,
            reason="reviewed",
            request_id="request-1",
        )
        assert decision.actor_user_id == actor.pk
        assert decision.sync_run_id is None

    @pytest.mark.parametrize(
        ("decision_type", "status", "relation_type", "classification", "version"),
        (
            ("matched_release_filing", "resolved", "PERIODIC_FILING", "YES", "v1"),
            ("matched_periodic_filing", "resolved", "PERIODIC_FILING", "YES", "v1"),
            ("matched_release_filing", "resolved", "RELEASE_FILING", None, ""),
            ("matched_release_filing", "resolved", "RELEASE_FILING", "YES", ""),
            ("manual_confirmed", "resolved", "RELEASE_FILING", "NO", "v1"),
        ),
    )
    def test_invalid_classification_shapes(
        self,
        decision_type: str,
        status: str,
        relation_type: str,
        classification: str | None,
        version: str,
    ) -> None:
        filing = make_filing_with_evidence(company=make_company("shape"))
        event = make_event(company=filing.company)
        actor = make_user("shape")
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_filing_link_decision(
                    filing=filing,
                    relation_type=relation_type,
                    decision_type=decision_type,
                    status=status,
                    target_event=event,
                    classification=classification,
                    classification_rule_version=version,
                    confidence="MANUAL" if decision_type == "manual_confirmed" else "EXACT",
                    decision_source=(
                        "manual" if decision_type == "manual_confirmed" else "automatic"
                    ),
                    actor_user=actor if decision_type == "manual_confirmed" else None,
                    sync_run=None if decision_type == "manual_confirmed" else make_sec_sync_run(),
                    reason="reviewed" if decision_type == "manual_confirmed" else "",
                    request_id="req-shape" if decision_type == "manual_confirmed" else "",
                )

        assert _constraint_name(exc_info.value) in {
            "filing_earnings_decision_classification_shape_valid",
            "filing_earnings_decision_classification_version_required",
        }

    @pytest.mark.parametrize(
        ("decision_type", "status", "with_target"),
        (
            ("review_required", "resolved", False),
            ("no_match", "open", False),
            ("no_match", "rejected", True),
            ("matched_periodic_filing", "open", True),
        ),
    )
    def test_invalid_outcome_couplings(
        self,
        decision_type: str,
        status: str,
        with_target: bool,
    ) -> None:
        filing = make_filing_with_evidence(company=make_company("outcome"))
        event = make_event(company=filing.company)
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_filing_link_decision(
                    filing=filing,
                    decision_type=decision_type,
                    status=status,
                    target_event=event if with_target else None,
                    relation_type=(
                        "PERIODIC_FILING"
                        if decision_type == "matched_periodic_filing"
                        else "RELEASE_FILING"
                    ),
                )

        assert _constraint_name(exc_info.value) == "filing_earnings_decision_outcome_valid"

    def test_automatic_decision_requires_sync_run(self) -> None:
        filing = make_filing_with_evidence(company=make_company("sync"))
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_filing_link_decision(filing=filing, sync_run=None)

        assert _constraint_name(exc_info.value) == ("filing_earnings_decision_source_context_valid")

    def test_manual_decision_requires_request_and_reason(self) -> None:
        filing = make_filing_with_evidence(company=make_company("manual-context"))
        actor = make_user("manual-context")
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_filing_link_decision(
                    filing=filing,
                    decision_source="manual",
                    actor_user=actor,
                    sync_run=None,
                    reason="",
                    request_id="",
                )

        assert _constraint_name(exc_info.value) == ("filing_earnings_decision_source_context_valid")

    def test_decision_key_format_is_enforced(self) -> None:
        filing = make_filing_with_evidence(company=make_company("key"))
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_filing_link_decision(filing=filing, decision_key="not-a-key")

        assert _constraint_name(exc_info.value) == "filing_earnings_decision_key_valid"

    def test_duplicate_decision_key_is_rejected(self) -> None:
        filing = make_filing_with_evidence(company=make_company("duplicate"))
        key = uuid.uuid4().hex + uuid.uuid4().hex
        make_filing_link_decision(filing=filing, decision_key=key)
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_filing_link_decision(filing=filing, decision_key=key)

        assert _constraint_name(exc_info.value) == "filing_earnings_decision_key_unique"

    def test_decision_is_append_only(self) -> None:
        filing = make_filing_with_evidence(company=make_company("append"))
        decision = make_filing_link_decision(filing=filing)
        decision.reason = "changed"
        with pytest.raises(AppendOnlyRecordError):
            decision.save()
        with pytest.raises(AppendOnlyRecordError):
            decision.delete()

    def test_decision_rejects_self_supersede(self) -> None:
        filing = make_filing_with_evidence(company=make_company("self"))
        decision = make_filing_link_decision(filing=filing)
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE earnings_filingearningsdecision "
                        "SET supersedes_id = id WHERE id = %s",
                        [decision.pk],
                    )

        assert _constraint_name(exc_info.value) == "filing_earnings_decision_not_self"


@pytest.mark.django_db
class TestFilingEarningsLinkModel:
    def test_minimal_release_link(self) -> None:
        _, _, decision, link = _release_pair()

        assert link.release_filing_classification == "YES"
        assert link.review_status == "auto"
        assert link.current_decision_id == decision.pk
        assert str(link).startswith(f"{link.filing_id}:{link.earnings_event_id}:")

    def test_periodic_link_has_no_release_classification(self) -> None:
        company = make_company("periodic")
        event = make_event(company=company)
        filing = make_filing_with_evidence(
            company=company,
            form_type="10-Q",
            period_of_report=event.period_end_date,
        )
        decision = make_filing_link_decision(
            filing=filing,
            relation_type="PERIODIC_FILING",
            decision_type="matched_periodic_filing",
            status="resolved",
            target_event=event,
            classification=None,
            confidence="EXACT",
            classification_rule_version="",
        )
        link = make_filing_earnings_link(
            filing=filing,
            earnings_event=event,
            current_decision=decision,
            relation_type="PERIODIC_FILING",
            release_filing_classification=None,
            classification_reason="",
            classification_rule_version="",
            confidence="EXACT",
        )

        assert link.release_filing_classification is None

    @pytest.mark.parametrize(
        "overrides",
        (
            {"release_filing_classification": None},
            {"classification_reason": ""},
            {"classification_rule_version": ""},
        ),
    )
    def test_release_link_requires_classification_provenance(
        self,
        overrides: dict[str, object],
    ) -> None:
        _, _, decision, _ = _release_pair()
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_filing_earnings_link(
                    filing=decision.filing,
                    earnings_event=decision.target_event,
                    current_decision=decision,
                    **overrides,
                )

        assert _constraint_name(exc_info.value) in {
            "filing_earnings_link_release_classification_scope_valid",
            "filing_earnings_link_classification_provenance_valid",
        }

    def test_auto_link_forbids_reviewed_fields(self) -> None:
        filing, event, decision, _ = _release_pair()
        actor = make_user("auto-link")
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_filing_earnings_link(
                    filing=filing,
                    earnings_event=event,
                    current_decision=decision,
                    reviewed_by=actor,
                    reviewed_at=datetime.now(UTC),
                    review_reason="should not exist",
                )

        assert _constraint_name(exc_info.value) == "filing_earnings_link_review_state_valid"

    def test_confirmed_release_requires_yes(self) -> None:
        filing, event, decision, _ = _release_pair()
        actor = make_user("confirmed")
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_filing_earnings_link(
                    filing=filing,
                    earnings_event=event,
                    current_decision=decision,
                    release_filing_classification="NO",
                    review_status="confirmed",
                    review_reason="reviewed",
                    reviewed_by=actor,
                    reviewed_at=datetime.now(UTC),
                )

        assert _constraint_name(exc_info.value) == "filing_earnings_link_confirmed_release_yes"

    def test_link_identity_unique(self) -> None:
        filing, event, decision, _ = _release_pair()
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_filing_earnings_link(
                    filing=filing,
                    earnings_event=event,
                    current_decision=decision,
                )

        assert _constraint_name(exc_info.value) == "filing_earnings_link_identity_unique"

    def test_link_protects_event_and_decision(self) -> None:
        _, event, decision, link = _release_pair()
        with pytest.raises(ProtectedError):
            event.delete()
        with pytest.raises(AppendOnlyRecordError):
            decision.delete()
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM earnings_filingearningsdecision WHERE id = %s",
                    [decision.pk],
                )
        assert FilingEarningsLink.objects.filter(pk=link.pk).exists()
