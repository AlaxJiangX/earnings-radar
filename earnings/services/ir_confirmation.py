"""IR authority, confirmation, release, cancellation and conflict evaluation.

Stage 4.5B fixture-first implementation of ADR-022.  This service consumes
persisted InvestorRelationsObservation rows, resolves the unique canonical
EarningsEvent by exact period identity, applies the field authority matrix and
writes schedule/status changes only through the existing earnings services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from django.db import transaction

from audit.models import AuditRecord, DataChange, RawDataObservation, SourceEvidence, SyncRun
from audit.services import record_source_evidence, record_system_action, record_user_action
from earnings.models import (
    EarningsDatePrecision,
    EarningsEvent,
    EarningsReconciliationDecision,
    EventStatus,
    IdentityStatus,
    InvestorRelationsDecision,
    InvestorRelationsItemType,
    InvestorRelationsObservation,
    ReleaseSession,
)
from earnings.services.date_changes import (
    MARKET_TIMEZONE,
    EarningsScheduleWriteResult,
    update_earnings_schedule,
)
from earnings.services.ir_decision import (
    IR_DECISION_RULE_VERSION,
    IR_EVIDENCE_NORMALIZER_VERSION,
    InvestorRelationsDecisionWriteResult,
    record_investor_relations_decision,
)
from earnings.services.lifecycle import (
    EarningsStatusWriteResult,
    cancel_earnings_event,
    confirm_earnings_event,
    mark_earnings_released,
)

if TYPE_CHECKING:
    from accounts.models import User

_MATCH_FACTORS_NAMESPACE = "investor_relations"
_PRECISION_RANK: dict[str, int] = {
    EarningsDatePrecision.UNKNOWN: 0,
    EarningsDatePrecision.DATE_ONLY: 1,
    EarningsDatePrecision.EXACT_DATETIME: 2,
}
_DATE_FIELDS = frozenset(
    {"estimated_release", "confirmed_release", "earnings_release", "conference_call"}
)


class InvestorRelationsConfirmationError(ValueError):
    """Base error for IR confirmation evaluation."""


class InvalidInvestorRelationsConfirmation(InvestorRelationsConfirmationError):
    pass


class InvestorRelationsConfirmationIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InvestorRelationsEvaluationResult:
    observation: InvestorRelationsObservation
    decision: InvestorRelationsDecision
    decision_created: bool
    event: EarningsEvent | None
    outcome: str
    schedule_result: EarningsScheduleWriteResult | None
    status_result: EarningsStatusWriteResult | None
    blocked_by_manual_authority: bool


@dataclass(frozen=True, slots=True)
class _PlannedFields:
    fields: tuple[str, ...]
    target_status: str | None
    decision_type: str
    outcome: str


@dataclass(frozen=True, slots=True)
class _Resolution:
    decision_type: str
    status: str
    outcome: str
    reason: str
    match_factors: dict[str, object]


def evaluate_investor_relations_observation(
    *,
    observation: InvestorRelationsObservation,
    sync_run: SyncRun,
) -> InvestorRelationsEvaluationResult:
    """Automatically evaluate one persisted IR observation.

    Same-authority IR conflicts and precision regressions fail closed into an
    open ``conflict`` decision without mutating the EarningsEvent.  A manual
    authority leaf blocks the automatic write and is recorded as ``ignored``.
    """

    with transaction.atomic():
        current_observation = _load_observation(observation)
        current_run = _load_run(sync_run)
        _validate_observation_run_lineage(
            observation=current_observation,
            sync_run=current_run,
        )
        event, resolution = _resolve_canonical_event(current_observation)
        if event is None:
            if resolution is None:
                raise InvestorRelationsConfirmationIntegrityError(
                    "IR event resolution produced no outcome."
                )
            decision_result = _record_decision(
                observation=current_observation,
                sync_run=current_run,
                decision_type=resolution.decision_type,
                status=resolution.status,
                covered_fields=(),
                match_factors=resolution.match_factors,
                reason=resolution.reason,
                target_event=None,
            )
            return InvestorRelationsEvaluationResult(
                observation=current_observation,
                decision=decision_result.decision,
                decision_created=decision_result.created,
                event=None,
                outcome=resolution.outcome,
                schedule_result=None,
                status_result=None,
                blocked_by_manual_authority=False,
            )

        if event.status == EventStatus.CANCELLED and (
            current_observation.item_type != InvestorRelationsItemType.CANCELLATION
        ):
            return _record_conflict(
                observation=current_observation,
                sync_run=current_run,
                event=event,
                reason_code="CANCELLED_CANONICAL_EVENT",
            )
        if event.status == EventStatus.RELEASED and (
            current_observation.item_type
            in {
                InvestorRelationsItemType.RELEASE_CONFIRMATION,
                InvestorRelationsItemType.CALL_NOTICE,
                InvestorRelationsItemType.CANCELLATION,
            }
        ):
            return _record_conflict(
                observation=current_observation,
                sync_run=current_run,
                event=event,
                reason_code="EVENT_ALREADY_RELEASED",
            )

        planned = _plan_fields(current_observation)
        if planned.outcome == "call_cancellation_not_event_cancellation":
            decision_result = _record_decision(
                observation=current_observation,
                sync_run=current_run,
                decision_type="ignored",
                status="rejected",
                covered_fields=(),
                match_factors=_match_factors(
                    observation=current_observation,
                    event=event,
                    outcome="call_cancellation_not_event_cancellation",
                    reason_code="CONFERENCE_CALL_CANCELLATION_IS_NOT_EVENT_CANCELLATION",
                    authority="ir",
                ),
                reason="CONFERENCE_CALL_CANCELLATION_IS_NOT_EVENT_CANCELLATION",
                target_event=event,
            )
            return InvestorRelationsEvaluationResult(
                observation=current_observation,
                decision=decision_result.decision,
                decision_created=decision_result.created,
                event=event,
                outcome="call_cancellation_not_event_cancellation",
                schedule_result=None,
                status_result=None,
                blocked_by_manual_authority=False,
            )

        manual_fields = _manual_authority_fields(event)
        blocked_fields = sorted(set(planned.fields) & manual_fields)
        if blocked_fields:
            decision_result = _record_decision(
                observation=current_observation,
                sync_run=current_run,
                decision_type="ignored",
                status="rejected",
                covered_fields=tuple(blocked_fields),
                match_factors=_match_factors(
                    observation=current_observation,
                    event=event,
                    outcome="blocked_by_manual_authority",
                    reason_code="MANUAL_AUTHORITY_PRESENT",
                    authority="manual",
                    extra={"blocked_fields": blocked_fields},
                ),
                reason="MANUAL_AUTHORITY_PRESENT",
                target_event=event,
            )
            return InvestorRelationsEvaluationResult(
                observation=current_observation,
                decision=decision_result.decision,
                decision_created=decision_result.created,
                event=event,
                outcome="blocked_by_manual_authority",
                schedule_result=None,
                status_result=None,
                blocked_by_manual_authority=True,
            )

        conflict = _detect_same_authority_conflict(
            observation=current_observation,
            event=event,
            planned=planned,
        )
        if conflict is not None:
            return _record_conflict(
                observation=current_observation,
                sync_run=current_run,
                event=event,
                reason_code=str(conflict["reason_code"]),
                covered_fields=planned.fields,
                extra=conflict,
            )

        facts = _normalized_facts(current_observation)
        schedule_result, status_result, evidence = _apply_planned_fields(
            observation=current_observation,
            sync_run=current_run,
            event=event,
            planned=planned,
            facts=facts,
            actor_user=None,
            reason="",
            request_id="",
        )
        decision_result = _record_decision(
            observation=current_observation,
            sync_run=current_run,
            decision_type=planned.decision_type,
            status="resolved",
            covered_fields=planned.fields,
            match_factors=_match_factors(
                observation=current_observation,
                event=event,
                outcome=planned.outcome,
                reason_code=planned.outcome.upper(),
                authority="ir",
                facts=facts,
            ),
            reason=planned.outcome.upper(),
            target_event=event,
            source_evidence=evidence,
        )
        return InvestorRelationsEvaluationResult(
            observation=current_observation,
            decision=decision_result.decision,
            decision_created=decision_result.created,
            event=event,
            outcome=planned.outcome,
            schedule_result=schedule_result,
            status_result=status_result,
            blocked_by_manual_authority=False,
        )


def resolve_investor_relations_observation_manually(
    *,
    observation: InvestorRelationsObservation,
    actor_user: User,
    reason: str,
    request_id: str,
    target_event: EarningsEvent | None = None,
) -> InvestorRelationsEvaluationResult:
    """Apply one observation under explicit manual authority.

    Manual authority is higher than IR: it may overwrite an automatic IR value
    and may apply a precision regression.  The manual path always appends a
    manual decision leaf plus the standard schedule/status audit history.
    """

    if not reason.strip():
        raise InvalidInvestorRelationsConfirmation("Manual resolution requires a reason.")
    if not request_id.strip():
        raise InvalidInvestorRelationsConfirmation("Manual resolution requires a request_id.")

    with transaction.atomic():
        current_observation = _load_observation(observation)
        event: EarningsEvent | None
        if target_event is not None:
            event = _load_event(target_event)
        else:
            resolved, _resolution = _resolve_canonical_event(current_observation)
            event = resolved
        if event is None:
            raise InvalidInvestorRelationsConfirmation(
                "Manual resolution requires an explicit or uniquely resolved canonical event."
            )
        planned = _plan_fields(current_observation)
        if planned.outcome == "call_cancellation_not_event_cancellation":
            raise InvalidInvestorRelationsConfirmation(
                "Conference call cancellation does not authorize EarningsEvent cancellation."
            )
        facts = _normalized_facts(current_observation)
        schedule_result, status_result, evidence = _apply_planned_fields(
            observation=current_observation,
            sync_run=None,
            event=event,
            planned=planned,
            facts=facts,
            actor_user=actor_user,
            reason=reason.strip(),
            request_id=request_id.strip(),
        )
        decision_result = _record_decision(
            observation=current_observation,
            sync_run=None,
            decision_type=planned.decision_type,
            status="resolved",
            covered_fields=planned.fields,
            match_factors=_match_factors(
                observation=current_observation,
                event=event,
                outcome=f"manual_{planned.outcome}",
                reason_code="MANUAL_RESOLUTION",
                authority="manual",
                facts=facts,
            ),
            reason=reason.strip(),
            target_event=event,
            actor_user=actor_user,
            request_id=request_id.strip(),
            source_evidence=evidence,
        )
        return InvestorRelationsEvaluationResult(
            observation=current_observation,
            decision=decision_result.decision,
            decision_created=decision_result.created,
            event=event,
            outcome=f"manual_{planned.outcome}",
            schedule_result=schedule_result,
            status_result=status_result,
            blocked_by_manual_authority=False,
        )


def _load_observation(
    observation: InvestorRelationsObservation,
) -> InvestorRelationsObservation:
    if observation._state.adding or observation.pk is None:
        raise InvalidInvestorRelationsConfirmation("observation must be saved before use.")
    try:
        return cast(
            InvestorRelationsObservation,
            InvestorRelationsObservation.objects.select_related(
                "source",
                "raw_data_record",
                "company",
            ).get(pk=observation.pk),
        )
    except InvestorRelationsObservation.DoesNotExist as error:
        raise InvalidInvestorRelationsConfirmation("observation no longer exists.") from error


def _load_run(sync_run: SyncRun) -> SyncRun:
    if sync_run._state.adding or sync_run.pk is None:
        raise InvalidInvestorRelationsConfirmation("sync_run must be saved before use.")
    try:
        return SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvalidInvestorRelationsConfirmation("sync_run no longer exists.") from error


def _load_event(event: EarningsEvent) -> EarningsEvent:
    try:
        return EarningsEvent.objects.select_for_update().get(pk=event.pk)
    except EarningsEvent.DoesNotExist as error:
        raise InvalidInvestorRelationsConfirmation("target_event no longer exists.") from error


def _validate_observation_run_lineage(
    *,
    observation: InvestorRelationsObservation,
    sync_run: SyncRun,
) -> None:
    if observation.source_id != sync_run.source_id:
        raise InvalidInvestorRelationsConfirmation(
            "observation and sync_run must share one DataSource."
        )
    if not RawDataObservation.objects.filter(
        sync_run_id=sync_run.pk,
        raw_data_record_id=observation.raw_data_record_id,
    ).exists():
        raise InvalidInvestorRelationsConfirmation(
            "sync_run must have observed the observation raw record."
        )


def _resolve_canonical_event(
    observation: InvestorRelationsObservation,
) -> tuple[EarningsEvent | None, _Resolution | None]:
    candidates = list(
        EarningsEvent.objects.select_for_update()
        .filter(
            company_id=observation.company_id,
            identity_status=IdentityStatus.CANONICAL,
            period_end_date=observation.period_end_date,
            period_type=observation.period_type,
        )
        .order_by("id")
    )
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, _Resolution(
            decision_type="conflict",
            status="open",
            outcome="review_required",
            reason="MULTIPLE_CANONICAL_EVENTS",
            match_factors=_match_factors(
                observation=observation,
                event=None,
                outcome="review_required",
                reason_code="MULTIPLE_CANONICAL_EVENTS",
                authority="ir",
                extra={"candidate_event_ids": [str(item.pk) for item in candidates]},
            ),
        )
    candidate_exists = EarningsEvent.objects.filter(
        company_id=observation.company_id,
        identity_status=IdentityStatus.CANDIDATE,
        period_end_date=observation.period_end_date,
        period_type=observation.period_type,
    ).exists()
    if candidate_exists:
        return None, _Resolution(
            decision_type="conflict",
            status="open",
            outcome="review_required",
            reason="CANDIDATE_ONLY_EVENT",
            match_factors=_match_factors(
                observation=observation,
                event=None,
                outcome="review_required",
                reason_code="CANDIDATE_ONLY_EVENT",
                authority="ir",
            ),
        )
    return None, _Resolution(
        decision_type="no_match",
        status="rejected",
        outcome="no_match",
        reason="CANONICAL_EVENT_NOT_FOUND",
        match_factors=_match_factors(
            observation=observation,
            event=None,
            outcome="no_match",
            reason_code="CANONICAL_EVENT_NOT_FOUND",
            authority="ir",
        ),
    )


def _plan_fields(observation: InvestorRelationsObservation) -> _PlannedFields:
    fields: list[str] = []
    if observation.release_session not in (None, ReleaseSession.UNKNOWN):
        fields.append("release_session")

    if observation.item_type == InvestorRelationsItemType.RELEASE_CONFIRMATION:
        if observation.confirmed_release_precision != EarningsDatePrecision.UNKNOWN:
            fields.append("confirmed_release")
            return _PlannedFields(
                fields=tuple(fields),
                target_status=EventStatus.SCHEDULED_CONFIRMED,
                decision_type="confirmed_schedule",
                outcome="confirmed_release",
            )
        return _PlannedFields(
            fields=tuple(fields + ["estimated_release"]),
            target_status=None,
            decision_type="confirmed_schedule",
            outcome="tentative_schedule",
        )

    if observation.item_type == InvestorRelationsItemType.CALL_NOTICE:
        fields.insert(0, "conference_call")
        if observation.confirmed_release_precision != EarningsDatePrecision.UNKNOWN:
            fields.append("confirmed_release")
            return _PlannedFields(
                fields=tuple(fields),
                target_status=EventStatus.SCHEDULED_CONFIRMED,
                decision_type="confirmed_schedule",
                outcome="confirmed_release_with_call",
            )
        return _PlannedFields(
            fields=tuple(fields),
            target_status=None,
            decision_type="updated_conference_call",
            outcome="conference_call",
        )

    if observation.item_type == InvestorRelationsItemType.RESULTS_RELEASE:
        fields.insert(0, "earnings_release")
        if observation.conference_call_precision != EarningsDatePrecision.UNKNOWN:
            fields.append("conference_call")
        return _PlannedFields(
            fields=tuple(fields),
            target_status=EventStatus.RELEASED,
            decision_type="released",
            outcome="released",
        )

    cancellation = observation.cancellation or {}
    if cancellation.get("scope") == "conference_call":
        return _PlannedFields(
            fields=(),
            target_status=None,
            decision_type="ignored",
            outcome="call_cancellation_not_event_cancellation",
        )
    return _PlannedFields(
        fields=("status",),
        target_status=EventStatus.CANCELLED,
        decision_type="cancelled",
        outcome="cancelled",
    )


def _normalized_facts(observation: InvestorRelationsObservation) -> dict[str, object]:
    facts: dict[str, object] = {}
    for field_name in _DATE_FIELDS:
        fact = _observation_date_fact(observation, field_name)
        if fact is not None:
            facts[field_name] = fact
    if observation.release_session not in (None, ReleaseSession.UNKNOWN):
        facts["release_session"] = {
            "kind": "session",
            "precision": "session_only",
            "value": observation.release_session,
        }
    if observation.item_type == InvestorRelationsItemType.CANCELLATION:
        facts["cancellation"] = dict(observation.cancellation or {})
    return facts


def _observation_date_fact(
    observation: InvestorRelationsObservation,
    field_name: str,
) -> dict[str, str] | None:
    if field_name == "estimated_release":
        precision = observation.estimated_release_precision
        date_value = observation.estimated_release_date
        datetime_value = observation.estimated_release_at
    elif field_name == "confirmed_release":
        precision = observation.confirmed_release_precision
        date_value = observation.confirmed_release_date
        datetime_value = observation.confirmed_release_at
    elif field_name == "earnings_release":
        precision = observation.earnings_release_precision
        date_value = observation.earnings_release_date
        datetime_value = observation.earnings_release_at
    else:
        precision = observation.conference_call_precision
        date_value = observation.conference_call_date
        datetime_value = observation.conference_call_at

    if precision == EarningsDatePrecision.DATE_ONLY and date_value is not None:
        return {
            "kind": "date",
            "precision": EarningsDatePrecision.DATE_ONLY,
            "value": date_value.isoformat(),
        }
    if precision == EarningsDatePrecision.EXACT_DATETIME and datetime_value is not None:
        return {
            "kind": "datetime",
            "precision": EarningsDatePrecision.EXACT_DATETIME,
            "value": datetime_value.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
    return None


def _current_date_state(
    event: EarningsEvent,
    field_name: str,
) -> tuple[str, date | datetime | None]:
    if field_name == "estimated_release":
        return (
            event.estimated_release_precision,
            event.estimated_release_at or event.estimated_release_date,
        )
    if field_name == "confirmed_release":
        return (
            event.confirmed_release_precision,
            event.confirmed_release_at or event.confirmed_release_date,
        )
    if field_name == "earnings_release":
        return (
            event.earnings_release_precision,
            event.earnings_release_at or event.earnings_release_date,
        )
    return (
        event.conference_call_precision,
        event.conference_call_at or event.conference_call_date,
    )


def _observation_date_state(
    observation: InvestorRelationsObservation,
    field_name: str,
) -> tuple[str, date | datetime | None]:
    if field_name == "estimated_release":
        return (
            observation.estimated_release_precision,
            observation.estimated_release_at or observation.estimated_release_date,
        )
    if field_name == "confirmed_release":
        return (
            observation.confirmed_release_precision,
            observation.confirmed_release_at or observation.confirmed_release_date,
        )
    if field_name == "earnings_release":
        return (
            observation.earnings_release_precision,
            observation.earnings_release_at or observation.earnings_release_date,
        )
    return (
        observation.conference_call_precision,
        observation.conference_call_at or observation.conference_call_date,
    )


def _detect_same_authority_conflict(
    *,
    observation: InvestorRelationsObservation,
    event: EarningsEvent,
    planned: _PlannedFields,
) -> dict[str, object] | None:
    ir_facts = _ir_authority_facts(event)
    for field_name in planned.fields:
        if field_name == "status":
            continue
        if field_name == "release_session":
            observed_session = observation.release_session
            current_session = event.release_session
            if (
                observed_session is not None
                and observed_session != ReleaseSession.UNKNOWN
                and current_session != ReleaseSession.UNKNOWN
                and current_session != observed_session
                and field_name in ir_facts
            ):
                return {
                    "reason_code": "IR_SAME_AUTHORITY_CONFLICT",
                    "conflicting_field": field_name,
                    "current_value": current_session,
                    "observed_value": observed_session,
                }
            continue
        current_precision, current_value = _current_date_state(event, field_name)
        observed_precision, observed_value = _observation_date_state(observation, field_name)
        if observed_precision == EarningsDatePrecision.UNKNOWN:
            continue
        if current_precision == observed_precision and current_value == observed_value:
            continue
        current_market_date = _market_date(current_value)
        observed_market_date = _market_date(observed_value)
        if current_precision != EarningsDatePrecision.UNKNOWN:
            if _PRECISION_RANK[observed_precision] < _PRECISION_RANK[current_precision]:
                return {
                    "reason_code": "PRECISION_REGRESSION_BLOCKED",
                    "conflicting_field": field_name,
                    "current_precision": current_precision,
                    "observed_precision": observed_precision,
                }
            if (
                current_market_date is not None
                and current_market_date == observed_market_date
                and _PRECISION_RANK[observed_precision] > _PRECISION_RANK[current_precision]
            ):
                # Precision refinement of the same business date is allowed.
                continue
        if field_name in ir_facts and (
            current_market_date != observed_market_date or current_precision == observed_precision
        ):
            return {
                "reason_code": "IR_SAME_AUTHORITY_CONFLICT",
                "conflicting_field": field_name,
                "current_value": _json_value(current_value),
                "observed_value": _json_value(observed_value),
            }
    return None


def _market_date(value: date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(MARKET_TIMEZONE).date()
    return value


def _ir_authority_facts(event: EarningsEvent) -> dict[str, object]:
    """Return fields already written by an effective automatic or manual IR leaf."""

    fields: dict[str, object] = {}
    decisions = InvestorRelationsDecision.objects.filter(
        target_event_id=event.pk,
        status="resolved",
    ).order_by("decided_at", "created_at", "id")
    for decision in decisions:
        if InvestorRelationsDecision.objects.filter(supersedes_id=decision.pk).exists():
            continue
        namespace = decision.match_factors.get(_MATCH_FACTORS_NAMESPACE)
        if not isinstance(namespace, dict):
            continue
        facts = namespace.get("facts")
        if not isinstance(facts, dict):
            continue
        for field_name in facts:
            fields[field_name] = facts[field_name]
    return fields


def _manual_authority_fields(event: EarningsEvent) -> set[str]:
    fields: set[str] = set()
    for decision in InvestorRelationsDecision.objects.filter(
        target_event_id=event.pk,
        actor_user__isnull=False,
        status="resolved",
    ):
        if InvestorRelationsDecision.objects.filter(supersedes_id=decision.pk).exists():
            continue
        fields.update(str(item) for item in decision.covered_fields)
    for reconciliation_decision in EarningsReconciliationDecision.objects.filter(
        target_event_id=event.pk,
        actor_user__isnull=False,
        status="resolved",
    ):
        if EarningsReconciliationDecision.objects.filter(
            supersedes_id=reconciliation_decision.pk
        ).exists():
            continue
        fields.update(str(item) for item in reconciliation_decision.covered_fields)
    # Manual schedule/status corrections performed through the existing
    # earnings services do not create a decision leaf, but their actor-owned
    # DataChange rows still express manual authority (manual > IR).
    fields.update(
        str(field_name)
        for field_name in DataChange.objects.filter(
            target_type=DataChange.TargetType.EARNINGS_EVENT,
            target_id=event.pk,
            actor_user__isnull=False,
        ).values_list("field_name", flat=True)
    )
    return fields


def _apply_planned_fields(
    *,
    observation: InvestorRelationsObservation,
    sync_run: SyncRun | None,
    event: EarningsEvent,
    planned: _PlannedFields,
    facts: dict[str, object],
    actor_user: User | None,
    reason: str,
    request_id: str,
) -> tuple[
    EarningsScheduleWriteResult | None,
    EarningsStatusWriteResult | None,
    SourceEvidence | None,
]:
    schedule_changes: dict[str, object] = {}
    for field_name in planned.fields:
        if field_name == "status":
            continue
        if field_name == "release_session":
            schedule_changes[field_name] = observation.release_session
            continue
        schedule_changes[field_name] = _observation_schedule_value(observation, field_name)

    schedule_result: EarningsScheduleWriteResult | None = None
    primary_evidence: SourceEvidence | None = None
    for field_name in sorted(schedule_changes):
        # Each IR-driven field gets its own SourceEvidence so the full
        # DataSource -> SyncRun -> RawDataRecord -> SourceEvidence -> DataChange
        # chain remains traceable per field.
        schedule_evidence = _record_field_evidence(
            observation=observation,
            sync_run=sync_run,
            event=event,
            field_name=field_name,
            normalized_value=facts.get(field_name),
            actor_user=actor_user,
        )
        if primary_evidence is None:
            primary_evidence = schedule_evidence
        schedule_result = update_earnings_schedule(
            earnings_event=event,
            changes={field_name: schedule_changes[field_name]},
            source_evidence=schedule_evidence if actor_user is None else None,
            sync_run=sync_run if actor_user is None else None,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
        )

    status_result: EarningsStatusWriteResult | None = None
    if planned.target_status is not None:
        status_evidence = _record_field_evidence(
            observation=observation,
            sync_run=sync_run,
            event=event,
            field_name="status",
            normalized_value=planned.target_status,
            actor_user=actor_user,
        )
        if primary_evidence is None:
            primary_evidence = status_evidence
        resolved_evidence = status_evidence if actor_user is None else None
        resolved_run = sync_run if actor_user is None else None
        if planned.target_status == EventStatus.CANCELLED:
            status_result = cancel_earnings_event(
                earnings_event=event,
                affirmative_cancellation=True,
                source_evidence=resolved_evidence,
                sync_run=resolved_run,
                actor_user=actor_user,
                reason=reason,
                request_id=request_id,
            )
        elif planned.target_status == EventStatus.RELEASED:
            status_result = mark_earnings_released(
                earnings_event=event,
                source_evidence=resolved_evidence,
                sync_run=resolved_run,
                actor_user=actor_user,
                reason=reason,
                request_id=request_id,
            )
        else:
            status_result = confirm_earnings_event(
                earnings_event=event,
                source_evidence=resolved_evidence,
                sync_run=resolved_run,
                actor_user=actor_user,
                reason=reason,
                request_id=request_id,
            )
    return schedule_result, status_result, primary_evidence


def _observation_schedule_value(
    observation: InvestorRelationsObservation,
    field_name: str,
) -> object:
    if field_name == "estimated_release":
        return observation.estimated_release_at or observation.estimated_release_date
    if field_name == "confirmed_release":
        return observation.confirmed_release_at or observation.confirmed_release_date
    if field_name == "earnings_release":
        return observation.earnings_release_at or observation.earnings_release_date
    return observation.conference_call_at or observation.conference_call_date


def _record_field_evidence(
    *,
    observation: InvestorRelationsObservation,
    sync_run: SyncRun | None,
    event: EarningsEvent,
    field_name: str,
    normalized_value: object,
    actor_user: User | None,
) -> SourceEvidence | None:
    if sync_run is None:
        if actor_user is None:
            raise InvalidInvestorRelationsConfirmation("Automatic IR writes require a SyncRun.")
        return None
    result = record_source_evidence(
        raw_data_record=observation.raw_data_record,
        sync_run=sync_run,
        target_type="earnings_event",
        target_id=event.pk,
        field_name=field_name,
        raw_value={
            "item_type": observation.item_type,
            "source_event_identity": observation.source_event_identity,
        },
        normalized_value=normalized_value,
        confidence=observation.confidence or Decimal("1.0000"),
        normalizer_version=IR_EVIDENCE_NORMALIZER_VERSION,
    )
    return result.evidence


def _record_conflict(
    *,
    observation: InvestorRelationsObservation,
    sync_run: SyncRun,
    event: EarningsEvent,
    reason_code: str,
    covered_fields: tuple[str, ...] = (),
    extra: dict[str, object] | None = None,
) -> InvestorRelationsEvaluationResult:
    decision_result = _record_decision(
        observation=observation,
        sync_run=sync_run,
        decision_type="conflict",
        status="open",
        covered_fields=covered_fields,
        match_factors=_match_factors(
            observation=observation,
            event=event,
            outcome="review_required",
            reason_code=reason_code,
            authority="ir",
            extra=extra,
        ),
        reason=reason_code,
        target_event=event,
    )
    return InvestorRelationsEvaluationResult(
        observation=observation,
        decision=decision_result.decision,
        decision_created=decision_result.created,
        event=event,
        outcome="review_required",
        schedule_result=None,
        status_result=None,
        blocked_by_manual_authority=False,
    )


def _match_factors(
    *,
    observation: InvestorRelationsObservation,
    event: EarningsEvent | None,
    outcome: str,
    reason_code: str,
    authority: str,
    facts: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    namespace: dict[str, object] = {
        "authority": authority,
        "item_type": observation.item_type,
        "outcome": outcome,
        "reason_code": reason_code,
        "rule_version": IR_DECISION_RULE_VERSION,
        "source_event_identity": observation.source_event_identity,
        "target_event_id": str(event.pk) if event is not None else None,
    }
    if facts is not None:
        namespace["facts"] = facts
    if extra:
        namespace.update(extra)
    return {_MATCH_FACTORS_NAMESPACE: namespace}


def _record_decision(
    *,
    observation: InvestorRelationsObservation,
    sync_run: SyncRun | None,
    decision_type: str,
    status: str,
    covered_fields: tuple[str, ...],
    match_factors: dict[str, object],
    reason: str,
    target_event: EarningsEvent | None,
    source_evidence: SourceEvidence | None = None,
    actor_user: User | None = None,
    request_id: str = "",
) -> InvestorRelationsDecisionWriteResult:
    result = record_investor_relations_decision(
        observation=observation,
        decision_type=decision_type,
        status=status,
        rule_version=IR_DECISION_RULE_VERSION,
        covered_fields=covered_fields,
        match_factors=match_factors,
        reason=reason,
        target_event=target_event,
        source_evidence=source_evidence,
        actor_user=actor_user,
        sync_run=sync_run,
        request_id=request_id,
    )
    if result.created:
        _record_decision_audit(
            decision=result.decision,
            sync_run=sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
        )
    return result


def _record_decision_audit(
    *,
    decision: InvestorRelationsDecision,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    request_id: str,
) -> None:
    after = {
        "covered_fields": list(decision.covered_fields),
        "decision_type": decision.decision_type,
        "reason": decision.reason,
        "status": decision.status,
    }
    if actor_user is not None:
        record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.MANUAL_CORRECTION,
            target_type=AuditRecord.TargetType.INVESTOR_RELATIONS_DECISION,
            target_id=decision.pk,
            before={},
            after=after,
            reason=reason,
            request_id=request_id,
        )
        return
    if sync_run is None:
        raise InvalidInvestorRelationsConfirmation("Automatic IR decisions require a SyncRun.")
    record_system_action(
        sync_run=sync_run,
        action=AuditRecord.Action.UPDATE,
        target_type=AuditRecord.TargetType.INVESTOR_RELATIONS_DECISION,
        target_id=decision.pk,
        before={},
        after=after,
        request_id=f"ir-decision:{decision.decision_key}",
    )


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value
