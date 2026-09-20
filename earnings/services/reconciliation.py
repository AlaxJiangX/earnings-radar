from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import SyncRun
from audit.security import AuditSecurityError, normalize_json_without_credentials
from earnings.models import (
    ALLOWED_RECONCILIATION_COVERED_FIELDS,
    ALLOWED_RECONCILIATION_DECISION_STATUSES,
    ALLOWED_RECONCILIATION_DECISION_TYPES,
    OPEN_RECONCILIATION_DECISION_TYPES,
    REJECTED_RECONCILIATION_DECISION_TYPES,
    RESOLVED_RECONCILIATION_DECISION_TYPES,
    EarningsCalendarObservation,
    EarningsEvent,
    EarningsReconciliationDecision,
)

if TYPE_CHECKING:
    from accounts.models import User

_UNIQUE_VIOLATION_SQLSTATE = "23505"
_DECISION_KEY_UNIQUE_CONSTRAINT = "earnings_reconciliation_decision_key_unique"


class EarningsReconciliationDecisionServiceError(ValueError):
    """Base error for the reconciliation decision persistence primitive."""


class InvalidEarningsReconciliationDecision(EarningsReconciliationDecisionServiceError):
    pass


class EarningsReconciliationDecisionIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EarningsReconciliationDecisionWriteResult:
    decision: EarningsReconciliationDecision
    created: bool


