"""Exact-only earnings candidate reconciliation and manual authority.

This module implements ADR-014 on top of the append-only decision primitive.
It deliberately has no Provider or runtime-orchestration dependency.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from django.db import transaction

from audit.models import AuditRecord, SyncRun
from audit.security import AuditSecurityError, normalize_json_without_credentials
from audit.services import record_system_action, record_user_action
from earnings.models import (
    EarningsCalendarObservation,
    EarningsDatePrecision,
    EarningsEvent,
    EarningsReconciliationDecision,
    FiscalCalendarType,
    IdentityStatus,
    ReleaseSession,
)
from earnings.services.date_changes import update_earnings_schedule
from earnings.services.promotion import EarningsPromotionCollision, promote_earnings_event
from earnings.services.reconciliation import record_earnings_reconciliation_decision

if TYPE_CHECKING:
    from accounts.models import User


EARNINGS_RECONCILIATION_VERSION = "earnings-reconciliation-v1"

_RECONCILIATION_NAMESPACE = "earnings_reconciliation"
_MANUAL_NAMESPACE = "manual_resolution"
_MARKET_TIMEZONE = ZoneInfo("America/New_York")
_MANUAL_DECISION_TYPES = frozenset(
    {"matched_candidate", "matched_canonical", "duplicate_of", "no_match"}
)
_SCHEDULE_FIELDS = frozenset({"estimated_release", "release_session"})


class EarningsReconciliationOutcome(StrEnum):
    DEFINITE_DUPLICATE = "DEFINITE_DUPLICATE"
    NOT_DUPLICATE = "NOT_DUPLICATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class EarningsReconciliationWorkflowError(ValueError):
    """Base error for the ADR-014 workflow."""


class InvalidEarningsReconciliationInput(EarningsReconciliationWorkflowError):
    pass


class EarningsReconciliationWorkflowIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EarningsReconciliationResult:
    subject: EarningsEvent
    observation: EarningsCalendarObservation
    outcome: EarningsReconciliationOutcome
    reconciliation_input_revision: str
    reconciliation_execution_key: str
    decision: EarningsReconciliationDecision
    winner: EarningsEvent | None
    candidate_ids: tuple[uuid.UUID, ...]
    conflict_codes: tuple[str, ...]
    decision_created: bool
    promoted: bool
    blocked_by_manual_authority: bool = False


@dataclass(frozen=True, slots=True)
class ManualEarningsReconciliationResult:
    observation: EarningsCalendarObservation
    decision: EarningsReconciliationDecision
    target_event: EarningsEvent | None
    decision_created: bool
    promoted: bool


@dataclass(frozen=True, slots=True)
class _CandidateContext:
    event: EarningsEvent
    observation: EarningsCalendarObservation
    lineage_decision: EarningsReconciliationDecision
    effective_decision: EarningsReconciliationDecision


@dataclass(frozen=True, slots=True)
class _Compatibility:
    conflict_codes: tuple[str, ...]
    compatible_facts: tuple[str, ...]
    schedule_changes: dict[str, object]


def reconcile_earnings_candidate(
    *,
    earnings_event: EarningsEvent,
    sync_run: SyncRun,
) -> EarningsReconciliationResult:
    """Evaluate and persist one candidate's exact-only reconciliation result."""

    event_id = _persisted_id(earnings_event, "earnings_event")
    current_run = _load_sync_run(sync_run)
    lineage = _load_single_lineage(event_id)

    with transaction.atomic():
        observation = EarningsCalendarObservation.objects.select_for_update().get(
            pk=lineage.observation_id
        )
        current = EarningsEvent.objects.select_for_update().get(pk=event_id)
        lineage = _load_single_lineage(event_id)
        if lineage.observation_id != observation.pk:
            raise EarningsReconciliationWorkflowIntegrityError(
                "Candidate lineage changed while reconciliation was starting."
            )

        if current.identity_status == IdentityStatus.CANONICAL:
            return _reuse_completed_canonical(
                current=current,
                observation=observation,
            )
        if current.identity_status != IdentityStatus.CANDIDATE:
            raise InvalidEarningsReconciliationInput(
                "reconciliation subject must be a candidate EarningsEvent."
            )

        contexts = _load_candidate_group(current)
        subject_context = _context_for_event(contexts, current.pk)
        if subject_context.observation.pk != observation.pk:
            raise EarningsReconciliationWorkflowIntegrityError(
                "Candidate group resolved a different subject observation."
            )

        if subject_context.effective_decision.actor_user_id is not None:
            return _manual_authority_result(subject_context, contexts)

        compatibility = _evaluate_compatibility(contexts, current)
        canonical = _load_canonical_owner(current)
        conflict_codes = set(compatibility.conflict_codes)
        conflict_codes.update(_source_mapping_conflicts(subject_context))
        conflict_codes.update(_effective_authority_conflicts(contexts, subject_context))

        outcome = _outcome_for(
            subject=current,
            contexts=contexts,
            canonical=canonical,
            conflict_codes=conflict_codes,
        )
        winner = _choose_winner(contexts, canonical)
        revision_payload = _build_revision_payload(
            subject_context=subject_context,
            contexts=contexts,
            canonical=canonical,
            compatibility=compatibility,
            conflict_codes=tuple(sorted(conflict_codes)),
        )
        input_revision = _sha256_json(revision_payload)
        execution_key = _sha256_json(
            {
                "version": EARNINGS_RECONCILIATION_VERSION,
                "subject_candidate_id": str(current.pk),
                "subject_observation_id": str(observation.pk),
                "ordered_candidate_ids": [str(item.event.pk) for item in contexts],
                "reconciliation_input_revision": input_revision,
            }
        )

        existing = _find_execution_decision(observation, execution_key)
        if existing is not None:
            return _result_from_existing(
                subject=current,
                observation=observation,
                contexts=contexts,
                decision=existing,
            )

        match_factors = _build_match_factors(
            subject_context=subject_context,
            contexts=contexts,
            canonical=canonical,
            outcome=outcome,
            winner=winner,
            compatibility=compatibility,
            conflict_codes=tuple(sorted(conflict_codes)),
            input_revision=input_revision,
            execution_key=execution_key,
        )
        predecessor = subject_context.effective_decision

        if outcome is EarningsReconciliationOutcome.REVIEW_REQUIRED:
            decision, created = _write_automatic_decision(
                observation=observation,
                sync_run=current_run,
                decision_type="review_required",
                status="open",
                target_event=None,
                match_factors=match_factors,
                reason=_review_reason(conflict_codes),
                predecessor=predecessor,
                execution_key=execution_key,
            )
            return EarningsReconciliationResult(
                subject=current,
                observation=observation,
                outcome=outcome,
                reconciliation_input_revision=input_revision,
                reconciliation_execution_key=execution_key,
                decision=decision,
                winner=None,
                candidate_ids=tuple(item.event.pk for item in contexts),
                conflict_codes=tuple(sorted(conflict_codes)),
                decision_created=created,
                promoted=False,
            )

        if winner is None:
            raise EarningsReconciliationWorkflowIntegrityError(
                "A resolvable reconciliation outcome requires a winner."
            )

        promoted = False
        if canonical is None and winner.pk == current.pk:
            try:
                promotion = promote_earnings_event(
                    earnings_event=winner,
                    period_end_date=_required_period_end_date(winner),
                    period_type=_required_period_type(winner),
                    sync_run=current_run,
                )
            except EarningsPromotionCollision as error:
                collision_factors = dict(match_factors)
                raw_namespace = collision_factors[_RECONCILIATION_NAMESPACE]
                if not isinstance(raw_namespace, Mapping):
                    raise EarningsReconciliationWorkflowIntegrityError(
                        "Reconciliation namespace is invalid."
                    ) from None
                namespace = dict(raw_namespace)
                namespace["outcome"] = EarningsReconciliationOutcome.REVIEW_REQUIRED.value
                namespace["reason_code"] = "canonical_collision"
                namespace["conflict_codes"] = ["CANONICAL_COLLISION"]
                namespace["canonical_collision_event_id"] = str(error.existing_canonical_id)
                collision_factors[_RECONCILIATION_NAMESPACE] = namespace
                decision, created = _write_automatic_decision(
                    observation=observation,
                    sync_run=current_run,
                    decision_type="collision",
                    status="open",
                    target_event=None,
                    match_factors=collision_factors,
                    reason="Canonical identity collision requires review.",
                    predecessor=predecessor,
                    execution_key=execution_key,
                )
                current.refresh_from_db()
                return EarningsReconciliationResult(
                    subject=current,
                    observation=observation,
                    outcome=EarningsReconciliationOutcome.REVIEW_REQUIRED,
                    reconciliation_input_revision=input_revision,
                    reconciliation_execution_key=execution_key,
                    decision=decision,
                    winner=None,
                    candidate_ids=tuple(item.event.pk for item in contexts),
                    conflict_codes=("CANONICAL_COLLISION",),
                    decision_created=created,
                    promoted=False,
                )
            winner = promotion.earnings_event
            current = winner
            promoted = promotion.changed

        if compatibility.schedule_changes:
            winner = update_earnings_schedule(
                earnings_event=winner,
                changes=compatibility.schedule_changes,
                sync_run=current_run,
            ).earnings_event
            if winner.pk == current.pk:
                current = winner

        if winner.identity_status == IdentityStatus.CANONICAL:
            match_factors = _with_canonical_target(match_factors, winner)

        decision_type = (
            "matched_canonical"
            if winner.pk == current.pk and winner.identity_status == IdentityStatus.CANONICAL
            else "duplicate_of"
        )
        decision, created = _write_automatic_decision(
            observation=observation,
            sync_run=current_run,
            decision_type=decision_type,
            status="resolved",
            target_event=winner,
            match_factors=match_factors,
            reason=(
                "Exact compatible identity reconciled to the canonical winner."
                if outcome is EarningsReconciliationOutcome.DEFINITE_DUPLICATE
                else "Complete exact identity promoted without a duplicate peer."
            ),
            predecessor=predecessor,
            execution_key=execution_key,
        )
        return EarningsReconciliationResult(
            subject=current,
            observation=observation,
            outcome=outcome,
            reconciliation_input_revision=input_revision,
            reconciliation_execution_key=execution_key,
            decision=decision,
            winner=winner,
            candidate_ids=tuple(item.event.pk for item in contexts),
            conflict_codes=tuple(sorted(conflict_codes)),
            decision_created=created,
            promoted=promoted,
        )


