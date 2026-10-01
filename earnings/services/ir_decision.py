"""Append-only persistence primitive for InvestorRelationsDecision rows."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import RawDataRecord, SourceEvidence, SyncRun
from audit.security import AuditSecurityError, normalize_json_without_credentials
from earnings.models import (
    ALLOWED_INVESTOR_RELATIONS_DECISION_STATUSES,
    ALLOWED_INVESTOR_RELATIONS_DECISION_TYPES,
    EarningsEvent,
    InvestorRelationsDecision,
    InvestorRelationsObservation,
)

if TYPE_CHECKING:
    from accounts.models import User

IR_DECISION_RULE_VERSION = "ir-confirmation-decision-v1"
IR_EVIDENCE_NORMALIZER_VERSION = "ir-confirmation-evidence-v1"

ALLOWED_IR_COVERED_FIELDS = frozenset(
    {
        "estimated_release",
        "confirmed_release",
        "earnings_release",
        "conference_call",
        "release_session",
        "status",
    }
)
_RESOLVED_DECISION_TYPES = frozenset(
    {"confirmed_schedule", "updated_conference_call", "released", "cancelled"}
)
_REJECTED_DECISION_TYPES = frozenset({"no_match", "ignored"})
_UNIQUE_VIOLATION_SQLSTATE = "23505"
_DECISION_KEY_UNIQUE_CONSTRAINT = "investor_relations_decision_key_unique"


class InvestorRelationsDecisionServiceError(ValueError):
    """Base error for the IR decision persistence primitive."""


class InvalidInvestorRelationsDecision(InvestorRelationsDecisionServiceError):
    pass


class InvestorRelationsDecisionIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InvestorRelationsDecisionWriteResult:
    decision: InvestorRelationsDecision
    created: bool


def build_investor_relations_decision_key(
    *,
    observation_id: uuid.UUID,
    decision_type: str,
    status: str,
    covered_fields: Sequence[str],
    rule_version: str,
    decision_source: str,
    match_factors: Mapping[str, object],
    target_event_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    request_id: str,
) -> str:
    """Build the deterministic identity of one IR authority decision.

    The canonical payload covers the observation subject, the decision and
    authority outcome, the affected fields, rule/evidence factors, and the
    automatic or manual origin.  It never contains wall clock, raw payload
    bytes, supersession pointers, or database insertion order.
    """

    normalized_type = _normalize_decision_type(decision_type)
    normalized_status = _normalize_status(status)
    normalized_fields = _normalize_covered_fields(covered_fields)
    normalized_rule = _normalize_rule_version(rule_version)
    normalized_source = _normalize_decision_source(decision_source)
    normalized_factors = _normalize_match_factors(match_factors)
    normalized_request_id = _normalize_request_id(request_id)

    if normalized_source == "automatic":
        if actor_user_id is not None:
            raise InvalidInvestorRelationsDecision(
                "automatic decisions must not define actor_user."
            )
        if normalized_request_id:
            raise InvalidInvestorRelationsDecision(
                "automatic decisions must not define request_id."
            )
        origin: dict[str, object] = {"kind": "automatic"}
    else:
        if actor_user_id is None:
            raise InvalidInvestorRelationsDecision("manual decisions require actor_user.")
        if not normalized_request_id:
            raise InvalidInvestorRelationsDecision("manual decisions require request_id.")
        origin = {
            "actor_user_id": str(actor_user_id),
            "kind": "manual",
            "request_id": normalized_request_id,
        }

    identity = {
        "covered_fields": list(normalized_fields),
        "decision_source": normalized_source,
        "decision_type": normalized_type,
        "match_factors": normalized_factors,
        "observation_id": str(observation_id),
        "origin": origin,
        "rule_version": normalized_rule,
        "status": normalized_status,
        "target_event_id": str(target_event_id) if target_event_id is not None else None,
    }
    serialized = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def record_investor_relations_decision(
    *,
    observation: InvestorRelationsObservation,
    decision_type: str,
    status: str,
    rule_version: str = IR_DECISION_RULE_VERSION,
    covered_fields: Sequence[str] = (),
    match_factors: Mapping[str, object] | None = None,
    reason: str = "",
    target_event: EarningsEvent | None = None,
    source_raw_data_record: RawDataRecord | None = None,
    source_evidence: SourceEvidence | None = None,
    actor_user: User | None = None,
    sync_run: SyncRun | None = None,
    request_id: str = "",
    supersedes: InvestorRelationsDecision | None = None,
    decided_at: datetime | None = None,
) -> InvestorRelationsDecisionWriteResult:
    """Persist one IR decision without choosing its outcome.

    The caller supplies the outcome and provenance.  This primitive validates
    persistence invariants, derives the deterministic decision key, appends the
    observation's effective-leaf successor when appropriate, and reuses an
    existing row when the same key is written concurrently.  It never mutates
    EarningsEvent or any schedule/lifecycle state.
    """

    normalized_type = _normalize_decision_type(decision_type)
    normalized_status = _normalize_status(status)
    normalized_fields = _normalize_covered_fields(covered_fields)
    normalized_rule = _normalize_rule_version(rule_version)
    normalized_factors = _normalize_match_factors(match_factors)
    normalized_reason = _normalize_reason(reason)
    normalized_request_id = _normalize_request_id(request_id)
    normalized_decided_at = _normalize_decided_at(decided_at)
    decision_source = "manual" if actor_user is not None else "automatic"

    _validate_outcome(
        decision_type=normalized_type,
        status=normalized_status,
        target_event=target_event,
    )
    _validate_context(
        decision_source=decision_source,
        actor_user=actor_user,
        sync_run=sync_run,
        reason=normalized_reason,
        request_id=normalized_request_id,
    )
    _require_persisted(observation, value_name="observation")
    if target_event is not None:
        _require_persisted(target_event, value_name="target_event")
    if actor_user is not None:
        _require_persisted(actor_user, value_name="actor_user")
    if sync_run is not None:
        _require_persisted(sync_run, value_name="sync_run")
    if supersedes is not None:
        _require_persisted(supersedes, value_name="supersedes")
    if source_raw_data_record is not None:
        _require_persisted(source_raw_data_record, value_name="source_raw_data_record")
    if source_evidence is not None:
        _require_persisted(source_evidence, value_name="source_evidence")

    with transaction.atomic():
        current_observation = _load_observation(observation)
        current_target = _load_target_event(target_event) if target_event is not None else None
        current_sync_run = _load_sync_run(sync_run) if sync_run is not None else None
        current_supersedes = _load_supersedes(supersedes) if supersedes is not None else None
        current_raw_record, current_evidence = _load_provenance(
            observation=current_observation,
            source_raw_data_record=source_raw_data_record,
            source_evidence=source_evidence,
        )
        if current_supersedes is not None and (
            current_supersedes.observation_id != current_observation.pk
        ):
            raise InvalidInvestorRelationsDecision(
                "supersedes must belong to the same observation."
            )
        if current_supersedes is None:
            current_supersedes = _default_supersedes(
                observation=current_observation,
                decision_source=decision_source,
            )

        decision_key = build_investor_relations_decision_key(
            observation_id=current_observation.pk,
            decision_type=normalized_type,
            status=normalized_status,
            covered_fields=normalized_fields,
            rule_version=normalized_rule,
            decision_source=decision_source,
            match_factors=normalized_factors,
            target_event_id=current_target.pk if current_target is not None else None,
            actor_user_id=actor_user.pk if actor_user is not None else None,
            request_id=normalized_request_id,
        )
        # An existing equivalent decision must be reused without appending a
        # second supersession link.
        existing = InvestorRelationsDecision.objects.filter(decision_key=decision_key).first()
        if existing is not None:
            verify_reused_investor_relations_decision(
                decision=existing,
                observation_id=current_observation.pk,
                decision_type=normalized_type,
                status=normalized_status,
                covered_fields=normalized_fields,
                rule_version=normalized_rule,
                decision_source=decision_source,
                match_factors=normalized_factors,
                target_event_id=current_target.pk if current_target is not None else None,
                actor_user_id=actor_user.pk if actor_user is not None else None,
                request_id=normalized_request_id,
            )
            return InvestorRelationsDecisionWriteResult(decision=existing, created=False)

        try:
            with transaction.atomic():
                decision = InvestorRelationsDecision.objects.create(
                    observation=current_observation,
                    target_event=current_target,
                    decision_type=normalized_type,
                    status=normalized_status,
                    covered_fields=list(normalized_fields),
                    rule_version=normalized_rule,
                    match_factors=normalized_factors,
                    reason=normalized_reason,
                    source_raw_data_record=current_raw_record,
                    source_evidence=current_evidence,
                    actor_user=actor_user,
                    sync_run=current_sync_run,
                    request_id=normalized_request_id,
                    decided_at=normalized_decided_at,
                    supersedes=current_supersedes,
                    decision_key=decision_key,
                )
                return InvestorRelationsDecisionWriteResult(decision=decision, created=True)
        except IntegrityError as error:
            if not _is_decision_key_unique_violation(error):
                raise
            existing = InvestorRelationsDecision.objects.filter(decision_key=decision_key).first()
            if existing is None:
                raise
            verify_reused_investor_relations_decision(
                decision=existing,
                observation_id=current_observation.pk,
                decision_type=normalized_type,
                status=normalized_status,
                covered_fields=normalized_fields,
                rule_version=normalized_rule,
                decision_source=decision_source,
                match_factors=normalized_factors,
                target_event_id=current_target.pk if current_target is not None else None,
                actor_user_id=actor_user.pk if actor_user is not None else None,
                request_id=normalized_request_id,
            )
            return InvestorRelationsDecisionWriteResult(decision=existing, created=False)


def verify_reused_investor_relations_decision(
    *,
    decision: InvestorRelationsDecision,
    observation_id: uuid.UUID,
    decision_type: str,
    status: str,
    covered_fields: Sequence[str],
    rule_version: str,
    decision_source: str,
    match_factors: Mapping[str, object],
    target_event_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    request_id: str,
) -> None:
    """Verify an existing key without requiring its supersession predecessor."""

    if (
        decision.observation_id != observation_id
        or decision.decision_type != decision_type
        or decision.status != status
        or list(decision.covered_fields) != list(covered_fields)
        or decision.rule_version != rule_version
        or decision.target_event_id != target_event_id
        or decision.match_factors != dict(match_factors)
        or decision.request_id != request_id
        or (decision.actor_user_id is not None) != (decision_source == "manual")
        or decision.actor_user_id != actor_user_id
    ):
        raise InvestorRelationsDecisionIntegrityError(
            "An existing InvestorRelationsDecision has the same key but different immutable data."
        )


def effective_leaf_for_observation(
    observation: InvestorRelationsObservation,
) -> InvestorRelationsDecision | None:
    """Return the latest non-superseded decision for one observation."""

    decisions = list(
        InvestorRelationsDecision.objects.filter(observation_id=observation.pk).order_by(
            "decided_at",
            "created_at",
            "id",
        )
    )
    if not decisions:
        return None
    superseded_ids = {item.supersedes_id for item in decisions if item.supersedes_id is not None}
    leaves = [item for item in decisions if item.pk not in superseded_ids]
    if not leaves:
        raise InvestorRelationsDecisionIntegrityError("IR decision history has no effective leaf.")
    return cast(InvestorRelationsDecision, leaves[-1])


def _default_supersedes(
    *,
    observation: InvestorRelationsObservation,
    decision_source: str,
) -> InvestorRelationsDecision | None:
    leaf = effective_leaf_for_observation(observation)
    if leaf is None:
        return None
    if decision_source == "automatic" and leaf.actor_user_id is not None:
        return None
    return leaf


def _load_observation(
    observation: InvestorRelationsObservation,
) -> InvestorRelationsObservation:
    if observation._state.adding or observation.pk is None:
        raise InvalidInvestorRelationsDecision("observation must be saved before use.")
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
        raise InvalidInvestorRelationsDecision("observation no longer exists.") from error


def _load_target_event(target_event: EarningsEvent) -> EarningsEvent:
    try:
        return EarningsEvent.objects.get(pk=target_event.pk)
    except EarningsEvent.DoesNotExist as error:
        raise InvalidInvestorRelationsDecision("target_event no longer exists.") from error


def _load_sync_run(sync_run: SyncRun) -> SyncRun:
    try:
        return SyncRun.objects.get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvalidInvestorRelationsDecision("sync_run no longer exists.") from error


def _load_supersedes(
    supersedes: InvestorRelationsDecision,
) -> InvestorRelationsDecision:
    try:
        return InvestorRelationsDecision.objects.get(pk=supersedes.pk)
    except InvestorRelationsDecision.DoesNotExist as error:
        raise InvalidInvestorRelationsDecision("supersedes no longer exists.") from error


def _load_provenance(
    *,
    observation: InvestorRelationsObservation,
    source_raw_data_record: RawDataRecord | None,
    source_evidence: SourceEvidence | None,
) -> tuple[RawDataRecord, SourceEvidence | None]:
    resolved_raw_record = observation.raw_data_record
    if source_raw_data_record is not None:
        try:
            resolved_raw_record = RawDataRecord.objects.get(pk=source_raw_data_record.pk)
        except RawDataRecord.DoesNotExist as error:
            raise InvalidInvestorRelationsDecision(
                "source_raw_data_record no longer exists."
            ) from error
    if resolved_raw_record.pk != observation.raw_data_record_id:
        raise InvalidInvestorRelationsDecision(
            "source_raw_data_record must match the observation raw record."
        )

    if source_evidence is None:
        return resolved_raw_record, None
    try:
        evidence = SourceEvidence.objects.get(pk=source_evidence.pk)
    except SourceEvidence.DoesNotExist as error:
        raise InvalidInvestorRelationsDecision("source_evidence no longer exists.") from error
    if evidence.raw_data_record_id != resolved_raw_record.pk:
        raise InvalidInvestorRelationsDecision(
            "source_evidence must reference the observation raw record."
        )
    return resolved_raw_record, evidence


def _validate_outcome(
    *,
    decision_type: str,
    status: str,
    target_event: EarningsEvent | None,
) -> None:
    if decision_type in _RESOLVED_DECISION_TYPES:
        if status != "resolved":
            raise InvalidInvestorRelationsDecision(
                "binding decision types require resolved status."
            )
        if target_event is None:
            raise InvalidInvestorRelationsDecision("binding decision types require a target_event.")
    elif decision_type == "conflict":
        if status != "open":
            raise InvalidInvestorRelationsDecision("conflict decisions require open status.")
    elif decision_type in _REJECTED_DECISION_TYPES:
        if status != "rejected":
            raise InvalidInvestorRelationsDecision(
                "no_match / ignored decisions require rejected status."
            )
        if decision_type == "no_match" and target_event is not None:
            raise InvalidInvestorRelationsDecision(
                "no_match decisions must not define a target_event."
            )


def _validate_context(
    *,
    decision_source: str,
    actor_user: User | None,
    sync_run: SyncRun | None,
    reason: str,
    request_id: str,
) -> None:
    if decision_source == "automatic":
        if sync_run is None:
            raise InvalidInvestorRelationsDecision("automatic decisions require sync_run.")
        if actor_user is not None:
            raise InvalidInvestorRelationsDecision(
                "automatic decisions must not define actor_user."
            )
        if request_id:
            raise InvalidInvestorRelationsDecision(
                "automatic decisions must not define request_id."
            )
        return
    if actor_user is None:
        raise InvalidInvestorRelationsDecision("manual decisions require actor_user.")
    if not reason:
        raise InvalidInvestorRelationsDecision("manual decisions require reason.")
    if not request_id:
        raise InvalidInvestorRelationsDecision("manual decisions require request_id.")


def _normalize_decision_type(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsDecision("decision_type must be a string.")
    normalized = value.strip().lower()
    if normalized not in ALLOWED_INVESTOR_RELATIONS_DECISION_TYPES:
        raise InvalidInvestorRelationsDecision(
            "decision_type must use the supported IR decision enum."
        )
    return normalized


def _normalize_status(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsDecision("status must be a string.")
    normalized = value.strip().lower()
    if normalized not in ALLOWED_INVESTOR_RELATIONS_DECISION_STATUSES:
        raise InvalidInvestorRelationsDecision("status must be open, resolved, or rejected.")
    return normalized


def _normalize_decision_source(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsDecision("decision_source must be a string.")
    normalized = value.strip().lower()
    if normalized not in ("automatic", "manual"):
        raise InvalidInvestorRelationsDecision("decision_source must be automatic or manual.")
    return normalized


def _normalize_covered_fields(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InvalidInvestorRelationsDecision("covered_fields must be a sequence of strings.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidInvestorRelationsDecision("covered_fields must contain strings.")
        field_name = item.strip().lower()
        if field_name not in ALLOWED_IR_COVERED_FIELDS:
            raise InvalidInvestorRelationsDecision(f"Unsupported IR covered field {item!r}.")
        if field_name not in normalized:
            normalized.append(field_name)
    return tuple(sorted(normalized))


def _normalize_match_factors(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidInvestorRelationsDecision("match_factors must be a JSON object.")
    try:
        normalized = normalize_json_without_credentials(
            dict(value),
            value_name="match_factors",
        )
    except AuditSecurityError as error:
        raise InvalidInvestorRelationsDecision(str(error)) from None
    if not isinstance(normalized, dict):
        raise InvalidInvestorRelationsDecision("match_factors must be a JSON object.")
    return normalized


def _normalize_rule_version(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsDecision("rule_version must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidInvestorRelationsDecision("rule_version must not be empty.")
    if len(normalized) > 100:
        raise InvalidInvestorRelationsDecision("rule_version must contain at most 100 characters.")
    return normalized


def _normalize_reason(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsDecision("reason must be a string.")
    normalized = value.strip()
    if len(normalized) > 2000:
        raise InvalidInvestorRelationsDecision("reason must contain at most 2000 characters.")
    return normalized


def _normalize_request_id(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsDecision("request_id must be a string.")
    normalized = value.strip()
    if len(normalized) > 255:
        raise InvalidInvestorRelationsDecision("request_id must contain at most 255 characters.")
    return normalized


def _normalize_decided_at(value: datetime | None) -> datetime:
    if value is None:
        return timezone.now()
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise InvalidInvestorRelationsDecision(
            "decided_at must be a timezone-aware datetime or null."
        )
    return value


def _is_decision_key_unique_violation(error: IntegrityError) -> bool:
    cause = error.__cause__
    sqlstate = getattr(cause, "sqlstate", None)
    if sqlstate is None:
        sqlstate = getattr(cause, "pgcode", None)
    if sqlstate != _UNIQUE_VIOLATION_SQLSTATE:
        return False
    constraint_name = getattr(getattr(cause, "diag", None), "constraint_name", None)
    return constraint_name == _DECISION_KEY_UNIQUE_CONSTRAINT


def _require_persisted(instance: object, *, value_name: str) -> None:
    state = getattr(instance, "_state", None)
    if state is None or getattr(state, "adding", True) or getattr(instance, "pk", None) is None:
        raise InvalidInvestorRelationsDecision(f"{value_name} must be saved before use.")
