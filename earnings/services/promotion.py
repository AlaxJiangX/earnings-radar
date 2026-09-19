from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from audit.models import AuditRecord, DataChange, SourceEvidence, SyncRun
from audit.services import (
    DataChangeWriteResult,
    InvalidEvidenceReference,
    SourceEvidenceReference,
    record_data_change,
    record_system_action,
    record_user_action,
    resolve_source_evidence_reference,
)
from earnings.identity import (
    IDENTITY_RULE_VERSION,
    derive_earnings_identity_key,
    normalize_period_type,
)
from earnings.models import EarningsEvent, IdentityStatus

if TYPE_CHECKING:
    from accounts.models import User


EARNINGS_CANDIDATE_PROMOTION_RULE_VERSION = "earnings-candidate-promotion-v1"

PROMOTION_IDENTITY_FIELDS: tuple[str, ...] = (
    "period_end_date",
    "period_type",
    "includes_q4",
    "identity_status",
    "identity_key",
    "identity_rule_version",
)

_UNIQUE_VIOLATION_SQLSTATE = "23505"
_IDENTITY_KEY_UNIQUE_CONSTRAINT = "earnings_earningsevent_identity_key_key"
_CANONICAL_BUSINESS_TUPLE_UNIQUE_CONSTRAINT = "earnings_event_canonical_business_unique"
_PROMOTION_COLLISION_CONSTRAINTS = frozenset(
    {
        _IDENTITY_KEY_UNIQUE_CONSTRAINT,
        _CANONICAL_BUSINESS_TUPLE_UNIQUE_CONSTRAINT,
    }
)


class EarningsPromotionServiceError(ValueError):
    """Base domain error for candidate promotion."""


class InvalidEarningsPromotion(EarningsPromotionServiceError):
    pass


class EarningsPromotionIntegrityError(RuntimeError):
    pass


class EarningsPromotionCollision(EarningsPromotionServiceError):
    """A different canonical event already owns the derived identity."""

    def __init__(
        self,
        *,
        candidate_id: UUID,
        existing_canonical_id: UUID,
        derived_identity_key: str,
    ) -> None:
        self.candidate_id = candidate_id
        self.existing_canonical_id = existing_canonical_id
        self.derived_identity_key = derived_identity_key
        super().__init__(
            "Earnings identity "
            f"{derived_identity_key} is already owned by canonical event "
            f"{existing_canonical_id}; candidate {candidate_id} was not promoted."
        )


@dataclass(frozen=True, slots=True)
class EarningsPromotionWriteResult:
    earnings_event: EarningsEvent
    changed: bool
    data_changes: tuple[DataChangeWriteResult, ...] = ()
    audit_record: AuditRecord | None = None


@dataclass(frozen=True, slots=True)
class _PromotionContext:
    source_evidence: SourceEvidence | None
    sync_run: SyncRun | None
    actor_user: User | None
    reason: str
    request_id: str
    ip_address: str | None


def _is_canonical_identity_unique_violation(error: IntegrityError) -> bool:
    """Return True only for a recognized canonical identity unique violation."""

    cause = error.__cause__
    sqlstate = getattr(cause, "sqlstate", None)
    if sqlstate is None:
        sqlstate = getattr(cause, "pgcode", None)
    if sqlstate != _UNIQUE_VIOLATION_SQLSTATE:
        return False
    constraint_name = getattr(getattr(cause, "diag", None), "constraint_name", None)
    return constraint_name in _PROMOTION_COLLISION_CONSTRAINTS