def resolve_earnings_reconciliation_manually(
    *,
    observation: EarningsCalendarObservation,
    decision_type: str,
    actor_user: User,
    reason: str,
    request_id: str,
    target_event: EarningsEvent | None = None,
    covered_fields: Sequence[str] = (),
    schedule_changes: Mapping[str, object] | None = None,
    promote: bool = False,
    sync_run: SyncRun | None = None,
    ip_address: str | None = None,
) -> ManualEarningsReconciliationResult:
    """Append one serialized manual decision and optional audited mutations."""

    observation_id = _persisted_id(observation, "observation")
    _persisted_id(actor_user, "actor_user")
    normalized_type = _normalize_manual_decision_type(decision_type)
    normalized_reason = _required_text(reason, "reason", 2000)
    normalized_request = _required_text(request_id, "request_id", 255)
    normalized_fields = _normalize_manual_fields(covered_fields)
    runtime_changes = dict(schedule_changes or {})
    normalized_changes = _normalize_manual_schedule(schedule_changes)
    if set(normalized_changes) != set(normalized_fields):
        raise InvalidEarningsReconciliationInput(
            "manual covered_fields must exactly match schedule_changes keys."
        )
    if normalized_type == "no_match":
        if target_event is not None or normalized_fields or promote:
            raise InvalidEarningsReconciliationInput(
                "no_match manual decisions cannot target, mutate, or promote an event."
            )
        status = "rejected"
    else:
        if target_event is None:
            raise InvalidEarningsReconciliationInput(
                "binding manual decisions require target_event."
            )
        status = "resolved"
    if promote and normalized_type != "matched_canonical":
        raise InvalidEarningsReconciliationInput(
            "manual promotion requires decision_type matched_canonical."
        )

    with transaction.atomic():
        current_observation = EarningsCalendarObservation.objects.select_for_update().get(
            pk=observation_id
        )
        existing = _find_manual_request(
            observation=current_observation,
            actor_user=actor_user,
            request_id=normalized_request,
        )
        if existing is not None:
            _verify_manual_replay(
                decision=existing,
                decision_type=normalized_type,
                target_event=target_event,
                covered_fields=normalized_fields,
                reason=normalized_reason,
                schedule_changes=normalized_changes,
                promote=promote,
            )
            return ManualEarningsReconciliationResult(
                observation=current_observation,
                decision=existing,
                target_event=existing.target_event,
                decision_created=False,
                promoted=False,
            )

        predecessor = _effective_leaf(current_observation)
        current_target = _load_manual_target(target_event) if target_event is not None else None
        _validate_manual_target(
            observation=current_observation,
            decision_type=normalized_type,
            target_event=current_target,
            promote=promote,
        )
        current_sync_run = _load_sync_run(sync_run) if sync_run is not None else None

        manual_factors: dict[str, object] = {
            _RECONCILIATION_NAMESPACE: {
                "version": EARNINGS_RECONCILIATION_VERSION,
                "outcome": (
                    EarningsReconciliationOutcome.NOT_DUPLICATE.value
                    if normalized_type == "no_match"
                    else EarningsReconciliationOutcome.DEFINITE_DUPLICATE.value
                ),
                "reason_code": "manual_resolution",
                "manual_authority_predecessor_id": str(predecessor.pk),
                "winner_id": str(current_target.pk) if current_target is not None else None,
            },
            _MANUAL_NAMESPACE: {
                "request_id": normalized_request,
                "decision_type": normalized_type,
                "target_event_id": str(current_target.pk) if current_target is not None else None,
                "covered_fields": list(normalized_fields),
                "schedule_changes": normalized_changes,
                "promote": promote,
            },
        }

        promoted = False
        if promote:
            assert current_target is not None
            if _has_other_open_review(current_target, current_observation.pk):
                raise InvalidEarningsReconciliationInput(
                    "another open group review still blocks manual promotion."
                )
            try:
                promotion = promote_earnings_event(
                    earnings_event=current_target,
                    period_end_date=_required_period_end_date(current_target),
                    period_type=_required_period_type(current_target),
                    actor_user=actor_user,
                    reason=normalized_reason,
                    request_id=normalized_request,
                    sync_run=current_sync_run,
                    ip_address=ip_address,
                )
            except EarningsPromotionCollision as error:
                normalized_type = "collision"
                status = "open"
                current_target = None
                normalized_fields = ()
                normalized_changes = {}
                runtime_changes = {}
                manual_factors[_RECONCILIATION_NAMESPACE] = {
                    "version": EARNINGS_RECONCILIATION_VERSION,
                    "outcome": EarningsReconciliationOutcome.REVIEW_REQUIRED.value,
                    "reason_code": "canonical_collision",
                    "conflict_codes": ["CANONICAL_COLLISION"],
                    "canonical_collision_event_id": str(error.existing_canonical_id),
                    "manual_authority_predecessor_id": str(predecessor.pk),
                }
            else:
                current_target = promotion.earnings_event
                promoted = promotion.changed
                manual_factors = _with_canonical_target(manual_factors, current_target)

        if runtime_changes:
            assert current_target is not None
            current_target = update_earnings_schedule(
                earnings_event=current_target,
                changes=runtime_changes,
                actor_user=actor_user,
                reason=normalized_reason,
                request_id=normalized_request,
                sync_run=current_sync_run,
                ip_address=ip_address,
            ).earnings_event

        write_result = record_earnings_reconciliation_decision(
            observation=current_observation,
            decision_type=normalized_type,
            status=status,
            rule_version=EARNINGS_RECONCILIATION_VERSION,
            target_event=current_target,
            covered_fields=normalized_fields,
            match_factors=manual_factors,
            reason=normalized_reason,
            actor_user=actor_user,
            sync_run=current_sync_run,
            request_id=normalized_request,
            supersedes=predecessor,
        )
        decision = write_result.decision
        _record_manual_decision_audit(
            decision=decision,
            predecessor=predecessor,
            actor_user=actor_user,
            reason=normalized_reason,
            request_id=normalized_request,
            sync_run=current_sync_run,
            ip_address=ip_address,
        )
        return ManualEarningsReconciliationResult(
            observation=current_observation,
            decision=decision,
            target_event=current_target,
            decision_created=write_result.created,
            promoted=promoted,
        )


