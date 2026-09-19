from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction
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
from earnings.models import EarningsEvent, EventStatus

if TYPE_CHECKING:
    from accounts.models import User


EARNINGS_STATUS_LIFECYCLE_RULE_VERSION = "earnings-status-lifecycle-v1"
STATUS_FIELD_NAME = "status"


class EarningsStatusServiceError(ValueError):
    """Base domain error for controlled EarningsEvent status mutations."""


class InvalidEarningsTargetStatus(EarningsStatusServiceError):
    pass


class InvalidEarningsStatusTransition(EarningsStatusServiceError):
    pass


class InvalidEarningsStatusCorrection(EarningsStatusServiceError):
    pass


class InvalidEarningsStatusReinstatement(EarningsStatusServiceError):
    pass


class InvalidEarningsStatusContext(EarningsStatusServiceError):
    pass


class EarningsStatusIdentityUncertain(EarningsStatusServiceError):
    pass


class InvalidEarningsStatusEvidence(EarningsStatusServiceError):
    pass


class EarningsStatusIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EarningsStatusWriteResult:
    earnings_event: EarningsEvent
    previous_status: str
    status: str
    changed: bool
    data_change: DataChangeWriteResult | None
    audit_record: AuditRecord | None


@dataclass(frozen=True, slots=True)
class _MutationContext:
    source_evidence: SourceEvidence | None
    sync_run: SyncRun | None
    actor_user: User | None
    reason: str
    request_id: str
    ip_address: str | None


_NORMAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (EventStatus.SCHEDULED_ESTIMATED, EventStatus.SCHEDULED_CONFIRMED),
        (EventStatus.SCHEDULED_ESTIMATED, EventStatus.RELEASED),
        (EventStatus.SCHEDULED_ESTIMATED, EventStatus.CANCELLED),
        (EventStatus.SCHEDULED_CONFIRMED, EventStatus.RELEASED),
        (EventStatus.SCHEDULED_CONFIRMED, EventStatus.CANCELLED),
    }
)
_CORRECTION_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (EventStatus.SCHEDULED_CONFIRMED, EventStatus.SCHEDULED_ESTIMATED),
        (EventStatus.RELEASED, EventStatus.SCHEDULED_CONFIRMED),
        (EventStatus.RELEASED, EventStatus.SCHEDULED_ESTIMATED),
        (EventStatus.RELEASED, EventStatus.CANCELLED),
        (EventStatus.CANCELLED, EventStatus.SCHEDULED_ESTIMATED),
        (EventStatus.CANCELLED, EventStatus.SCHEDULED_CONFIRMED),
        (EventStatus.CANCELLED, EventStatus.RELEASED),
    }
)
_REINSTATEMENT_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (EventStatus.CANCELLED, EventStatus.SCHEDULED_ESTIMATED),
        (EventStatus.CANCELLED, EventStatus.SCHEDULED_CONFIRMED),
        (EventStatus.CANCELLED, EventStatus.RELEASED),
    }
)


def transition_earnings_status(
    *,
    earnings_event: EarningsEvent,
    target_status: str,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
    changed_at: datetime | None = None,
) -> EarningsStatusWriteResult:
    """Apply one normal, non-cancellation lifecycle transition.

    Cancellation is intentionally excluded: callers must use
    ``cancel_earnings_event`` so an affirmative cancellation intent is explicit.
    """

    normalized_target = _normalize_target_status(target_status)
    if normalized_target == EventStatus.CANCELLED:
        raise InvalidEarningsStatusTransition("Cancellation must use cancel_earnings_event().")
    return _apply_status_mutation(
        earnings_event=earnings_event,
        target_status=normalized_target,
        allowed_transitions=_NORMAL_TRANSITIONS,
        source_evidence=source_evidence,
        sync_run=sync_run,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        changed_at=changed_at,
        audit_action=AuditRecord.Action.UPDATE,
    )


def confirm_earnings_event(
    *,
    earnings_event: EarningsEvent,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
    changed_at: datetime | None = None,
) -> EarningsStatusWriteResult:
    return transition_earnings_status(
        earnings_event=earnings_event,
        target_status=EventStatus.SCHEDULED_CONFIRMED,
        source_evidence=source_evidence,
        sync_run=sync_run,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        changed_at=changed_at,
    )


def mark_earnings_released(
    *,
    earnings_event: EarningsEvent,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
    changed_at: datetime | None = None,
) -> EarningsStatusWriteResult:
    return transition_earnings_status(
        earnings_event=earnings_event,
        target_status=EventStatus.RELEASED,
        source_evidence=source_evidence,
        sync_run=sync_run,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        changed_at=changed_at,
    )