def promote_earnings_event(
    *,
    earnings_event: EarningsEvent,
    period_end_date: date,
    period_type: str,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
    changed_at: datetime | None = None,
) -> EarningsPromotionWriteResult:
    """Complete a candidate EarningsEvent into its canonical identity.

    Promotion is an in-place completion of the same row. It never corrects an
    existing canonical identity, merges candidates, or moves schedule/status
    history. Caller-owned event fields are ignored; only the persisted row is
    locked and evaluated.
    """

    event_id = _require_event_id(earnings_event)
    timestamp = _normalize_timestamp(changed_at)

    with transaction.atomic():
        try:
            current = EarningsEvent.objects.select_for_update().get(pk=event_id)
        except EarningsEvent.DoesNotExist as error:
            raise InvalidEarningsPromotion("EarningsEvent must exist before promotion.") from error

        normalized_date = _normalize_period_end_date(period_end_date)
        normalized_type, includes_q4 = _normalize_period_type(period_type)
        target_identity_key = derive_earnings_identity_key(
            company_id=current.company_id,
            period_end_date=normalized_date,
            period_type=normalized_type,
        )

        if current.identity_status == IdentityStatus.CANONICAL:
            _verify_canonical_replay(
                current=current,
                target_date=normalized_date,
                target_type=normalized_type,
                target_includes_q4=includes_q4,
                target_identity_key=target_identity_key,
            )
            return EarningsPromotionWriteResult(
                earnings_event=current,
                changed=False,
            )
        if current.identity_status != IdentityStatus.CANDIDATE:
            raise InvalidEarningsPromotion("identity_status must be candidate or canonical.")

        _validate_candidate_completion(
            current=current,
            target_date=normalized_date,
            target_type=normalized_type,
        )
        changes = _build_identity_changes(
            current=current,
            target_date=normalized_date,
            target_type=normalized_type,
            target_includes_q4=includes_q4,
            target_identity_key=target_identity_key,
        )

        existing = _precheck_canonical_owner(
            company_id=current.company_id,
            period_end_date=normalized_date,
            period_type=normalized_type,
            identity_key=target_identity_key,
            exclude_event_id=current.pk,
        )
        if existing is not None:
            raise _collision(
                candidate=current,
                existing=existing,
                derived_identity_key=target_identity_key,
            )

        context = _resolve_promotion_context(
            earnings_event=current,
            changed_field_names=tuple(field_name for field_name, _old_value, _new_value in changes),
            source_evidence=source_evidence,
            sync_run=sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        before_values = _identity_values_from_event(current)

        try:
            with transaction.atomic():
                for field_name, _old_value, new_value in changes:
                    setattr(current, field_name, new_value)
                current.save(
                    update_fields=(
                        *(field_name for field_name, _old_value, _new_value in changes),
                        "updated_at",
                    )
                )
        except IntegrityError as error:
            if not _is_canonical_identity_unique_violation(error):
                raise
            existing = _find_canonical_owner(
                company_id=current.company_id,
                period_end_date=normalized_date,
                period_type=normalized_type,
                identity_key=target_identity_key,
                exclude_event_id=current.pk,
            )
            if existing is not None:
                raise _collision(
                    candidate=current,
                    existing=existing,
                    derived_identity_key=target_identity_key,
                ) from None
            raise

        data_changes = _record_identity_data_changes(
            current=current,
            changes=changes,
            context=context,
            changed_at=timestamp,
        )
        audit_record = _record_promotion_audit(
            current=current,
            before_values=before_values,
            changes=changes,
            context=context,
        )
        return EarningsPromotionWriteResult(
            earnings_event=current,
            changed=True,
            data_changes=data_changes,
            audit_record=audit_record,
        )


def _require_event_id(earnings_event: EarningsEvent) -> UUID:
    if earnings_event._state.adding or earnings_event.pk is None:
        raise InvalidEarningsPromotion("EarningsEvent must be saved before promotion.")
    return earnings_event.pk


def _normalize_timestamp(value: datetime | None) -> datetime:
    timestamp = value or timezone.now()
    if timezone.is_naive(timestamp):
        raise InvalidEarningsPromotion("changed_at must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _normalize_period_end_date(value: date) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise InvalidEarningsPromotion("period_end_date must be a date.")
    return value


def _normalize_period_type(value: str) -> tuple[str, bool]:
    if not isinstance(value, str):
        raise InvalidEarningsPromotion("period_type must be a domain period label.")
    normalized_type, includes_q4 = normalize_period_type(value)
    if normalized_type is None:
        raise InvalidEarningsPromotion(
            f"period_type {value!r} cannot be normalized to a canonical period."
        )
    return normalized_type, includes_q4


def _verify_canonical_replay(
    *,
    current: EarningsEvent,
    target_date: date,
    target_type: str,
    target_includes_q4: bool,
    target_identity_key: str,
) -> None:
    if current.period_end_date != target_date or current.period_type != target_type:
        raise InvalidEarningsPromotion(
            "Canonical EarningsEvent identity differs from the promotion input."
        )
    if current.includes_q4 != target_includes_q4:
        raise EarningsPromotionIntegrityError(
            "Canonical EarningsEvent includes_q4 is inconsistent with its period type."
        )
    if (
        current.identity_key != target_identity_key
        or current.identity_rule_version != IDENTITY_RULE_VERSION
    ):
        raise EarningsPromotionIntegrityError(
            "Canonical EarningsEvent identity metadata is inconsistent with its facts."
        )


def _validate_candidate_completion(
    *,
    current: EarningsEvent,
    target_date: date,
    target_type: str,
) -> None:
    if current.identity_key is not None or current.identity_rule_version is not None:
        raise EarningsPromotionIntegrityError(
            "Candidate EarningsEvent must not carry canonical identity metadata."
        )
    if current.period_end_date is not None and current.period_end_date != target_date:
        raise InvalidEarningsPromotion(
            "Candidate period_end_date conflicts with the promotion input."
        )
    if current.period_type is not None and current.period_type != target_type:
        raise InvalidEarningsPromotion("Candidate period_type conflicts with the promotion input.")


def _build_identity_changes(
    *,
    current: EarningsEvent,
    target_date: date,
    target_type: str,
    target_includes_q4: bool,
    target_identity_key: str,
) -> tuple[tuple[str, object, object], ...]:
    candidates: tuple[tuple[str, object, object], ...] = (
        ("period_end_date", current.period_end_date, target_date),
        ("period_type", current.period_type, target_type),
        ("includes_q4", current.includes_q4, target_includes_q4),
        ("identity_status", current.identity_status, IdentityStatus.CANONICAL),
        ("identity_key", current.identity_key, target_identity_key),
        ("identity_rule_version", current.identity_rule_version, IDENTITY_RULE_VERSION),
    )
    return tuple(item for item in candidates if item[1] != item[2])


def _precheck_canonical_owner(
    *,
    company_id: UUID,
    period_end_date: date,
    period_type: str,
    identity_key: str,
    exclude_event_id: UUID,
) -> EarningsEvent | None:
    return _find_canonical_owner(
        company_id=company_id,
        period_end_date=period_end_date,
        period_type=period_type,
        identity_key=identity_key,
        exclude_event_id=exclude_event_id,
    )


def _find_canonical_owner(
    *,
    company_id: UUID,
    period_end_date: date,
    period_type: str,
    identity_key: str,
    exclude_event_id: UUID,
) -> EarningsEvent | None:
    return (
        EarningsEvent.objects.filter(identity_status=IdentityStatus.CANONICAL)
        .filter(
            Q(identity_key=identity_key)
            | Q(
                company_id=company_id,
                period_end_date=period_end_date,
                period_type=period_type,
            )
        )
        .exclude(pk=exclude_event_id)
        .order_by("created_at", "pk")
        .first()
    )


def _collision(
    *,
    candidate: EarningsEvent,
    existing: EarningsEvent,
    derived_identity_key: str,
) -> EarningsPromotionCollision:
    return EarningsPromotionCollision(
        candidate_id=candidate.pk,
        existing_canonical_id=existing.pk,
        derived_identity_key=derived_identity_key,
    )


def _resolve_promotion_context(
    *,
    earnings_event: EarningsEvent,
    changed_field_names: tuple[str, ...],
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    request_id: str,
    ip_address: str | None,
) -> _PromotionContext:
    if actor_user is None and source_evidence is None and sync_run is None:
        raise InvalidEarningsPromotion(
            "Promotion requires an actor or machine provenance (SourceEvidence or SyncRun)."
        )
    if actor_user is not None and not reason.strip():
        raise InvalidEarningsPromotion("Manual promotion requires a reason.")
    if actor_user is not None and not request_id.strip():
        raise InvalidEarningsPromotion("Manual promotion requires a stable request_id.")

    evidence_reference: SourceEvidenceReference | None = None
    if source_evidence is not None:
        try:
            evidence_reference = resolve_source_evidence_reference(
                source_evidence=source_evidence,
                sync_run=sync_run,
                target_type=DataChange.TargetType.EARNINGS_EVENT,
                target_id=earnings_event.pk,
                field_names=changed_field_names,
            )
        except InvalidEvidenceReference as error:
            raise InvalidEarningsPromotion(str(error)) from None

    resolved_sync_run = (
        evidence_reference.sync_run if evidence_reference is not None else _load_sync_run(sync_run)
    )
    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        if resolved_sync_run is None:
            raise InvalidEarningsPromotion(
                "Automatic promotion requires a SyncRun or SourceEvidence."
            )
        normalized_request_id = f"sync-run:{resolved_sync_run.pk}"

    return _PromotionContext(
        source_evidence=evidence_reference.evidence if evidence_reference else None,
        sync_run=resolved_sync_run,
        actor_user=actor_user,
        reason=reason.strip(),
        request_id=normalized_request_id,
        ip_address=ip_address,
    )


def _load_sync_run(sync_run: SyncRun | None) -> SyncRun | None:
    if sync_run is None:
        return None
    if sync_run._state.adding or sync_run.pk is None:
        raise InvalidEarningsPromotion("sync_run must be saved before use.")
    try:
        return SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvalidEarningsPromotion("sync_run must exist before use.") from error


def _record_identity_data_changes(
    *,
    current: EarningsEvent,
    changes: tuple[tuple[str, object, object], ...],
    context: _PromotionContext,
    changed_at: datetime,
) -> tuple[DataChangeWriteResult, ...]:
    results: list[DataChangeWriteResult] = []
    for field_name, old_value, new_value in changes:
        result = record_data_change(
            target_type=DataChange.TargetType.EARNINGS_EVENT,
            target_id=current.pk,
            field_name=field_name,
            old_value=_json_identity_value(old_value),
            new_value=_json_identity_value(new_value),
            rule_version=EARNINGS_CANDIDATE_PROMOTION_RULE_VERSION,
            source_evidence=_evidence_for_field(context, field_name),
            sync_run=context.sync_run,
            actor_user=context.actor_user,
            reason=context.reason,
            origin_key=context.request_id if context.actor_user is not None else "",
            changed_at=changed_at,
        )
        if result.change is None or result.skipped:
            raise EarningsPromotionIntegrityError(
                "A changed promotion identity field must produce a DataChange."
            )
        if not result.created:
            raise EarningsPromotionIntegrityError(
                "Promotion DataChange already exists for a still-candidate event."
            )
        results.append(result)
    return tuple(results)


def _evidence_for_field(
    context: _PromotionContext,
    field_name: str,
) -> SourceEvidence | None:
    evidence = context.source_evidence
    if evidence is None:
        return None
    if evidence.field_name not in ("", field_name):
        return None
    return evidence


def _record_promotion_audit(
    *,
    current: EarningsEvent,
    before_values: dict[str, object],
    changes: tuple[tuple[str, object, object], ...],
    context: _PromotionContext,
) -> AuditRecord:
    after_values = dict(before_values)
    for field_name, _old_value, new_value in changes:
        after_values[field_name] = new_value
    before = {
        field_name: _json_identity_value(before_values[field_name])
        for field_name in PROMOTION_IDENTITY_FIELDS
    }
    after = {
        field_name: _json_identity_value(after_values[field_name])
        for field_name in PROMOTION_IDENTITY_FIELDS
    }

    if context.actor_user is not None:
        result = record_user_action(
            actor_user=context.actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.EARNINGS_EVENT,
            target_id=current.pk,
            before=before,
            after=after,
            reason=context.reason,
            request_id=context.request_id,
            ip_address=context.ip_address,
            sync_run=context.sync_run,
        )
    else:
        if context.sync_run is None:
            raise InvalidEarningsPromotion(
                "Automatic promotion requires a SyncRun or SourceEvidence."
            )
        result = record_system_action(
            sync_run=context.sync_run,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.EARNINGS_EVENT,
            target_id=current.pk,
            before=before,
            after=after,
            request_id=context.request_id,
        )
    return result.record


def _identity_values_from_event(earnings_event: EarningsEvent) -> dict[str, object]:
    return {
        field_name: getattr(earnings_event, field_name) for field_name in PROMOTION_IDENTITY_FIELDS
    }


def _json_identity_value(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    return value