def build_earnings_reconciliation_decision_key(
    *,
    observation_id: uuid.UUID,
    decision_type: str,
    status: str,
    target_event_id: uuid.UUID | None,
    covered_fields: Sequence[str],
    match_factors: Mapping[str, object],
    rule_version: str,
    supersedes_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    request_id: str,
) -> str:
    """Build the deterministic identity of one reconciliation decision."""

    normalized_type = _normalize_decision_type(decision_type)
    normalized_status = _normalize_status(status)
    normalized_fields = _normalize_covered_fields(covered_fields)
    normalized_factors = _normalize_match_factors(match_factors)
    normalized_rule = _normalize_rule_version(rule_version)
    normalized_request_id = _normalize_request_id(request_id)

    if actor_user_id is None:
        if normalized_request_id:
            raise InvalidEarningsReconciliationDecision(
                "automatic decisions must not define request_id."
            )
        origin: dict[str, object] = {"kind": "automatic"}
    else:
        if not normalized_request_id:
            raise InvalidEarningsReconciliationDecision("manual decisions require request_id.")
        origin = {
            "actor_user_id": str(actor_user_id),
            "kind": "manual",
            "request_id": normalized_request_id,
        }

    identity = {
        "covered_fields": list(normalized_fields),
        "decision_type": normalized_type,
        "match_factors": normalized_factors,
        "observation_id": str(observation_id),
        "origin": origin,
        "rule_version": normalized_rule,
        "status": normalized_status,
        "supersedes_id": str(supersedes_id) if supersedes_id is not None else None,
        "target_event_id": str(target_event_id) if target_event_id is not None else None,
    }
    serialized = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def record_earnings_reconciliation_decision(
    *,
    observation: EarningsCalendarObservation,
    decision_type: str,
    status: str,
    rule_version: str,
    target_event: EarningsEvent | None = None,
    covered_fields: Sequence[str] = (),
    match_factors: Mapping[str, object] | None = None,
    reason: str = "",
    actor_user: User | None = None,
    sync_run: SyncRun | None = None,
    request_id: str = "",
    supersedes: EarningsReconciliationDecision | None = None,
    decided_at: datetime | None = None,
) -> EarningsReconciliationDecisionWriteResult:
    """Persist one reconciliation decision without choosing its outcome.

    The caller supplies the decision type, status, target and evidence. This
    primitive validates persistence invariants and computes the deterministic
    decision identity; it does not match companies or periods, create
    candidates, resolve effective decisions, or write AuditRecord rows.
    """

    normalized_type = _normalize_decision_type(decision_type)
    normalized_status = _normalize_status(status)
    normalized_fields = _normalize_covered_fields(covered_fields)
    normalized_factors = _normalize_match_factors(match_factors)
    normalized_rule = _normalize_rule_version(rule_version)
    normalized_reason = _normalize_reason(reason)
    normalized_request_id = _normalize_request_id(request_id)
    normalized_decided_at = _normalize_decided_at(decided_at)

    _validate_outcome(
        decision_type=normalized_type,
        status=normalized_status,
        target_event=target_event,
    )
    _validate_context(
        actor_user=actor_user,
        sync_run=sync_run,
        reason=normalized_reason,
        request_id=normalized_request_id,
    )
    _validate_covered_fields(
        covered_fields=normalized_fields,
        actor_user=actor_user,
        status=normalized_status,
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

    with transaction.atomic():
        current_observation = _load_observation(observation)
        current_target_event = (
            _load_target_event(target_event) if target_event is not None else None
        )
        current_sync_run = _load_sync_run(sync_run) if sync_run is not None else None
        current_supersedes = _load_supersedes(supersedes) if supersedes is not None else None
        if (
            current_supersedes is not None
            and current_supersedes.observation_id != current_observation.pk
        ):
            raise InvalidEarningsReconciliationDecision(
                "supersedes must belong to the same observation."
            )

        decision_key = build_earnings_reconciliation_decision_key(
            observation_id=current_observation.pk,
            decision_type=normalized_type,
            status=normalized_status,
            target_event_id=(current_target_event.pk if current_target_event is not None else None),
            covered_fields=normalized_fields,
            match_factors=normalized_factors,
            rule_version=normalized_rule,
            supersedes_id=(current_supersedes.pk if current_supersedes is not None else None),
            actor_user_id=actor_user.pk if actor_user is not None else None,
            request_id=normalized_request_id,
        )

        try:
            with transaction.atomic():
                decision = EarningsReconciliationDecision.objects.create(
                    observation=current_observation,
                    decision_type=normalized_type,
                    status=normalized_status,
                    target_event=current_target_event,
                    covered_fields=list(normalized_fields),
                    rule_version=normalized_rule,
                    match_factors=normalized_factors,
                    reason=normalized_reason,
                    actor_user=actor_user,
                    sync_run=current_sync_run,
                    request_id=normalized_request_id,
                    decided_at=normalized_decided_at,
                    supersedes=current_supersedes,
                    decision_key=decision_key,
                )
                return EarningsReconciliationDecisionWriteResult(
                    decision=decision,
                    created=True,
                )
        except IntegrityError as error:
            if not _is_decision_key_unique_violation(error):
                raise
            existing = EarningsReconciliationDecision.objects.filter(
                decision_key=decision_key
            ).first()
            if existing is None:
                raise
            _verify_existing_decision(
                decision=existing,
                observation_id=current_observation.pk,
                decision_type=normalized_type,
                status=normalized_status,
                target_event_id=(
                    current_target_event.pk if current_target_event is not None else None
                ),
                covered_fields=normalized_fields,
                rule_version=normalized_rule,
                match_factors=normalized_factors,
                reason=normalized_reason,
                actor_user_id=actor_user.pk if actor_user is not None else None,
                request_id=normalized_request_id,
                supersedes_id=(current_supersedes.pk if current_supersedes is not None else None),
            )
            return EarningsReconciliationDecisionWriteResult(
                decision=existing,
                created=False,
            )


def _load_observation(
    observation: EarningsCalendarObservation,
) -> EarningsCalendarObservation:
    _require_persisted(observation, value_name="observation")
    try:
        return EarningsCalendarObservation.objects.get(pk=observation.pk)
    except EarningsCalendarObservation.DoesNotExist as error:
        raise InvalidEarningsReconciliationDecision("observation no longer exists.") from error


def _load_target_event(target_event: EarningsEvent) -> EarningsEvent:
    _require_persisted(target_event, value_name="target_event")
    try:
        return EarningsEvent.objects.get(pk=target_event.pk)
    except EarningsEvent.DoesNotExist as error:
        raise InvalidEarningsReconciliationDecision("target_event no longer exists.") from error


def _load_sync_run(sync_run: SyncRun) -> SyncRun:
    _require_persisted(sync_run, value_name="sync_run")
    try:
        return SyncRun.objects.get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvalidEarningsReconciliationDecision("sync_run no longer exists.") from error


def _load_supersedes(
    supersedes: EarningsReconciliationDecision,
) -> EarningsReconciliationDecision:
    _require_persisted(supersedes, value_name="supersedes")
    try:
        return EarningsReconciliationDecision.objects.get(pk=supersedes.pk)
    except EarningsReconciliationDecision.DoesNotExist as error:
        raise InvalidEarningsReconciliationDecision("supersedes no longer exists.") from error


def _verify_existing_decision(
    *,
    decision: EarningsReconciliationDecision,
    observation_id: uuid.UUID,
    decision_type: str,
    status: str,
    target_event_id: uuid.UUID | None,
    covered_fields: tuple[str, ...],
    rule_version: str,
    match_factors: dict[str, object],
    reason: str,
    actor_user_id: uuid.UUID | None,
    request_id: str,
    supersedes_id: uuid.UUID | None,
) -> None:
    if (
        decision.observation_id != observation_id
        or decision.decision_type != decision_type
        or decision.status != status
        or decision.target_event_id != target_event_id
        or decision.covered_fields != list(covered_fields)
        or decision.rule_version != rule_version
        or decision.match_factors != match_factors
        or decision.reason != reason
        or decision.actor_user_id != actor_user_id
        or decision.request_id != request_id
        or decision.supersedes_id != supersedes_id
    ):
        raise EarningsReconciliationDecisionIntegrityError(
            "An existing EarningsReconciliationDecision has the same key "
            "but different immutable data."
        )


def _is_decision_key_unique_violation(error: IntegrityError) -> bool:
    cause = error.__cause__
    sqlstate = getattr(cause, "sqlstate", None)
    if sqlstate is None:
        sqlstate = getattr(cause, "pgcode", None)
    if sqlstate != _UNIQUE_VIOLATION_SQLSTATE:
        return False
    constraint_name = getattr(getattr(cause, "diag", None), "constraint_name", None)
    return constraint_name == _DECISION_KEY_UNIQUE_CONSTRAINT


def _validate_outcome(
    *,
    decision_type: str,
    status: str,
    target_event: EarningsEvent | None,
) -> None:
    if status == "resolved":
        if decision_type not in RESOLVED_RECONCILIATION_DECISION_TYPES:
            raise InvalidEarningsReconciliationDecision(
                "resolved decisions require a binding decision_type."
            )
        if target_event is None:
            raise InvalidEarningsReconciliationDecision("resolved decisions require target_event.")
        return
    if status == "open":
        if decision_type not in OPEN_RECONCILIATION_DECISION_TYPES:
            raise InvalidEarningsReconciliationDecision(
                "open decisions require an open decision_type."
            )
        return
    if decision_type not in REJECTED_RECONCILIATION_DECISION_TYPES:
        raise InvalidEarningsReconciliationDecision(
            "rejected decisions require a rejected decision_type."
        )
    if target_event is not None:
        raise InvalidEarningsReconciliationDecision(
            "rejected decisions must not define target_event."
        )


def _validate_context(
    *,
    actor_user: User | None,
    sync_run: SyncRun | None,
    reason: str,
    request_id: str,
) -> None:
    if actor_user is not None:
        if not reason:
            raise InvalidEarningsReconciliationDecision("manual decisions require reason.")
        if not request_id:
            raise InvalidEarningsReconciliationDecision("manual decisions require request_id.")
        return
    if sync_run is None:
        raise InvalidEarningsReconciliationDecision("automatic decisions require sync_run.")
    if request_id:
        raise InvalidEarningsReconciliationDecision(
            "automatic decisions must not define request_id."
        )


def _validate_covered_fields(
    *,
    covered_fields: tuple[str, ...],
    actor_user: User | None,
    status: str,
) -> None:
    if not covered_fields:
        return
    if status != "resolved":
        raise InvalidEarningsReconciliationDecision(
            "only resolved decisions may define covered_fields."
        )
    if actor_user is None:
        raise InvalidEarningsReconciliationDecision(
            "automatic decisions must not define covered_fields."
        )


def _normalize_decision_type(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsReconciliationDecision("decision_type must be a string.")
    normalized = value.strip().lower()
    if normalized not in ALLOWED_RECONCILIATION_DECISION_TYPES:
        raise InvalidEarningsReconciliationDecision(
            "decision_type must use the supported reconciliation enum."
        )
    return normalized


def _normalize_status(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsReconciliationDecision("status must be a string.")
    normalized = value.strip().lower()
    if normalized not in ALLOWED_RECONCILIATION_DECISION_STATUSES:
        raise InvalidEarningsReconciliationDecision(
            "status must use the supported reconciliation enum."
        )
    return normalized


def _normalize_covered_fields(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str | bytes | bytearray):
        raise InvalidEarningsReconciliationDecision(
            "covered_fields must be a sequence of field names."
        )
    try:
        raw_fields = list(value)
    except TypeError as error:
        raise InvalidEarningsReconciliationDecision(
            "covered_fields must be a sequence of field names."
        ) from error
    normalized: set[str] = set()
    for raw_field in raw_fields:
        if not isinstance(raw_field, str):
            raise InvalidEarningsReconciliationDecision("covered_fields must contain only strings.")
        field_name = raw_field.strip().lower()
        if field_name not in ALLOWED_RECONCILIATION_COVERED_FIELDS:
            raise InvalidEarningsReconciliationDecision(
                "covered_fields contains an unsupported field."
            )
        normalized.add(field_name)
    return tuple(sorted(normalized))


def _normalize_match_factors(
    value: Mapping[str, object] | None,
) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidEarningsReconciliationDecision("match_factors must be a JSON object.")
    try:
        normalized = normalize_json_without_credentials(
            dict(value),
            value_name="match_factors",
        )
    except AuditSecurityError as error:
        raise InvalidEarningsReconciliationDecision(str(error)) from None
    if not isinstance(normalized, dict):
        raise InvalidEarningsReconciliationDecision("match_factors must be a JSON object.")
    return normalized


def _normalize_rule_version(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsReconciliationDecision("rule_version must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidEarningsReconciliationDecision("rule_version must not be empty.")
    if len(normalized) > 100:
        raise InvalidEarningsReconciliationDecision(
            "rule_version must contain at most 100 characters."
        )
    return normalized


def _normalize_reason(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsReconciliationDecision("reason must be a string.")
    normalized = value.strip()
    if len(normalized) > 2000:
        raise InvalidEarningsReconciliationDecision("reason must contain at most 2000 characters.")
    return normalized


def _normalize_request_id(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsReconciliationDecision("request_id must be a string.")
    normalized = value.strip()
    if len(normalized) > 255:
        raise InvalidEarningsReconciliationDecision(
            "request_id must contain at most 255 characters."
        )
    return normalized


def _normalize_decided_at(value: datetime | None) -> datetime:
    if value is None:
        return timezone.now()
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise InvalidEarningsReconciliationDecision(
            "decided_at must be a timezone-aware datetime or null."
        )
    return value


def _require_persisted(instance: object, *, value_name: str) -> None:
    state = getattr(instance, "_state", None)
    if state is None or getattr(state, "adding", True) or getattr(instance, "pk", None) is None:
        raise InvalidEarningsReconciliationDecision(f"{value_name} must be saved before use.")