def _load_candidate_group(subject: EarningsEvent) -> tuple[_CandidateContext, ...]:
    event_ids: tuple[uuid.UUID, ...]
    if subject.period_end_date is None or subject.period_type is None:
        event_ids = (subject.pk,)
    else:
        event_ids = tuple(
            EarningsEvent.objects.filter(
                identity_status=IdentityStatus.CANDIDATE,
                company_id=subject.company_id,
                period_end_date=subject.period_end_date,
                period_type=subject.period_type,
            )
            .order_by("id")
            .values_list("id", flat=True)
        )
    contexts = tuple(_load_candidate_context(event_id) for event_id in event_ids)
    if not contexts or all(item.event.pk != subject.pk for item in contexts):
        raise EarningsReconciliationWorkflowIntegrityError(
            "Candidate reconciliation group does not contain the subject."
        )
    return tuple(sorted(contexts, key=lambda item: str(item.event.pk)))


def _load_candidate_context(event_id: uuid.UUID) -> _CandidateContext:
    event = EarningsEvent.objects.get(pk=event_id)
    lineage = _load_single_lineage(event_id)
    observation = EarningsCalendarObservation.objects.get(pk=lineage.observation_id)
    return _CandidateContext(
        event=event,
        observation=observation,
        lineage_decision=lineage,
        effective_decision=_effective_leaf(observation),
    )


