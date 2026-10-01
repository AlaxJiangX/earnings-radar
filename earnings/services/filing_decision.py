"""Append-only persistence primitive for FilingEarningsDecision rows."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import DomainTargetType, RawDataRecord, SourceEvidence, SyncRun
from audit.security import AuditSecurityError, normalize_json_without_credentials
from audit.services import resolve_source_evidence_reference
from earnings.models import (
    ALLOWED_FILING_EARNINGS_CONFIDENCES,
    ALLOWED_FILING_EARNINGS_DECISION_SOURCES,
    ALLOWED_FILING_EARNINGS_DECISION_STATUSES,
    ALLOWED_FILING_EARNINGS_DECISION_TYPES,
    ALLOWED_FILING_EARNINGS_RELATION_TYPES,
    ALLOWED_FILING_RELEASE_CLASSIFICATIONS,
    EarningsEvent,
    FilingEarningsDecision,
)
from filings.models import Filing

if TYPE_CHECKING:
    from accounts.models import User

_UNIQUE_VIOLATION_SQLSTATE = "23505"
_DECISION_KEY_UNIQUE_CONSTRAINT = "filing_earnings_decision_key_unique"


class FilingEarningsDecisionServiceError(ValueError):
    """Base error for the FilingEarningsDecision persistence primitive."""


class InvalidFilingEarningsDecision(FilingEarningsDecisionServiceError):
    pass


class FilingEarningsDecisionIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FilingEarningsDecisionWriteResult:
    decision: FilingEarningsDecision
    created: bool


def build_filing_earnings_decision_key(
    *,
    filing_id: uuid.UUID,
    relation_type: str,
    decision_type: str,
    status: str,
    match_rule_version: str,
    classification_rule_version: str,
    decision_source: str,
    classification: str | None,
    confidence: str | None,
    match_factors: Mapping[str, object],
    target_event_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    request_id: str,
) -> str:
    """Build the deterministic identity of one FilingEarningsDecision.

    The canonical payload covers filing identity, relation type, decision and
    classification outcomes, rule versions, the normalized evidence/opportunity
    factors (ordered candidate ids and evidence digest), and the automatic or
    manual origin.  It never contains wall clock, raw filing body, or database
    insertion order.
    """

    normalized_relation = _normalize_relation_type(relation_type)
    normalized_type = _normalize_decision_type(decision_type)
    normalized_status = _normalize_status(status)
    normalized_rule = _normalize_rule_version(match_rule_version)
    normalized_source = _normalize_decision_source(decision_source)
    normalized_classification = _normalize_optional_classification(classification)
    normalized_confidence = _normalize_optional_confidence(confidence)
    normalized_classification_version = _normalize_classification_version(
        classification_rule_version,
        classification=normalized_classification,
    )
    normalized_factors = _normalize_match_factors(match_factors)
    normalized_request_id = _normalize_request_id(request_id)

    if normalized_source == "automatic":
        if actor_user_id is not None:
            raise InvalidFilingEarningsDecision("automatic decisions must not define actor_user.")
        if normalized_request_id:
            raise InvalidFilingEarningsDecision("automatic decisions must not define request_id.")
        origin: dict[str, object] = {"kind": "automatic"}
    else:
        if actor_user_id is None:
            raise InvalidFilingEarningsDecision("manual decisions require actor_user.")
        if not normalized_request_id:
            raise InvalidFilingEarningsDecision("manual decisions require request_id.")
        origin = {
            "actor_user_id": str(actor_user_id),
            "kind": "manual",
            "request_id": normalized_request_id,
        }

    identity = {
        "classification": normalized_classification,
        "classification_rule_version": normalized_classification_version,
        "confidence": normalized_confidence,
        "decision_source": normalized_source,
        "decision_type": normalized_type,
        "filing_id": str(filing_id),
        "match_factors": normalized_factors,
        "match_rule_version": normalized_rule,
        "origin": origin,
        "relation_type": normalized_relation,
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


def record_filing_earnings_decision(
    *,
    filing: Filing,
    relation_type: str,
    decision_type: str,
    status: str,
    match_rule_version: str,
    decision_source: str,
    classification: str | None = None,
    confidence: str | None = None,
    classification_rule_version: str = "",
    match_factors: Mapping[str, object] | None = None,
    reason: str = "",
    target_event: EarningsEvent | None = None,
    source_raw_data_record: RawDataRecord | None = None,
    source_evidence: SourceEvidence | None = None,
    actor_user: User | None = None,
    sync_run: SyncRun | None = None,
    request_id: str = "",
    supersedes: FilingEarningsDecision | None = None,
    decided_at: datetime | None = None,
) -> FilingEarningsDecisionWriteResult:
    """Persist one decision without choosing its outcome.

    The caller supplies the decision outcome and provenance.  This primitive
    validates persistence invariants, derives the deterministic decision key,
    and reuses an existing row when the same key is written concurrently.  It
    never touches FilingEarningsLink or chooses matching/classification rules.
    """

    normalized_relation = _normalize_relation_type(relation_type)
    normalized_type = _normalize_decision_type(decision_type)
    normalized_status = _normalize_status(status)
    normalized_rule = _normalize_rule_version(match_rule_version)
    normalized_source = _normalize_decision_source(decision_source)
    normalized_classification = _normalize_optional_classification(classification)
    normalized_confidence = _normalize_optional_confidence(confidence)
    normalized_classification_version = _normalize_classification_version(
        classification_rule_version,
        classification=normalized_classification,
    )
    normalized_factors = _normalize_match_factors(match_factors)
    normalized_reason = _normalize_reason(reason)
    normalized_request_id = _normalize_request_id(request_id)
    normalized_decided_at = _normalize_decided_at(decided_at)

    _validate_outcome(
        decision_type=normalized_type,
        status=normalized_status,
        relation_type=normalized_relation,
        classification=normalized_classification,
        classification_rule_version=normalized_classification_version,
        target_event=target_event,
    )
    _validate_context(
        decision_source=normalized_source,
        actor_user=actor_user,
        sync_run=sync_run,
        reason=normalized_reason,
        request_id=normalized_request_id,
    )
    _require_persisted(filing, value_name="filing")
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
        current_filing = _load_filing(filing)
        current_target = _load_target_event(target_event) if target_event is not None else None
        current_sync_run = _load_sync_run(sync_run) if sync_run is not None else None
        current_supersedes = _load_supersedes(supersedes) if supersedes is not None else None
        current_evidence, current_raw_record = _load_provenance(
            filing=current_filing,
            source_evidence=source_evidence,
            source_raw_data_record=source_raw_data_record,
        )
        if current_supersedes is not None and (
            current_supersedes.filing_id != current_filing.pk
            or current_supersedes.relation_type != normalized_relation
        ):
            raise InvalidFilingEarningsDecision(
                "supersedes must belong to the same filing and relation_type."
            )

        decision_key = build_filing_earnings_decision_key(
            filing_id=current_filing.pk,
            relation_type=normalized_relation,
            decision_type=normalized_type,
            status=normalized_status,
            match_rule_version=normalized_rule,
            classification_rule_version=normalized_classification_version,
            decision_source=normalized_source,
            classification=normalized_classification,
            confidence=normalized_confidence,
            match_factors=normalized_factors,
            target_event_id=current_target.pk if current_target is not None else None,
            actor_user_id=actor_user.pk if actor_user is not None else None,
            request_id=normalized_request_id,
        )

        try:
            with transaction.atomic():
                decision = FilingEarningsDecision.objects.create(
                    filing=current_filing,
                    relation_type=normalized_relation,
                    target_event=current_target,
                    decision_type=normalized_type,
                    status=normalized_status,
                    classification=normalized_classification,
                    confidence=normalized_confidence,
                    match_rule_version=normalized_rule,
                    classification_rule_version=normalized_classification_version,
                    decision_source=normalized_source,
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
                return FilingEarningsDecisionWriteResult(decision=decision, created=True)
        except IntegrityError as error:
            if not _is_decision_key_unique_violation(error):
                raise
            existing = FilingEarningsDecision.objects.filter(decision_key=decision_key).first()
            if existing is None:
                raise
            _verify_existing_decision(
                decision=existing,
                filing_id=current_filing.pk,
                relation_type=normalized_relation,
                target_event_id=current_target.pk if current_target is not None else None,
                decision_type=normalized_type,
                status=normalized_status,
                classification=normalized_classification,
                confidence=normalized_confidence,
                match_rule_version=normalized_rule,
                classification_rule_version=normalized_classification_version,
                decision_source=normalized_source,
                match_factors=normalized_factors,
                reason=normalized_reason,
                actor_user_id=actor_user.pk if actor_user is not None else None,
                request_id=normalized_request_id,
                supersedes_id=(current_supersedes.pk if current_supersedes is not None else None),
            )
            return FilingEarningsDecisionWriteResult(decision=existing, created=False)


def verify_reused_decision(
    *,
    decision: FilingEarningsDecision,
    filing_id: uuid.UUID,
    relation_type: str,
    target_event_id: uuid.UUID | None,
    decision_type: str,
    status: str,
    classification: str | None,
    confidence: str | None,
    match_rule_version: str,
    classification_rule_version: str,
    decision_source: str,
    match_factors: Mapping[str, object],
    actor_user_id: uuid.UUID | None,
    request_id: str,
) -> None:
    """Verify a reused key without requiring the original predecessor.

    A decision that already exists may have been superseded by a newer
    automatic or manual decision.  Replay verifies immutable content only.
    """

    if (
        decision.filing_id != filing_id
        or decision.relation_type != relation_type
        or decision.target_event_id != target_event_id
        or decision.decision_type != decision_type
        or decision.status != status
        or decision.classification != classification
        or decision.confidence != confidence
        or decision.match_rule_version != match_rule_version
        or decision.classification_rule_version != classification_rule_version
        or decision.decision_source != decision_source
        or decision.match_factors != dict(match_factors)
        or decision.actor_user_id != actor_user_id
        or decision.request_id != request_id
    ):
        raise FilingEarningsDecisionIntegrityError(
            "An existing FilingEarningsDecision has the same key but different immutable data."
        )


def _load_filing(filing: Filing) -> Filing:
    try:
        return Filing.objects.get(pk=filing.pk)
    except Filing.DoesNotExist as error:
        raise InvalidFilingEarningsDecision("filing no longer exists.") from error


def _load_target_event(target_event: EarningsEvent) -> EarningsEvent:
    try:
        return EarningsEvent.objects.get(pk=target_event.pk)
    except EarningsEvent.DoesNotExist as error:
        raise InvalidFilingEarningsDecision("target_event no longer exists.") from error


def _load_sync_run(sync_run: SyncRun) -> SyncRun:
    try:
        return SyncRun.objects.get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvalidFilingEarningsDecision("sync_run no longer exists.") from error


def _load_supersedes(supersedes: FilingEarningsDecision) -> FilingEarningsDecision:
    try:
        return FilingEarningsDecision.objects.get(pk=supersedes.pk)
    except FilingEarningsDecision.DoesNotExist as error:
        raise InvalidFilingEarningsDecision("supersedes no longer exists.") from error


def _load_provenance(
    *,
    filing: Filing,
    source_evidence: SourceEvidence | None,
    source_raw_data_record: RawDataRecord | None,
) -> tuple[SourceEvidence | None, RawDataRecord | None]:
    if source_evidence is None:
        if source_raw_data_record is not None:
            try:
                current = RawDataRecord.objects.get(pk=source_raw_data_record.pk)
            except RawDataRecord.DoesNotExist as error:
                raise InvalidFilingEarningsDecision(
                    "source_raw_data_record no longer exists."
                ) from error
            return None, current
        return None, None
    try:
        reference = resolve_source_evidence_reference(
            source_evidence=source_evidence,
            sync_run=None,
            target_type=DomainTargetType.FILING,
            target_id=filing.pk,
        )
    except ValueError as error:
        raise InvalidFilingEarningsDecision(str(error)) from None
    evidence = reference.evidence
    if source_raw_data_record is not None and evidence.raw_data_record_id != (
        source_raw_data_record.pk
    ):
        raise InvalidFilingEarningsDecision(
            "source_raw_data_record must match the SourceEvidence raw record."
        )
    return evidence, evidence.raw_data_record


def _verify_existing_decision(
    *,
    decision: FilingEarningsDecision,
    filing_id: uuid.UUID,
    relation_type: str,
    target_event_id: uuid.UUID | None,
    decision_type: str,
    status: str,
    classification: str | None,
    confidence: str | None,
    match_rule_version: str,
    classification_rule_version: str,
    decision_source: str,
    match_factors: dict[str, object],
    reason: str,
    actor_user_id: uuid.UUID | None,
    request_id: str,
    supersedes_id: uuid.UUID | None,
) -> None:
    verify_reused_decision(
        decision=decision,
        filing_id=filing_id,
        relation_type=relation_type,
        target_event_id=target_event_id,
        decision_type=decision_type,
        status=status,
        classification=classification,
        confidence=confidence,
        match_rule_version=match_rule_version,
        classification_rule_version=classification_rule_version,
        decision_source=decision_source,
        match_factors=match_factors,
        actor_user_id=actor_user_id,
        request_id=request_id,
    )
    if decision.reason != reason or decision.supersedes_id != supersedes_id:
        raise FilingEarningsDecisionIntegrityError(
            "An existing FilingEarningsDecision has the same key but different immutable data."
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
    relation_type: str,
    classification: str | None,
    classification_rule_version: str,
    target_event: EarningsEvent | None,
) -> None:
    if decision_type in {
        "matched_release_filing",
        "matched_periodic_filing",
        "manual_confirmed",
    }:
        if status != "resolved":
            raise InvalidFilingEarningsDecision("binding decision types require resolved status.")
        if target_event is None:
            raise InvalidFilingEarningsDecision("binding decision types require a target_event.")
    elif decision_type == "review_required":
        if status != "open":
            raise InvalidFilingEarningsDecision("review_required decisions require open status.")
    elif decision_type == "no_match":
        if status != "rejected" or target_event is not None:
            raise InvalidFilingEarningsDecision(
                "no_match decisions require rejected status and no target_event."
            )
    elif decision_type == "manual_rejected":
        if status != "rejected":
            raise InvalidFilingEarningsDecision(
                "manual_rejected decisions require rejected status."
            )

    if decision_type == "matched_periodic_filing":
        if relation_type != "PERIODIC_FILING":
            raise InvalidFilingEarningsDecision(
                "matched_periodic_filing decisions require a PERIODIC_FILING relation."
            )
        if classification is not None or classification_rule_version:
            raise InvalidFilingEarningsDecision(
                "matched_periodic_filing decisions must not define release classification."
            )
    if decision_type == "matched_release_filing":
        if relation_type != "RELEASE_FILING":
            raise InvalidFilingEarningsDecision(
                "matched_release_filing decisions require a RELEASE_FILING relation."
            )
        if classification is None or not classification_rule_version:
            raise InvalidFilingEarningsDecision(
                "matched_release_filing decisions require release classification provenance."
            )
    if decision_type == "manual_confirmed":
        if relation_type == "RELEASE_FILING":
            if classification != "YES" or not classification_rule_version:
                raise InvalidFilingEarningsDecision(
                    "manual_confirmed release links require classification YES."
                )
        elif relation_type == "PERIODIC_FILING":
            if classification is not None or classification_rule_version:
                raise InvalidFilingEarningsDecision(
                    "manual_confirmed periodic links must not define release classification."
                )
        else:
            raise InvalidFilingEarningsDecision(
                "manual_confirmed decisions require RELEASE_FILING or PERIODIC_FILING."
            )
    if classification is not None and not classification_rule_version:
        raise InvalidFilingEarningsDecision(
            "classification requires a non-empty classification_rule_version."
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
            raise InvalidFilingEarningsDecision("automatic decisions require sync_run.")
        if actor_user is not None:
            raise InvalidFilingEarningsDecision("automatic decisions must not define actor_user.")
        if request_id:
            raise InvalidFilingEarningsDecision("automatic decisions must not define request_id.")
        return
    if actor_user is None:
        raise InvalidFilingEarningsDecision("manual decisions require actor_user.")
    if not reason:
        raise InvalidFilingEarningsDecision("manual decisions require reason.")
    if not request_id:
        raise InvalidFilingEarningsDecision("manual decisions require request_id.")


def _normalize_relation_type(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidFilingEarningsDecision("relation_type must be a string.")
    normalized = value.strip()
    if normalized not in ALLOWED_FILING_EARNINGS_RELATION_TYPES:
        raise InvalidFilingEarningsDecision(
            "relation_type must use the supported Filing relation enum."
        )
    return normalized


def _normalize_decision_type(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidFilingEarningsDecision("decision_type must be a string.")
    normalized = value.strip().lower()
    if normalized not in ALLOWED_FILING_EARNINGS_DECISION_TYPES:
        raise InvalidFilingEarningsDecision(
            "decision_type must use the supported Filing decision enum."
        )
    return normalized


def _normalize_status(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidFilingEarningsDecision("status must be a string.")
    normalized = value.strip().lower()
    if normalized not in ALLOWED_FILING_EARNINGS_DECISION_STATUSES:
        raise InvalidFilingEarningsDecision("status must be open, resolved, or rejected.")
    return normalized


def _normalize_decision_source(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidFilingEarningsDecision("decision_source must be a string.")
    normalized = value.strip().lower()
    if normalized not in ALLOWED_FILING_EARNINGS_DECISION_SOURCES:
        raise InvalidFilingEarningsDecision("decision_source must be automatic or manual.")
    return normalized


def _normalize_optional_classification(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidFilingEarningsDecision("classification must be a string or null.")
    normalized = value.strip().upper()
    if normalized not in ALLOWED_FILING_RELEASE_CLASSIFICATIONS:
        raise InvalidFilingEarningsDecision(
            "classification must be YES, NO, REVIEW_REQUIRED, or null."
        )
    return normalized


def _normalize_optional_confidence(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidFilingEarningsDecision("confidence must be a string or null.")
    normalized = value.strip().upper()
    if normalized not in ALLOWED_FILING_EARNINGS_CONFIDENCES:
        raise InvalidFilingEarningsDecision(
            "confidence must be EXACT, BOUNDED_WINDOW, MANUAL, or null."
        )
    return normalized


def _normalize_classification_version(
    value: str,
    *,
    classification: str | None,
) -> str:
    if not isinstance(value, str):
        raise InvalidFilingEarningsDecision("classification_rule_version must be a string.")
    normalized = value.strip()
    if len(normalized) > 100:
        raise InvalidFilingEarningsDecision(
            "classification_rule_version must contain at most 100 characters."
        )
    if classification is not None and not normalized:
        raise InvalidFilingEarningsDecision(
            "classification requires a non-empty classification_rule_version."
        )
    return normalized


def _normalize_match_factors(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidFilingEarningsDecision("match_factors must be a JSON object.")
    try:
        normalized = normalize_json_without_credentials(
            dict(value),
            value_name="match_factors",
        )
    except AuditSecurityError as error:
        raise InvalidFilingEarningsDecision(str(error)) from None
    if not isinstance(normalized, dict):
        raise InvalidFilingEarningsDecision("match_factors must be a JSON object.")
    return normalized


def _normalize_rule_version(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidFilingEarningsDecision("match_rule_version must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidFilingEarningsDecision("match_rule_version must not be empty.")
    if len(normalized) > 100:
        raise InvalidFilingEarningsDecision(
            "match_rule_version must contain at most 100 characters."
        )
    return normalized


def _normalize_reason(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidFilingEarningsDecision("reason must be a string.")
    normalized = value.strip()
    if len(normalized) > 2000:
        raise InvalidFilingEarningsDecision("reason must contain at most 2000 characters.")
    return normalized


def _normalize_request_id(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidFilingEarningsDecision("request_id must be a string.")
    normalized = value.strip()
    if len(normalized) > 255:
        raise InvalidFilingEarningsDecision("request_id must contain at most 255 characters.")
    return normalized


def _normalize_decided_at(value: datetime | None) -> datetime:
    if value is None:
        return timezone.now()
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise InvalidFilingEarningsDecision("decided_at must be a timezone-aware datetime or null.")
    return value


def _require_persisted(instance: object, *, value_name: str) -> None:
    state = getattr(instance, "_state", None)
    if state is None or getattr(state, "adding", True) or getattr(instance, "pk", None) is None:
        raise InvalidFilingEarningsDecision(f"{value_name} must be saved before use.")