def cancel_earnings_event(
    *,
    earnings_event: EarningsEvent,
    affirmative_cancellation: bool = False,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
    changed_at: datetime | None = None,
) -> EarningsStatusWriteResult:
    """Cancel an event only when the caller explicitly confirms cancellation.

    ``affirmative_cancellation`` is required for automatic/source-only
    cancellations so a missing provider record or generic empty observation
    cannot be mapped to ``cancelled`` accidentally. Manual actor-driven
    cancellations carry their intent through the actor/reason/request context.
    """

    return _apply_status_mutation(
        earnings_event=earnings_event,
        target_status=EventStatus.CANCELLED,
        allowed_transitions=_NORMAL_TRANSITIONS,
        source_evidence=source_evidence,
        sync_run=sync_run,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        changed_at=changed_at,
        audit_action=AuditRecord.Action.UPDATE,
        require_affirmative_cancellation=True,
        affirmative_cancellation=affirmative_cancellation,
    )


def correct_earnings_status(
    *,
    earnings_event: EarningsEvent,
    target_status: str,
    actor_user: User,
    reason: str,
    request_id: str,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    ip_address: str | None = None,
    changed_at: datetime | None = None,
) -> EarningsStatusWriteResult:
    """Correct a previously recorded status that is known to be a false fact."""

    normalized_target = _normalize_target_status(target_status)
    return _apply_status_mutation(
        earnings_event=earnings_event,
        target_status=normalized_target,
        allowed_transitions=_CORRECTION_TRANSITIONS,
        source_evidence=source_evidence,
        sync_run=sync_run,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        changed_at=changed_at,
        audit_action=AuditRecord.Action.MANUAL_CORRECTION,
        path_error_type=InvalidEarningsStatusCorrection,
    )


def reinstate_earnings_event(
    *,
    earnings_event: EarningsEvent,
    target_status: str,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
    changed_at: datetime | None = None,
) -> EarningsStatusWriteResult:
    """Restore a genuinely cancelled event under the same canonical identity.

    No identity, schedule or intermediate status mutation is performed.
    """

    normalized_target = _normalize_target_status(target_status)
    return _apply_status_mutation(
        earnings_event=earnings_event,
        target_status=normalized_target,
        allowed_transitions=_REINSTATEMENT_TRANSITIONS,
        source_evidence=source_evidence,
        sync_run=sync_run,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        changed_at=changed_at,
        audit_action=AuditRecord.Action.UPDATE,
        path_error_type=InvalidEarningsStatusReinstatement,
    )


def _apply_status_mutation(
    *,
    earnings_event: EarningsEvent,
    target_status: str,
    allowed_transitions: frozenset[tuple[str, str]],
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    request_id: str,
    ip_address: str | None,
    changed_at: datetime | None,
    audit_action: str,
    path_error_type: type[EarningsStatusServiceError] = InvalidEarningsStatusTransition,
    require_affirmative_cancellation: bool = False,
    affirmative_cancellation: bool = False,
) -> EarningsStatusWriteResult:
    event_id = _require_event_id(earnings_event)
    timestamp = _normalize_timestamp(changed_at)
    if not _is_persisted_identity(earnings_event):
        raise EarningsStatusIdentityUncertain(
            "Status lifecycle only accepts a persisted EarningsEvent identity."
        )

    with transaction.atomic():
        try:
            current = EarningsEvent.objects.select_for_update().get(pk=event_id)
        except EarningsEvent.DoesNotExist as error:
            raise EarningsStatusIdentityUncertain(
                "EarningsEvent must exist before changing its status."
            ) from error

        previous_status = current.status
        if previous_status == target_status:
            return _no_op_result(current, target_status)

        if (
            require_affirmative_cancellation
            and actor_user is None
            and affirmative_cancellation is not True
        ):
            raise InvalidEarningsStatusContext(
                "Automatic cancellation requires explicit affirmative_cancellation=True."
            )

        if (previous_status, target_status) not in allowed_transitions:
            raise _path_error(
                current_status=previous_status,
                target_status=target_status,
                allowed_transitions=allowed_transitions,
                error_type=path_error_type,
            )

        context = _resolve_mutation_context(
            earnings_event=current,
            source_evidence=source_evidence,
            sync_run=sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )

        current.status = target_status
        current.save(update_fields=("status", "updated_at"))

        data_change_result = record_data_change(
            target_type=DataChange.TargetType.EARNINGS_EVENT,
            target_id=current.pk,
            field_name=STATUS_FIELD_NAME,
            old_value=previous_status,
            new_value=target_status,
            rule_version=EARNINGS_STATUS_LIFECYCLE_RULE_VERSION,
            source_evidence=context.source_evidence,
            sync_run=context.sync_run,
            actor_user=context.actor_user,
            reason=context.reason,
            origin_key=context.request_id if context.actor_user is not None else "",
            changed_at=timestamp,
        )
        if data_change_result.change is None or data_change_result.skipped:
            raise EarningsStatusIntegrityError(
                "A changed EarningsEvent status must produce a DataChange."
            )

        audit_record = _record_status_audit(
            context=context,
            earnings_event=current,
            previous_status=previous_status,
            target_status=target_status,
            action=audit_action,
        )
        return EarningsStatusWriteResult(
            earnings_event=current,
            previous_status=previous_status,
            status=target_status,
            changed=True,
            data_change=data_change_result,
            audit_record=audit_record,
        )