def _load_single_lineage(event_id: uuid.UUID) -> EarningsReconciliationDecision:
    decisions = list(
        EarningsReconciliationDecision.objects.filter(
            target_event_id=event_id,
            decision_type="created_candidate",
            status="resolved",
        ).order_by("id")
    )
    if len(decisions) != 1:
        raise InvalidEarningsReconciliationInput(
            "reconciliation subject must have exactly one created_candidate lineage."
        )
    return cast(EarningsReconciliationDecision, decisions[0])


def _effective_leaf(
    observation: EarningsCalendarObservation,
) -> EarningsReconciliationDecision:
    decisions = list(
        EarningsReconciliationDecision.objects.filter(observation=observation).order_by("id")
    )
    if not decisions:
        raise EarningsReconciliationWorkflowIntegrityError(
            "Reconciliation observation has no decision history."
        )
    superseded_ids = {item.supersedes_id for item in decisions if item.supersedes_id is not None}
    leaves = [item for item in decisions if item.pk not in superseded_ids]
    if len(leaves) != 1:
        raise EarningsReconciliationWorkflowIntegrityError(
            "Reconciliation decision history has multiple effective leaves."
        )
    return cast(EarningsReconciliationDecision, leaves[0])


def _context_for_event(
    contexts: tuple[_CandidateContext, ...],
    event_id: uuid.UUID,
) -> _CandidateContext:
    for context in contexts:
        if context.event.pk == event_id:
            return context
    raise EarningsReconciliationWorkflowIntegrityError("Subject candidate context is missing.")


def _load_canonical_owner(subject: EarningsEvent) -> EarningsEvent | None:
    if subject.period_end_date is None or subject.period_type is None:
        return None
    return (
        EarningsEvent.objects.filter(
            identity_status=IdentityStatus.CANONICAL,
            company_id=subject.company_id,
            period_end_date=subject.period_end_date,
            period_type=subject.period_type,
        )
        .order_by("id")
        .first()
    )


def _evaluate_compatibility(
    contexts: tuple[_CandidateContext, ...],
    subject: EarningsEvent,
) -> _Compatibility:
    events = [item.event for item in contexts]
    canonical = _load_canonical_owner(subject)
    if canonical is not None:
        events.append(canonical)
    conflicts: set[str] = set()
    compatible: set[str] = set()

    if subject.period_end_date is None or subject.period_type is None:
        conflicts.add("IDENTITY_INCOMPLETE")

    _compare_scalar(
        events,
        "fiscal_calendar_type",
        unknown_values={FiscalCalendarType.UNKNOWN},
        conflict_code="FISCAL_CALENDAR_CONFLICT",
        compatible=compatible,
        conflicts=conflicts,
    )
    calendars = {event.fiscal_calendar_type for event in events}
    if FiscalCalendarType.WEEK_BASED_52_53 in calendars and any(
        event.fiscal_calendar_type == FiscalCalendarType.WEEK_BASED_52_53
        and event.period_length_weeks not in (52, 53)
        for event in events
    ):
        conflicts.add("WEEK_LENGTH_MISSING")
    _compare_scalar(
        events,
        "period_length_weeks",
        unknown_values={None},
        conflict_code="PERIOD_LENGTH_CONFLICT",
        compatible=compatible,
        conflicts=conflicts,
    )
    _compare_scalar(
        events,
        "fiscal_year",
        unknown_values={None},
        conflict_code="FISCAL_YEAR_CONFLICT",
        compatible=compatible,
        conflicts=conflicts,
    )
    _compare_scalar(
        events,
        "status",
        unknown_values=set(),
        conflict_code="STATUS_CONFLICT",
        compatible=compatible,
        conflicts=conflicts,
    )

    estimated_value, estimated_conflict = _merge_schedule_facts(
        [_schedule_fact(event, "estimated_release") for event in events]
    )
    if estimated_conflict:
        conflicts.add("ESTIMATED_RELEASE_CONFLICT")
    elif estimated_value is not None:
        compatible.add("estimated_release")

    _compare_scalar(
        events,
        "release_session",
        unknown_values={ReleaseSession.UNKNOWN},
        conflict_code="RELEASE_SESSION_CONFLICT",
        compatible=compatible,
        conflicts=conflicts,
    )
    session_values = {
        event.release_session for event in events if event.release_session != ReleaseSession.UNKNOWN
    }

    for field_name in ("confirmed_release", "earnings_release", "conference_call"):
        _value, conflict = _merge_schedule_facts(
            [_schedule_fact(event, field_name) for event in events]
        )
        if conflict:
            conflicts.add(f"{field_name.upper()}_CONFLICT")

    changes: dict[str, object] = {}
    if estimated_value is not None:
        changes["estimated_release"] = estimated_value
    if len(session_values) == 1:
        changes["release_session"] = next(iter(session_values))
    return _Compatibility(
        conflict_codes=tuple(sorted(conflicts)),
        compatible_facts=tuple(sorted(compatible)),
        schedule_changes=changes,
    )


def _compare_scalar(
    events: list[EarningsEvent],
    field_name: str,
    *,
    unknown_values: set[object],
    conflict_code: str,
    compatible: set[str],
    conflicts: set[str],
) -> None:
    known = {getattr(event, field_name) for event in events} - unknown_values
    if len(known) > 1:
        conflicts.add(conflict_code)
    elif known:
        compatible.add(field_name)


def _schedule_fact(event: EarningsEvent, prefix: str) -> tuple[str, object] | None:
    precision = getattr(event, f"{prefix}_precision")
    if precision == EarningsDatePrecision.UNKNOWN:
        return None
    if precision == EarningsDatePrecision.DATE_ONLY:
        return ("date", getattr(event, f"{prefix}_date"))
    value = getattr(event, f"{prefix}_at")
    return ("datetime", value)


