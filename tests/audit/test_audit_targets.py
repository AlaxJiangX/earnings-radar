# mypy: ignore-errors
"""Audit target integration tests for reconciliation decisions."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from accounts.models import User
from audit.services import (
    InvalidAuditRecord,
    InvalidDataChange,
    InvalidSourceEvidence,
    record_data_change,
    record_source_evidence,
    record_system_action,
    record_user_action,
)


@pytest.mark.django_db
def test_system_action_accepts_reconciliation_decision_target(sync_run) -> None:
    result = record_system_action(
        sync_run=sync_run,
        action="create",
        target_type="earnings_reconciliation_decision",
        target_id=uuid.uuid4(),
        before={},
        after={"decision_type": "matched_canonical"},
        request_id="audit-decision-target",
    )

    assert result.record.target_type == "earnings_reconciliation_decision"


@pytest.mark.django_db
def test_user_action_accepts_reconciliation_decision_target(db) -> None:
    actor = User.objects.create_user(
        email="audit-target@example.test",
        password="test",
    )
    result = record_user_action(
        actor_user=actor,
        action="manual_correction",
        target_type="earnings_reconciliation_decision",
        target_id=uuid.uuid4(),
        before={},
        after={"decision_type": "no_match"},
        reason="Reviewed reconciliation decision.",
        request_id="audit-decision-manual-target",
    )

    assert result.record.actor_user_id == actor.pk
    assert result.record.target_type == "earnings_reconciliation_decision"


@pytest.mark.django_db
def test_observation_target_remains_invalid(sync_run) -> None:
    with pytest.raises(InvalidAuditRecord):
        record_system_action(
            sync_run=sync_run,
            action="create",
            target_type="earnings_calendar_observation",
            target_id=uuid.uuid4(),
            before={},
            after={},
            request_id="audit-observation-target",
        )


@pytest.mark.django_db
def test_existing_audit_targets_still_work(sync_run) -> None:
    result = record_system_action(
        sync_run=sync_run,
        action="create",
        target_type="earnings_event",
        target_id=uuid.uuid4(),
        before={},
        after={},
        request_id="audit-existing-target",
    )

    assert result.record.target_type == "earnings_event"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "target_type",
    ("earnings_calendar_observation", "earnings_reconciliation_decision"),
)
def test_source_evidence_rejects_new_targets(
    sync_run,
    raw_data_observation,
    target_type: str,
) -> None:
    with pytest.raises(InvalidSourceEvidence):
        record_source_evidence(
            raw_data_record=raw_data_observation.raw_data_record,
            sync_run=sync_run,
            target_type=target_type,
            target_id=uuid.uuid4(),
            field_name="estimated_release",
            raw_value="2026-04-22",
            normalized_value="2026-04-22",
            confidence=Decimal("0.9000"),
            normalizer_version="fixture-v1",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "target_type",
    ("earnings_calendar_observation", "earnings_reconciliation_decision"),
)
def test_data_change_rejects_new_targets(
    sync_run,
    target_type: str,
) -> None:
    with pytest.raises(InvalidDataChange):
        record_data_change(
            target_type=target_type,
            target_id=uuid.uuid4(),
            field_name="estimated_release",
            old_value=None,
            new_value={"kind": "date", "value": "2026-04-22", "precision": "date_only"},
            rule_version="fixture-v1",
            sync_run=sync_run,
        )