def _require_event_id(earnings_event: EarningsEvent) -> UUID:
    if earnings_event._state.adding or earnings_event.pk is None:
        raise EarningsStatusIdentityUncertain("EarningsEvent must be saved before use.")
    return earnings_event.pk


def _is_persisted_identity(earnings_event: EarningsEvent) -> bool:
    return bool(earnings_event.company_id)


def _normalize_target_status(target_status: str) -> str:
    if not isinstance(target_status, str):
        raise InvalidEarningsTargetStatus("target_status must be a string.")
    normalized = target_status.strip().lower()
    if normalized not in EventStatus.values:
        raise InvalidEarningsTargetStatus(
            "target_status must be scheduled_estimated, scheduled_confirmed, released or cancelled."
        )
    return normalized


def _no_op_result(earnings_event: EarningsEvent, target_status: str) -> EarningsStatusWriteResult:
    return EarningsStatusWriteResult(
        earnings_event=earnings_event,
        previous_status=target_status,
        status=target_status,
        changed=False,
        data_change=None,
        audit_record=None,
    )


def _path_error(
    *,
    current_status: str,
    target_status: str,
    allowed_transitions: frozenset[tuple[str, str]],
    error_type: type[EarningsStatusServiceError],
) -> EarningsStatusServiceError:
    transition = (current_status, target_status)
    if error_type is InvalidEarningsStatusTransition:
        if transition in _CORRECTION_TRANSITIONS:
            return InvalidEarningsStatusTransition(
                f"{current_status!r} -> {target_status!r} requires the correction path."
            )
        if transition in _REINSTATEMENT_TRANSITIONS:
            return InvalidEarningsStatusTransition(
                f"{current_status!r} -> {target_status!r} requires the reinstatement path."
            )
    if error_type is InvalidEarningsStatusCorrection:
        return InvalidEarningsStatusCorrection(
            f"{current_status!r} -> {target_status!r} is not a correction transition."
        )
    if error_type is InvalidEarningsStatusReinstatement:
        return InvalidEarningsStatusReinstatement(
            f"{current_status!r} -> {target_status!r} is not a reinstatement transition."
        )
    return error_type(f"Transition {current_status!r} -> {target_status!r} is not allowed.")


def _normalize_timestamp(value: datetime | None) -> datetime:
    timestamp = value or timezone.now()
    if timezone.is_naive(timestamp):
        raise InvalidEarningsStatusContext("changed_at must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _resolve_mutation_context(
    *,
    earnings_event: EarningsEvent,
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    request_id: str,
    ip_address: str | None,
) -> _MutationContext:
    if actor_user is None and source_evidence is None and sync_run is None:
        raise InvalidEarningsStatusContext(
            "A status mutation requires an actor or machine provenance (SourceEvidence or SyncRun)."
        )
    if actor_user is not None and not reason.strip():
        raise InvalidEarningsStatusContext("Manual status mutations require a reason.")
    if actor_user is not None and not request_id.strip():
        raise InvalidEarningsStatusContext("Manual status mutations require a stable request_id.")

    evidence_reference: SourceEvidenceReference | None = None
    if source_evidence is not None:
        try:
            evidence_reference = resolve_source_evidence_reference(
                source_evidence=source_evidence,
                sync_run=sync_run,
                target_type=DataChange.TargetType.EARNINGS_EVENT,
                target_id=earnings_event.pk,
                field_names=(STATUS_FIELD_NAME,),
            )
        except InvalidEvidenceReference as error:
            raise InvalidEarningsStatusEvidence(str(error)) from None

    resolved_sync_run = (
        evidence_reference.sync_run if evidence_reference is not None else _load_sync_run(sync_run)
    )
    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        if resolved_sync_run is None:
            raise InvalidEarningsStatusContext(
                "Automatic status mutations require a SyncRun or SourceEvidence."
            )
        normalized_request_id = f"sync-run:{resolved_sync_run.pk}"

    return _MutationContext(
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
        raise InvalidEarningsStatusContext("sync_run must be saved before use.")
    try:
        return SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvalidEarningsStatusContext("sync_run must exist before use.") from error


def _record_status_audit(
    *,
    context: _MutationContext,
    earnings_event: EarningsEvent,
    previous_status: str,
    target_status: str,
    action: str,
) -> AuditRecord:
    before = {STATUS_FIELD_NAME: previous_status}
    after = {STATUS_FIELD_NAME: target_status}
    if context.actor_user is not None:
        result = record_user_action(
            actor_user=context.actor_user,
            action=action,
            target_type=AuditRecord.TargetType.EARNINGS_EVENT,
            target_id=earnings_event.pk,
            before=before,
            after=after,
            reason=context.reason,
            request_id=context.request_id,
            ip_address=context.ip_address,
            sync_run=context.sync_run,
        )
    else:
        if context.sync_run is None:
            raise InvalidEarningsStatusContext("Automatic status mutations require a SyncRun.")
        result = record_system_action(
            sync_run=context.sync_run,
            action=action,
            target_type=AuditRecord.TargetType.EARNINGS_EVENT,
            target_id=earnings_event.pk,
            before=before,
            after=after,
            request_id=context.request_id,
        )
    return result.record