def _merge_schedule_facts(
    facts: list[tuple[str, object] | None],
) -> tuple[object | None, bool]:
    known = [fact for fact in facts if fact is not None]
    if not known:
        return None, False
    dates = {_business_date(fact) for fact in known}
    if len(dates) > 1:
        return None, True
    datetimes = {fact[1] for fact in known if fact[0] == "datetime"}
    if len(datetimes) > 1:
        return None, True
    if datetimes:
        return next(iter(datetimes)), False
    date_values = {fact[1] for fact in known}
    if len(date_values) != 1:
        return None, True
    return next(iter(date_values)), False


def _business_date(fact: tuple[str, object]) -> date:
    kind, value = fact
    if kind == "date" and isinstance(value, date) and not isinstance(value, datetime):
        return value
    if kind == "datetime" and isinstance(value, datetime):
        return value.astimezone(_MARKET_TIMEZONE).date()
    raise EarningsReconciliationWorkflowIntegrityError(
        "Persisted schedule precision does not match its value."
    )


def _source_mapping_conflicts(subject: _CandidateContext) -> set[str]:
    decisions = EarningsReconciliationDecision.objects.filter(
        decision_type="created_candidate",
        status="resolved",
        observation__source_id=subject.observation.source_id,
        observation__provider_event_id=subject.observation.provider_event_id,
    ).select_related("target_event")
    subject_identity = _event_identity(subject.event)
    for decision in decisions:
        target = decision.target_event
        if target is None or target.pk == subject.event.pk:
            continue
        if _event_identity(target) != subject_identity:
            return {"SOURCE_MAPPING_CONFLICT"}
    return set()


def _effective_authority_conflicts(
    contexts: tuple[_CandidateContext, ...],
    subject: _CandidateContext,
) -> set[str]:
    conflicts: set[str] = set()
    for context in contexts:
        leaf = context.effective_decision
        if context.event.pk != subject.event.pk and leaf.actor_user_id is not None:
            conflicts.add("MANUAL_AUTHORITY")
        if leaf.status == "open" and leaf.pk != subject.effective_decision.pk:
            conflicts.add("OPEN_REVIEW")
    return conflicts


def _outcome_for(
    *,
    subject: EarningsEvent,
    contexts: tuple[_CandidateContext, ...],
    canonical: EarningsEvent | None,
    conflict_codes: set[str],
) -> EarningsReconciliationOutcome:
    if conflict_codes:
        return EarningsReconciliationOutcome.REVIEW_REQUIRED
    if subject.period_end_date is None or subject.period_type is None:
        return EarningsReconciliationOutcome.REVIEW_REQUIRED
    if canonical is not None or len(contexts) > 1:
        return EarningsReconciliationOutcome.DEFINITE_DUPLICATE
    return EarningsReconciliationOutcome.NOT_DUPLICATE


def _choose_winner(
    contexts: tuple[_CandidateContext, ...],
    canonical: EarningsEvent | None,
) -> EarningsEvent | None:
    if canonical is not None:
        return canonical
    if not contexts:
        return None
    best_vector = max(_completeness_vector(item.event) for item in contexts)
    eligible = [item.event for item in contexts if _completeness_vector(item.event) == best_vector]
    return min(eligible, key=lambda event: str(event.pk))


def _completeness_vector(event: EarningsEvent) -> tuple[int, int, int, int, int]:
    return (
        int(event.fiscal_calendar_type != FiscalCalendarType.UNKNOWN),
        int(event.period_length_weeks is not None),
        int(event.fiscal_year is not None),
        int(event.estimated_release_precision != EarningsDatePrecision.UNKNOWN),
        int(event.release_session != ReleaseSession.UNKNOWN),
    )


def _build_revision_payload(
    *,
    subject_context: _CandidateContext,
    contexts: tuple[_CandidateContext, ...],
    canonical: EarningsEvent | None,
    compatibility: _Compatibility,
    conflict_codes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "version": EARNINGS_RECONCILIATION_VERSION,
        "subject_candidate_id": str(subject_context.event.pk),
        "subject_observation_id": str(subject_context.observation.pk),
        "candidate_group_key": _group_key(subject_context.event),
        "candidates": [_candidate_fact(context) for context in contexts],
        "canonical": _plain_event_fact(canonical) if canonical is not None else None,
        "compatible_facts": list(compatibility.compatible_facts),
        "conflict_codes": list(conflict_codes),
        "effective_manual_decision_ids": sorted(
            str(context.effective_decision.pk)
            for context in contexts
            if context.effective_decision.actor_user_id is not None
        ),
    }


def _candidate_fact(context: _CandidateContext) -> dict[str, object]:
    fact = _plain_event_fact(context.event)
    fact.update(
        {
            "observation_id": str(context.observation.pk),
            "source_id": str(context.observation.source_id),
            "provider_key": context.observation.provider_key,
            "provider_event_id": context.observation.provider_event_id,
            "raw_data_record_id": str(context.observation.raw_data_record_id),
            "source_evidence_id": (
                str(context.event.source_evidence_id)
                if context.event.source_evidence_id is not None
                else None
            ),
        }
    )
    return fact


def _plain_event_fact(event: EarningsEvent) -> dict[str, object]:
    return {
        "event_id": str(event.pk),
        "company_id": str(event.company_id),
        "period_end_date": event.period_end_date.isoformat() if event.period_end_date else None,
        "period_type": event.period_type,
        "fiscal_calendar_type": event.fiscal_calendar_type,
        "period_length_weeks": event.period_length_weeks,
        "fiscal_year": event.fiscal_year,
        "estimated_release": _json_schedule_fact(_schedule_fact(event, "estimated_release")),
        "release_session": event.release_session,
        "confirmed_release": _json_schedule_fact(_schedule_fact(event, "confirmed_release")),
        "earnings_release": _json_schedule_fact(_schedule_fact(event, "earnings_release")),
        "conference_call": _json_schedule_fact(_schedule_fact(event, "conference_call")),
        "status": event.status,
    }


