# mypy: ignore-errors
"""Model and DB constraint tests for EarningsReconciliationDecision."""

from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError

from audit.models import AppendOnlyRecordError
from earnings.models import EarningsReconciliationDecision
from tests.earnings.helpers import (
    make_calendar_observation,
    make_event,
    make_reconciliation_decision,
    make_sync_run,
    make_user,
)


def _constraint_name(error: IntegrityError) -> str | None:
    cause = error.__cause__
    return getattr(getattr(cause, "diag", None), "constraint_name", None)


def _decision_key() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


@pytest.mark.django_db
class TestEarningsReconciliationDecisionModel:
    def test_minimal_automatic_resolved_decision(self) -> None:
        decision = make_reconciliation_decision()

        assert decision.pk is not None
        assert decision.decision_type == "matched_canonical"
        assert decision.status == "resolved"
        assert decision.target_event_id is not None
        assert decision.actor_user_id is None
        assert decision.sync_run_id is not None
        assert decision.request_id == ""
        assert decision.covered_fields == []
        assert decision.match_factors == {}
        assert str(decision) == (
            f"{decision.observation_id}:{decision.decision_type}:{decision.status}"
        )

    def test_minimal_manual_resolved_decision(self) -> None:
        actor = make_user("manual")
        decision = make_reconciliation_decision(
            actor_user=actor,
            sync_run=None,
            reason="manual review",
            request_id="request-1",
        )

        assert decision.actor_user_id == actor.pk
        assert decision.sync_run_id is None
        assert decision.reason == "manual review"
        assert decision.request_id == "request-1"

    @pytest.mark.parametrize(
        "decision_type",
        ("collision", "conflict", "review_required"),
    )
    def test_valid_open_decisions(self, decision_type: str) -> None:
        target_event = None if decision_type == "review_required" else make_event()
        decision = make_reconciliation_decision(
            decision_type=decision_type,
            status="open",
            target_event=target_event,
        )

        assert decision.status == "open"
        assert decision.decision_type == decision_type
        assert decision.target_event_id == getattr(target_event, "pk", None)

    @pytest.mark.parametrize("decision_type", ("no_match", "ignored"))
    def test_valid_rejected_decisions(self, decision_type: str) -> None:
        decision = make_reconciliation_decision(
            decision_type=decision_type,
            status="rejected",
            target_event=None,
        )

        assert decision.status == "rejected"
        assert decision.decision_type == decision_type
        assert decision.target_event_id is None

    def test_invalid_decision_type(self) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_reconciliation_decision(decision_type="manual_link")

        assert _constraint_name(exc_info.value) in {
            "earnings_reconciliation_decision_type_valid",
            "earnings_reconciliation_decision_outcome_valid",
        }

    def test_invalid_status(self) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_reconciliation_decision(status="superseded")

        assert _constraint_name(exc_info.value) in {
            "earnings_reconciliation_decision_status_valid",
            "earnings_reconciliation_decision_outcome_valid",
        }

    @pytest.mark.parametrize(
        ("decision_type", "status", "target_kind"),
        (
            ("collision", "resolved", "event"),
            ("matched_canonical", "resolved", "none"),
            ("matched_canonical", "open", "event"),
            ("matched_canonical", "rejected", "event"),
            ("no_match", "rejected", "event"),
        ),
    )
    def test_invalid_outcome_couplings(
        self,
        decision_type: str,
        status: str,
        target_kind: str,
    ) -> None:
        target_event = make_event() if target_kind == "event" else None

        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_reconciliation_decision(
                    decision_type=decision_type,
                    status=status,
                    target_event=target_event,
                )

        assert _constraint_name(exc_info.value) == (
            "earnings_reconciliation_decision_outcome_valid"
        )

    @pytest.mark.parametrize(
        "scenario",
        (
            "no_actor_no_sync",
            "missing_reason",
            "missing_request",
            "automatic_request",
        ),
    )
    def test_invalid_context(self, scenario: str) -> None:
        if scenario == "no_actor_no_sync":
            overrides: dict[str, object] = {
                "actor_user": None,
                "sync_run": None,
                "request_id": "",
            }
        elif scenario == "missing_reason":
            overrides = {
                "actor_user": make_user("missing-reason"),
                "reason": "",
                "request_id": "r",
            }
        elif scenario == "missing_request":
            overrides = {
                "actor_user": make_user("missing-request"),
                "reason": "why",
                "request_id": "",
            }
        else:
            overrides = {
                "actor_user": None,
                "sync_run": make_sync_run("automatic-request"),
                "request_id": "not-empty",
            }

        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_reconciliation_decision(**overrides)

        assert _constraint_name(exc_info.value) == (
            "earnings_reconciliation_decision_context_valid"
        )

    def test_invalid_decision_key_format(self) -> None:
        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_reconciliation_decision(decision_key="not-a-key")

        assert _constraint_name(exc_info.value) == ("earnings_reconciliation_decision_key_valid")

    def test_decision_key_is_unique(self) -> None:
        key = _decision_key()
        make_reconciliation_decision(decision_key=key)

        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_reconciliation_decision(decision_key=key)

        assert _constraint_name(exc_info.value) == ("earnings_reconciliation_decision_key_unique")

    def test_self_supersede_is_rejected(self) -> None:
        decision_id = uuid.uuid4()

        with pytest.raises(IntegrityError) as exc_info:
            with transaction.atomic():
                make_reconciliation_decision(
                    id=decision_id,
                    supersedes_id=decision_id,
                    decision_key=_decision_key(),
                )

        assert _constraint_name(exc_info.value) == ("earnings_reconciliation_decision_not_self")

    def test_same_predecessor_allows_multiple_successors(self) -> None:
        observation = make_calendar_observation()
        predecessor = make_reconciliation_decision(
            observation=observation,
            decision_key=_decision_key(),
        )
        first = make_reconciliation_decision(
            observation=observation,
            supersedes=predecessor,
            decision_key=_decision_key(),
        )
        second = make_reconciliation_decision(
            observation=observation,
            supersedes=predecessor,
            decision_key=_decision_key(),
        )

        assert first.supersedes_id == predecessor.pk
        assert second.supersedes_id == predecessor.pk
        assert EarningsReconciliationDecision.objects.count() == 3

    def test_foreign_keys_are_protected(self) -> None:
        observation = make_calendar_observation()
        target_event = make_event()
        actor = make_user("protected")
        sync_run = make_sync_run("protected")
        predecessor = make_reconciliation_decision(
            observation=observation,
            decision_key=_decision_key(),
        )
        make_reconciliation_decision(
            observation=observation,
            target_event=target_event,
            actor_user=actor,
            sync_run=sync_run,
            reason="protected",
            request_id="protected-request",
            supersedes=predecessor,
            decision_key=_decision_key(),
        )

        with pytest.raises(ProtectedError):
            target_event.delete()
        with pytest.raises(ProtectedError):
            actor.delete()
        with pytest.raises(ProtectedError):
            sync_run.delete()
        with pytest.raises(AppendOnlyRecordError):
            observation.delete()
        with pytest.raises(AppendOnlyRecordError):
            predecessor.delete()
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                    cursor.execute(
                        "DELETE FROM earnings_earningscalendarobservation WHERE id = %s",
                        [observation.pk],
                    )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                    cursor.execute(
                        "DELETE FROM earnings_earningsreconciliationdecision WHERE id = %s",
                        [predecessor.pk],
                    )

    def test_decision_is_append_only(self) -> None:
        decision = make_reconciliation_decision()
        decision.reason = "changed"

        with pytest.raises(AppendOnlyRecordError):
            decision.save()
        with pytest.raises(AppendOnlyRecordError):
            EarningsReconciliationDecision.objects.filter(pk=decision.pk).update(reason="changed")
        with pytest.raises(AppendOnlyRecordError):
            EarningsReconciliationDecision.objects.filter(pk=decision.pk).delete()
        with pytest.raises(AppendOnlyRecordError):
            decision.delete()
        with pytest.raises(AppendOnlyRecordError):
            EarningsReconciliationDecision.objects.bulk_update(
                [decision],
                ["reason"],
            )

    def test_json_defaults_are_isolated(self) -> None:
        first = make_reconciliation_decision()
        second = make_reconciliation_decision()

        first.covered_fields.append("estimated_release")
        first.match_factors["fixture"] = True

        assert second.covered_fields == []
        assert second.match_factors == {}
