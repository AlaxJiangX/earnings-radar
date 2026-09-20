# mypy: ignore-errors
"""Persistence and concurrency tests for reconciliation decisions."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from unittest import mock

import pytest
from django.db import (
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)

from earnings.models import EarningsReconciliationDecision
from earnings.services.reconciliation import (
    EarningsReconciliationDecisionIntegrityError,
    InvalidEarningsReconciliationDecision,
    build_earnings_reconciliation_decision_key,
    record_earnings_reconciliation_decision,
)
from tests.earnings.helpers import (
    make_calendar_observation,
    make_event,
    make_sync_run,
    make_user,
)

_UNSET = object()


def _record(
    *,
    observation,
    decision_type: str = "matched_canonical",
    status: str = "resolved",
    rule_version: str = "fixture-reconciliation-v1",
    target_event: object = _UNSET,
    covered_fields=(),
    match_factors: object = _UNSET,
    reason: str = "",
    actor_user=None,
    sync_run: object = _UNSET,
    request_id: str = "",
    supersedes=None,
    decided_at=None,
):
    if target_event is _UNSET:
        target_event = make_event() if status == "resolved" else None
    if match_factors is _UNSET:
        match_factors = {}
    if sync_run is _UNSET:
        sync_run = make_sync_run("reconciliation") if actor_user is None else None
    return record_earnings_reconciliation_decision(
        observation=observation,
        decision_type=decision_type,
        status=status,
        rule_version=rule_version,
        target_event=target_event,
        covered_fields=covered_fields,
        match_factors=match_factors,
        reason=reason,
        actor_user=actor_user,
        sync_run=sync_run,
        request_id=request_id,
        supersedes=supersedes,
        decided_at=decided_at,
    )


@pytest.mark.django_db
class TestEarningsReconciliationDecisionPersistence:
    def test_automatic_resolved_decision_replays_across_sync_runs(self) -> None:
        observation = make_calendar_observation()
        target_event = make_event()

        first = _record(
            observation=observation,
            target_event=target_event,
            sync_run=make_sync_run("automatic-one"),
        )
        second = _record(
            observation=observation,
            target_event=target_event,
            sync_run=make_sync_run("automatic-two"),
        )

        assert first.created is True
        assert second.created is False
        assert second.decision.pk == first.decision.pk
        assert EarningsReconciliationDecision.objects.count() == 1

    def test_manual_resolved_decision_replays(self) -> None:
        observation = make_calendar_observation()
        actor = make_user("manual-replay")
        target_event = make_event()

        first = _record(
            observation=observation,
            target_event=target_event,
            actor_user=actor,
            sync_run=None,
            reason="manual review",
            request_id="request-1",
        )
        second = _record(
            observation=observation,
            target_event=target_event,
            actor_user=actor,
            sync_run=None,
            reason="manual review",
            request_id="request-1",
        )

        assert first.created is True
        assert second.created is False
        assert second.decision.pk == first.decision.pk

    def test_decision_key_is_deterministic_and_order_independent(self) -> None:
        observation_id = uuid.uuid4()
        target_event_id = uuid.uuid4()

        first = build_earnings_reconciliation_decision_key(
            observation_id=observation_id,
            decision_type="matched_canonical",
            status="resolved",
            target_event_id=target_event_id,
            covered_fields=("release_session", "estimated_release"),
            match_factors={"b": 2, "a": 1},
            rule_version="v1",
            supersedes_id=None,
            actor_user_id=None,
            request_id="",
        )
        second = build_earnings_reconciliation_decision_key(
            observation_id=observation_id,
            decision_type="MATCHED_CANONICAL",
            status="RESOLVED",
            target_event_id=target_event_id,
            covered_fields=("estimated_release", "estimated_release", "release_session"),
            match_factors={"a": 1, "b": 2},
            rule_version="v1",
            supersedes_id=None,
            actor_user_id=None,
            request_id="",
        )
        different_factors = build_earnings_reconciliation_decision_key(
            observation_id=observation_id,
            decision_type="matched_canonical",
            status="resolved",
            target_event_id=target_event_id,
            covered_fields=("estimated_release", "release_session"),
            match_factors={"a": 1, "b": 3},
            rule_version="v1",
            supersedes_id=None,
            actor_user_id=None,
            request_id="",
        )
        different_supersedes = build_earnings_reconciliation_decision_key(
            observation_id=observation_id,
            decision_type="matched_canonical",
            status="resolved",
            target_event_id=target_event_id,
            covered_fields=("estimated_release", "release_session"),
            match_factors={"a": 1, "b": 2},
            rule_version="v1",
            supersedes_id=uuid.uuid4(),
            actor_user_id=None,
            request_id="",
        )

        assert first == second
        assert first != different_factors
        assert first != different_supersedes

    def test_covered_fields_are_sorted_and_deduplicated(self) -> None:
        observation = make_calendar_observation()
        actor = make_user("covered-fields")

        result = _record(
            observation=observation,
            actor_user=actor,
            sync_run=None,
            reason="manual authority",
            request_id="covered-fields",
            covered_fields=("release_session", "estimated_release", "estimated_release"),
        )

        assert result.decision.covered_fields == [
            "estimated_release",
            "release_session",
        ]

    @pytest.mark.parametrize(
        "covered_fields",
        (
            "estimated_release",
            [None],
            [123],
            ["unknown_field"],
        ),
    )
    def test_invalid_covered_fields_are_rejected(self, covered_fields: object) -> None:
        observation = make_calendar_observation()
        actor = make_user("invalid-covered-fields")

        with pytest.raises(InvalidEarningsReconciliationDecision):
            _record(
                observation=observation,
                actor_user=actor,
                sync_run=None,
                reason="manual authority",
                request_id="invalid-covered-fields",
                covered_fields=covered_fields,
            )

    def test_automatic_covered_fields_are_rejected(self) -> None:
        observation = make_calendar_observation()

        with pytest.raises(InvalidEarningsReconciliationDecision):
            _record(
                observation=observation,
                covered_fields=("estimated_release",),
            )

    def test_open_and_rejected_covered_fields_are_rejected(self) -> None:
        observation = make_calendar_observation()
        actor = make_user("open-covered-fields")

        with pytest.raises(InvalidEarningsReconciliationDecision):
            _record(
                observation=observation,
                decision_type="collision",
                status="open",
                actor_user=actor,
                sync_run=None,
                reason="collision",
                request_id="collision",
                covered_fields=("estimated_release",),
            )
        with pytest.raises(InvalidEarningsReconciliationDecision):
            _record(
                observation=observation,
                decision_type="no_match",
                status="rejected",
                target_event=None,
                actor_user=actor,
                sync_run=None,
                reason="no match",
                request_id="no-match",
                covered_fields=("estimated_release",),
            )

    @pytest.mark.parametrize("match_factors", ([], "not-an-object"))
    def test_match_factors_must_be_object(self, match_factors: object) -> None:
        observation = make_calendar_observation()

        with pytest.raises(InvalidEarningsReconciliationDecision):
            _record(observation=observation, match_factors=match_factors)

    def test_credential_like_match_factors_are_rejected(self) -> None:
        observation = make_calendar_observation()

        with pytest.raises(InvalidEarningsReconciliationDecision):
            _record(
                observation=observation,
                match_factors={"api_key": "fixture-secret"},
            )

    def test_duplicate_key_with_different_reason_fails_closed(self) -> None:
        observation = make_calendar_observation()
        target_event = make_event()
        sync_run = make_sync_run("reason-mismatch")
        _record(
            observation=observation,
            target_event=target_event,
            sync_run=sync_run,
            reason="first reason",
        )

        with pytest.raises(
            EarningsReconciliationDecisionIntegrityError,
            match="different immutable data",
        ):
            _record(
                observation=observation,
                target_event=target_event,
                sync_run=sync_run,
                reason="second reason",
            )

    def test_decided_at_is_not_part_of_identity(self) -> None:
        observation = make_calendar_observation()
        target_event = make_event()
        sync_run = make_sync_run("decided-at")

        first = _record(
            observation=observation,
            target_event=target_event,
            sync_run=sync_run,
            decided_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
        )
        second = _record(
            observation=observation,
            target_event=target_event,
            sync_run=sync_run,
            decided_at=datetime(2026, 9, 20, 11, 0, tzinfo=UTC),
        )

        assert second.created is False
        assert second.decision.pk == first.decision.pk
        assert second.decision.decided_at == datetime(2026, 9, 20, 10, 0, tzinfo=UTC)

    def test_cross_observation_supersedes_is_rejected(self) -> None:
        first_observation = make_calendar_observation()
        second_observation = make_calendar_observation()
        predecessor = _record(observation=first_observation)

        with pytest.raises(InvalidEarningsReconciliationDecision, match="same observation"):
            _record(
                observation=second_observation,
                supersedes=predecessor.decision,
            )

    def test_multiple_successors_are_allowed(self) -> None:
        observation = make_calendar_observation()
        predecessor = _record(observation=observation)

        first = _record(
            observation=observation,
            supersedes=predecessor.decision,
            match_factors={"branch": "one"},
        )
        second = _record(
            observation=observation,
            supersedes=predecessor.decision,
            match_factors={"branch": "two"},
        )

        assert first.created is True
        assert second.created is True
        assert (
            EarningsReconciliationDecision.objects.filter(supersedes=predecessor.decision).count()
            == 2
        )

    def test_unknown_integrity_error_is_not_swallowed(self) -> None:
        observation = make_calendar_observation()

        with mock.patch.object(
            EarningsReconciliationDecision.objects,
            "create",
            side_effect=IntegrityError("unknown integrity failure"),
        ):
            with pytest.raises(IntegrityError, match="unknown integrity failure"):
                _record(observation=observation)

    def test_invalid_context_is_rejected_before_insert(self) -> None:
        observation = make_calendar_observation()
        actor = make_user("invalid-context")

        cases = (
            {"actor_user": None, "sync_run": None, "request_id": ""},
            {"actor_user": actor, "sync_run": None, "reason": "", "request_id": "r"},
            {"actor_user": actor, "sync_run": None, "reason": "why", "request_id": ""},
            {
                "actor_user": None,
                "sync_run": make_sync_run("invalid-context"),
                "request_id": "not-empty",
            },
        )
        for overrides in cases:
            with pytest.raises(InvalidEarningsReconciliationDecision):
                _record(observation=observation, **overrides)


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_decision_key_creates_one_row() -> None:
    observation = make_calendar_observation()
    target_event = make_event()
    first_sync_run = make_sync_run("concurrent-one")
    second_sync_run = make_sync_run("concurrent-two")
    barrier = threading.Barrier(2, timeout=10)
    results: list[object] = []
    errors: list[BaseException] = []

    def worker(sync_run) -> None:
        close_old_connections()
        try:
            barrier.wait()
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '5s'")
                results.append(
                    _record(
                        observation=observation,
                        target_event=target_event,
                        sync_run=sync_run,
                    )
                )
        except Exception as error:
            errors.append(error)
        finally:
            for current_connection in connections.all():
                current_connection.close()

    threads = [
        threading.Thread(target=worker, args=(first_sync_run,)),
        threading.Thread(target=worker, args=(second_sync_run,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    for thread in threads:
        assert not thread.is_alive(), "Concurrent reconciliation thread hung"

    assert errors == []
    assert len(results) == 2
    assert sorted(result.created for result in results) == [False, True]
    assert EarningsReconciliationDecision.objects.count() == 1