def _json_schedule_fact(fact: tuple[str, object] | None) -> object:
    if fact is None:
        return None
    kind, value = fact
    if isinstance(value, datetime):
        normalized_value: object = value.isoformat()
    elif isinstance(value, date):
        normalized_value = value.isoformat()
    else:
        normalized_value = value
    return {"kind": kind, "value": normalized_value}


def _build_match_factors(
    *,
    subject_context: _CandidateContext,
    contexts: tuple[_CandidateContext, ...],
    canonical: EarningsEvent | None,
    outcome: EarningsReconciliationOutcome,
    winner: EarningsEvent | None,
    compatibility: _Compatibility,
    conflict_codes: tuple[str, ...],
    input_revision: str,
    execution_key: str,
) -> dict[str, object]:
    loser_ids = [
        str(item.event.pk) for item in contexts if winner is not None and item.event.pk != winner.pk
    ]
    return {
        _RECONCILIATION_NAMESPACE: {
            "version": EARNINGS_RECONCILIATION_VERSION,
            "reconciliation_execution_key": execution_key,
            "reconciliation_input_revision": input_revision,
            "outcome": outcome.value,
            "reason_code": _reason_code(outcome, conflict_codes),
            "candidate_group_key": _group_key(subject_context.event),
            "subject_candidate_id": str(subject_context.event.pk),
            "subject_observation_id": str(subject_context.observation.pk),
            "ordered_candidate_ids": [str(item.event.pk) for item in contexts],
            "ordered_observation_ids": [str(item.observation.pk) for item in contexts],
            "source_identity": {
                "source_id": str(subject_context.observation.source_id),
                "provider_key": subject_context.observation.provider_key,
                "provider_event_id": subject_context.observation.provider_event_id,
            },
            "period_facts": _plain_event_fact(subject_context.event),
            "compatible_facts": list(compatibility.compatible_facts),
            "conflict_codes": list(conflict_codes),
            "winner_id": str(winner.pk) if winner is not None else None,
            "loser_ids": loser_ids,
            "canonical_target_id": str(canonical.pk) if canonical is not None else None,
            "source_evidence_ids": sorted(
                str(item.event.source_evidence_id)
                for item in contexts
                if item.event.source_evidence_id is not None
            ),
            "manual_authority_predecessor_id": (
                str(subject_context.effective_decision.pk)
                if subject_context.effective_decision.actor_user_id is not None
                else None
            ),
        }
    }


def _with_canonical_target(
    match_factors: dict[str, object],
    canonical: EarningsEvent,
) -> dict[str, object]:
    updated = dict(match_factors)
    raw_namespace = updated.get(_RECONCILIATION_NAMESPACE)
    if not isinstance(raw_namespace, Mapping):
        raise EarningsReconciliationWorkflowIntegrityError("Reconciliation namespace is invalid.")
    namespace = dict(raw_namespace)
    namespace["canonical_target_id"] = str(canonical.pk)
    updated[_RECONCILIATION_NAMESPACE] = namespace
    return updated


def _write_automatic_decision(
    *,
    observation: EarningsCalendarObservation,
    sync_run: SyncRun,
    decision_type: str,
    status: str,
    target_event: EarningsEvent | None,
    match_factors: Mapping[str, object],
    reason: str,
    predecessor: EarningsReconciliationDecision,
    execution_key: str,
) -> tuple[EarningsReconciliationDecision, bool]:
    write_result = record_earnings_reconciliation_decision(
        observation=observation,
        decision_type=decision_type,
        status=status,
        rule_version=EARNINGS_RECONCILIATION_VERSION,
        target_event=target_event,
        match_factors=match_factors,
        reason=reason,
        sync_run=sync_run,
        supersedes=predecessor,
    )
    decision = write_result.decision
    record_system_action(
        sync_run=sync_run,
        action=AuditRecord.Action.CREATE,
        target_type=AuditRecord.TargetType.EARNINGS_RECONCILIATION_DECISION,
        target_id=decision.pk,
        before={"predecessor_decision_id": str(predecessor.pk)},
        after={
            "decision_type": decision.decision_type,
            "status": decision.status,
            "target_event_id": str(decision.target_event_id) if decision.target_event_id else None,
            "decision_key": decision.decision_key,
        },
        reason=reason,
        request_id=f"earnings-reconciliation:{execution_key}",
    )
    return decision, write_result.created


def _record_manual_decision_audit(
    *,
    decision: EarningsReconciliationDecision,
    predecessor: EarningsReconciliationDecision,
    actor_user: User,
    reason: str,
    request_id: str,
    sync_run: SyncRun | None,
    ip_address: str | None,
) -> None:
    record_user_action(
        actor_user=actor_user,
        action=AuditRecord.Action.MANUAL_CORRECTION,
        target_type=AuditRecord.TargetType.EARNINGS_RECONCILIATION_DECISION,
        target_id=decision.pk,
        before={"predecessor_decision_id": str(predecessor.pk)},
        after={
            "decision_type": decision.decision_type,
            "status": decision.status,
            "target_event_id": str(decision.target_event_id) if decision.target_event_id else None,
            "decision_key": decision.decision_key,
            "covered_fields": decision.covered_fields,
        },
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        sync_run=sync_run,
    )


def _manual_authority_result(
    subject: _CandidateContext,
    contexts: tuple[_CandidateContext, ...],
) -> EarningsReconciliationResult:
    decision = subject.effective_decision
    namespace = decision.match_factors.get(_RECONCILIATION_NAMESPACE, {})
    if not isinstance(namespace, dict):
        namespace = {}
    raw_outcome = namespace.get("outcome")
    try:
        outcome = EarningsReconciliationOutcome(str(raw_outcome))
    except ValueError:
        outcome = (
            EarningsReconciliationOutcome.NOT_DUPLICATE
            if decision.status == "rejected"
            else EarningsReconciliationOutcome.DEFINITE_DUPLICATE
        )
    return EarningsReconciliationResult(
        subject=subject.event,
        observation=subject.observation,
        outcome=outcome,
        reconciliation_input_revision=str(namespace.get("reconciliation_input_revision", "")),
        reconciliation_execution_key=str(namespace.get("reconciliation_execution_key", "")),
        decision=decision,
        winner=decision.target_event,
        candidate_ids=tuple(item.event.pk for item in contexts),
        conflict_codes=(),
        decision_created=False,
        promoted=False,
        blocked_by_manual_authority=True,
    )


def _reuse_completed_canonical(
    *,
    current: EarningsEvent,
    observation: EarningsCalendarObservation,
) -> EarningsReconciliationResult:
    leaf = _effective_leaf(observation)
    if leaf.actor_user_id is not None:
        namespace = leaf.match_factors.get(_RECONCILIATION_NAMESPACE, {})
        if not isinstance(namespace, dict):
            namespace = {}
        raw_outcome = namespace.get(
            "outcome", EarningsReconciliationOutcome.DEFINITE_DUPLICATE.value
        )
        return EarningsReconciliationResult(
            subject=current,
            observation=observation,
            outcome=EarningsReconciliationOutcome(str(raw_outcome)),
            reconciliation_input_revision=str(namespace.get("reconciliation_input_revision", "")),
            reconciliation_execution_key=str(namespace.get("reconciliation_execution_key", "")),
            decision=leaf,
            winner=leaf.target_event,
            candidate_ids=(current.pk,),
            conflict_codes=(),
            decision_created=False,
            promoted=False,
            blocked_by_manual_authority=True,
        )
    if leaf.rule_version != EARNINGS_RECONCILIATION_VERSION or leaf.target_event_id != current.pk:
        raise InvalidEarningsReconciliationInput(
            "canonical event is not the result of a completed 4.2E reconciliation."
        )
    namespace = leaf.match_factors.get(_RECONCILIATION_NAMESPACE)
    if not isinstance(namespace, dict):
        raise EarningsReconciliationWorkflowIntegrityError(
            "Completed reconciliation decision is missing its controlled match factors."
        )
    return EarningsReconciliationResult(
        subject=current,
        observation=observation,
        outcome=EarningsReconciliationOutcome(str(namespace["outcome"])),
        reconciliation_input_revision=str(namespace["reconciliation_input_revision"]),
        reconciliation_execution_key=str(namespace["reconciliation_execution_key"]),
        decision=leaf,
        winner=current,
        candidate_ids=tuple(uuid.UUID(value) for value in namespace["ordered_candidate_ids"]),
        conflict_codes=tuple(str(value) for value in namespace["conflict_codes"]),
        decision_created=False,
        promoted=False,
    )


def _find_execution_decision(
    observation: EarningsCalendarObservation,
    execution_key: str,
) -> EarningsReconciliationDecision | None:
    matches: list[EarningsReconciliationDecision] = []
    for decision in EarningsReconciliationDecision.objects.filter(
        observation=observation,
        rule_version=EARNINGS_RECONCILIATION_VERSION,
    ).select_related("target_event"):
        namespace = decision.match_factors.get(_RECONCILIATION_NAMESPACE)
        if (
            isinstance(namespace, dict)
            and namespace.get("reconciliation_execution_key") == execution_key
        ):
            matches.append(decision)
    if len(matches) > 1:
        raise EarningsReconciliationWorkflowIntegrityError(
            "One reconciliation execution has multiple decisions."
        )
    return matches[0] if matches else None


def _result_from_existing(
    *,
    subject: EarningsEvent,
    observation: EarningsCalendarObservation,
    contexts: tuple[_CandidateContext, ...],
    decision: EarningsReconciliationDecision,
) -> EarningsReconciliationResult:
    namespace = decision.match_factors[_RECONCILIATION_NAMESPACE]
    if not isinstance(namespace, dict):
        raise EarningsReconciliationWorkflowIntegrityError("Reconciliation namespace is invalid.")
    return EarningsReconciliationResult(
        subject=subject,
        observation=observation,
        outcome=EarningsReconciliationOutcome(str(namespace["outcome"])),
        reconciliation_input_revision=str(namespace["reconciliation_input_revision"]),
        reconciliation_execution_key=str(namespace["reconciliation_execution_key"]),
        decision=decision,
        winner=decision.target_event,
        candidate_ids=tuple(item.event.pk for item in contexts),
        conflict_codes=tuple(str(value) for value in namespace["conflict_codes"]),
        decision_created=False,
        promoted=False,
    )


def _find_manual_request(
    *,
    observation: EarningsCalendarObservation,
    actor_user: User,
    request_id: str,
) -> EarningsReconciliationDecision | None:
    matches = list(
        EarningsReconciliationDecision.objects.filter(
            observation=observation,
            actor_user=actor_user,
            request_id=request_id,
        ).select_related("target_event")
    )
    if len(matches) > 1:
        raise EarningsReconciliationWorkflowIntegrityError(
            "One manual request identity has multiple decisions."
        )
    return matches[0] if matches else None


def _verify_manual_replay(
    *,
    decision: EarningsReconciliationDecision,
    decision_type: str,
    target_event: EarningsEvent | None,
    covered_fields: tuple[str, ...],
    reason: str,
    schedule_changes: dict[str, object],
    promote: bool,
) -> None:
    namespace = decision.match_factors.get(_MANUAL_NAMESPACE)
    expected_target_id = str(target_event.pk) if target_event is not None else None
    if (
        not isinstance(namespace, dict)
        or namespace.get("decision_type") != decision_type
        or namespace.get("target_event_id") != expected_target_id
        or namespace.get("covered_fields") != list(covered_fields)
        or namespace.get("schedule_changes") != schedule_changes
        or namespace.get("promote") is not promote
        or decision.reason != reason
    ):
        raise EarningsReconciliationWorkflowIntegrityError(
            "Manual request identity was reused with different immutable facts."
        )


def _load_manual_target(target_event: EarningsEvent) -> EarningsEvent:
    event_id = _persisted_id(target_event, "target_event")
    try:
        return EarningsEvent.objects.select_for_update().get(pk=event_id)
    except EarningsEvent.DoesNotExist as error:
        raise InvalidEarningsReconciliationInput("target_event no longer exists.") from error


def _validate_manual_target(
    *,
    observation: EarningsCalendarObservation,
    decision_type: str,
    target_event: EarningsEvent | None,
    promote: bool,
) -> None:
    if target_event is None:
        return
    lineage = _load_single_lineage_for_observation(observation)
    subject = lineage.target_event
    if subject is None:
        raise InvalidEarningsReconciliationInput(
            "manual reconciliation requires candidate lineage for the observation."
        )
    if target_event.company_id != subject.company_id:
        raise InvalidEarningsReconciliationInput(
            "manual reconciliation cannot change the candidate Company."
        )
    if target_event.pk != subject.pk and _event_identity(target_event) != _event_identity(subject):
        raise InvalidEarningsReconciliationInput(
            "manual duplicate target must have the same exact period identity."
        )
    if decision_type == "matched_canonical" and not promote:
        if target_event.identity_status != IdentityStatus.CANONICAL:
            raise InvalidEarningsReconciliationInput(
                "matched_canonical target must already be canonical or be promoted atomically."
            )


def _load_single_lineage_for_observation(
    observation: EarningsCalendarObservation,
) -> EarningsReconciliationDecision:
    decisions = list(
        EarningsReconciliationDecision.objects.select_related("target_event").filter(
            observation=observation,
            decision_type="created_candidate",
            status="resolved",
        )
    )
    if len(decisions) != 1:
        raise InvalidEarningsReconciliationInput(
            "manual reconciliation requires exactly one created_candidate lineage."
        )
    return cast(EarningsReconciliationDecision, decisions[0])


def _has_other_open_review(target: EarningsEvent, observation_id: uuid.UUID) -> bool:
    contexts = _load_candidate_group(target)
    return any(
        context.observation.pk != observation_id and context.effective_decision.status == "open"
        for context in contexts
    )


def _normalize_manual_decision_type(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsReconciliationInput("decision_type must be a string.")
    normalized = value.strip().lower()
    if normalized not in _MANUAL_DECISION_TYPES:
        raise InvalidEarningsReconciliationInput("manual decision_type is not allowed by ADR-014.")
    return normalized


def _normalize_manual_fields(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str | bytes | bytearray):
        raise InvalidEarningsReconciliationInput("covered_fields must be a sequence.")
    normalized = {str(item).strip().lower() for item in value}
    if not normalized.issubset(_SCHEDULE_FIELDS):
        raise InvalidEarningsReconciliationInput(
            "manual authority only covers estimated_release and release_session."
        )
    return tuple(sorted(normalized))


def _normalize_manual_schedule(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidEarningsReconciliationInput("schedule_changes must be a mapping.")
    if not set(value).issubset(_SCHEDULE_FIELDS):
        raise InvalidEarningsReconciliationInput(
            "manual schedule changes contain an unauthorized field."
        )
    canonical = {key: _json_manual_value(raw_value) for key, raw_value in sorted(value.items())}
    try:
        normalized = normalize_json_without_credentials(
            canonical,
            value_name="manual schedule changes",
        )
    except AuditSecurityError as error:
        raise InvalidEarningsReconciliationInput(str(error)) from None
    if not isinstance(normalized, dict):
        raise InvalidEarningsReconciliationInput("schedule_changes must normalize to an object.")
    return normalized


def _json_manual_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _event_identity(event: EarningsEvent) -> tuple[uuid.UUID, date | None, str | None]:
    return event.company_id, event.period_end_date, event.period_type


def _group_key(event: EarningsEvent) -> dict[str, object]:
    return {
        "company_id": str(event.company_id),
        "period_end_date": event.period_end_date.isoformat() if event.period_end_date else None,
        "period_type": event.period_type,
    }


def _reason_code(
    outcome: EarningsReconciliationOutcome,
    conflict_codes: tuple[str, ...],
) -> str:
    if conflict_codes:
        return conflict_codes[0].lower()
    if outcome is EarningsReconciliationOutcome.DEFINITE_DUPLICATE:
        return "exact_compatible_duplicate"
    if outcome is EarningsReconciliationOutcome.NOT_DUPLICATE:
        return "single_exact_identity"
    return "review_required"


def _review_reason(conflict_codes: set[str]) -> str:
    if not conflict_codes:
        return "Exact reconciliation requires manual review."
    return "Exact reconciliation requires review: " + ", ".join(sorted(conflict_codes)) + "."


def _required_period_end_date(event: EarningsEvent) -> date:
    if event.period_end_date is None:
        raise InvalidEarningsReconciliationInput("promotion requires period_end_date.")
    return event.period_end_date


def _required_period_type(event: EarningsEvent) -> str:
    if event.period_type is None:
        raise InvalidEarningsReconciliationInput("promotion requires period_type.")
    return event.period_type


def _load_sync_run(sync_run: SyncRun) -> SyncRun:
    run_id = _persisted_id(sync_run, "sync_run")
    try:
        return SyncRun.objects.get(pk=run_id)
    except SyncRun.DoesNotExist as error:
        raise InvalidEarningsReconciliationInput("sync_run no longer exists.") from error


def _persisted_id(instance: object, value_name: str) -> uuid.UUID:
    state = getattr(instance, "_state", None)
    pk = getattr(instance, "pk", None)
    if state is None or getattr(state, "adding", True) or not isinstance(pk, uuid.UUID):
        raise InvalidEarningsReconciliationInput(f"{value_name} must be persisted.")
    return pk


def _required_text(value: object, value_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsReconciliationInput(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise InvalidEarningsReconciliationInput(
            f"{value_name} must contain 1 to {max_length} characters."
        )
    return normalized


def _sha256_json(value: object) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()
