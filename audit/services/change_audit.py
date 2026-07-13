from __future__ import annotations

import hashlib
import ipaddress
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from audit.models import AuditRecord, DataChange, RawDataObservation, SourceEvidence, SyncRun
from audit.security import (
    InvalidAuditValue,
    SensitiveAuditData,
    is_sensitive_field_name,
    normalize_json_without_credentials,
)

if TYPE_CHECKING:
    from accounts.models import User


class InvalidDataChange(ValueError):
    pass


class SensitiveDataChangeValue(InvalidDataChange):
    pass


class DataChangeIntegrityError(RuntimeError):
    pass


class InvalidAuditRecord(ValueError):
    pass


class SensitiveAuditRecordValue(InvalidAuditRecord):
    pass


class AuditRecordIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DataChangeWriteResult:
    change: DataChange | None
    created: bool
    skipped: bool


@dataclass(frozen=True, slots=True)
class AuditRecordWriteResult:
    record: AuditRecord
    created: bool


def record_data_change(
    *,
    target_type: str,
    target_id: uuid.UUID,
    field_name: str,
    old_value: object,
    new_value: object,
    rule_version: str,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    origin_key: str = "",
    changed_at: datetime | None = None,
) -> DataChangeWriteResult:
    normalized_target_type = _normalize_data_change_target_type(target_type)
    normalized_target_id = _normalize_target_id(target_id, value_name="DataChange target_id")
    normalized_field_name = _normalize_data_change_field_name(field_name)
    try:
        normalized_old, canonical_old = _canonicalize_secure_json(
            old_value,
            value_name="DataChange old_value",
        )
        normalized_new, canonical_new = _canonicalize_secure_json(
            new_value,
            value_name="DataChange new_value",
        )
        normalized_rule = _normalize_safe_text(
            rule_version,
            value_name="DataChange rule_version",
            maximum_length=100,
            required=True,
        )
        normalized_reason = _normalize_safe_text(
            reason,
            value_name="DataChange reason",
            maximum_length=2000,
            required=actor_user is not None,
        )
    except SensitiveAuditData as error:
        raise SensitiveDataChangeValue(str(error)) from None
    except InvalidAuditValue as error:
        raise InvalidDataChange(str(error)) from None

    if canonical_old == canonical_new:
        return DataChangeWriteResult(change=None, created=False, skipped=True)

    timestamp = _aware_data_change_timestamp(changed_at)
    _require_persisted_actor(actor_user, error_type=InvalidDataChange)
    _require_persisted_sync_run(sync_run, error_type=InvalidDataChange)
    _require_persisted_source_evidence(source_evidence, error_type=InvalidDataChange)
    if actor_user is None and source_evidence is None and sync_run is None:
        raise InvalidDataChange("Automatic DataChange records require a SyncRun or SourceEvidence.")

    if actor_user is not None:
        try:
            normalized_origin = _normalize_safe_text(
                origin_key,
                value_name="DataChange origin_key",
                maximum_length=255,
                required=True,
            )
        except SensitiveAuditData as error:
            raise SensitiveDataChangeValue(str(error)) from None
        except InvalidAuditValue as error:
            raise InvalidDataChange(str(error)) from None
    elif source_evidence is not None:
        normalized_origin = f"source_evidence:{source_evidence.pk}"
    else:
        if sync_run is None:
            raise InvalidDataChange(
                "Automatic DataChange records require a SyncRun or SourceEvidence."
            )
        normalized_origin = f"sync_run:{sync_run.pk}"

    with transaction.atomic():
        current_evidence = _load_source_evidence(source_evidence)
        current_sync_run = _load_sync_run(sync_run)
        _validate_change_evidence_reference(
            source_evidence=current_evidence,
            sync_run=current_sync_run,
            target_type=normalized_target_type,
            target_id=normalized_target_id,
            field_name=normalized_field_name,
        )
        change_key = _build_change_key(
            target_type=normalized_target_type,
            target_id=normalized_target_id,
            field_name=normalized_field_name,
            canonical_old_value=canonical_old,
            canonical_new_value=canonical_new,
            rule_version=normalized_rule,
            source_evidence=current_evidence,
            sync_run=current_sync_run,
            actor_user=actor_user,
            origin_key=normalized_origin,
        )

        try:
            with transaction.atomic():
                change = DataChange.objects.create(
                    target_type=normalized_target_type,
                    target_id=normalized_target_id,
                    field_name=normalized_field_name,
                    old_value=normalized_old,
                    new_value=normalized_new,
                    source_evidence=current_evidence,
                    sync_run=current_sync_run,
                    actor_user=actor_user,
                    reason=normalized_reason,
                    origin_key=normalized_origin,
                    rule_version=normalized_rule,
                    change_key=change_key,
                    changed_at=timestamp,
                )
                return DataChangeWriteResult(change=change, created=True, skipped=False)
        except IntegrityError as error:
            try:
                existing = DataChange.objects.get(change_key=change_key)
            except DataChange.DoesNotExist:
                raise error from None
            _verify_existing_data_change(
                change=existing,
                target_type=normalized_target_type,
                target_id=normalized_target_id,
                field_name=normalized_field_name,
                old_value=normalized_old,
                new_value=normalized_new,
                source_evidence=current_evidence,
                sync_run=current_sync_run,
                actor_user=actor_user,
                reason=normalized_reason,
                origin_key=normalized_origin,
                rule_version=normalized_rule,
            )
            return DataChangeWriteResult(change=existing, created=False, skipped=False)


def record_user_action(
    *,
    actor_user: User,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    before: object,
    after: object,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
    sync_run: SyncRun | None = None,
) -> AuditRecordWriteResult:
    _require_persisted_actor(actor_user, error_type=InvalidAuditRecord)
    return _record_audit_record(
        actor_user=actor_user,
        sync_run=sync_run,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
    )


def record_system_action(
    *,
    sync_run: SyncRun,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    before: object,
    after: object,
    request_id: str,
    reason: str = "",
) -> AuditRecordWriteResult:
    _require_persisted_sync_run(sync_run, error_type=InvalidAuditRecord)
    return _record_audit_record(
        actor_user=None,
        sync_run=sync_run,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        reason=reason,
        request_id=request_id,
        ip_address=None,
    )


def _record_audit_record(
    *,
    actor_user: User | None,
    sync_run: SyncRun | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    before: object,
    after: object,
    reason: str,
    request_id: str,
    ip_address: str | None,
) -> AuditRecordWriteResult:
    normalized_action = _normalize_audit_action(action)
    normalized_target_type = _normalize_audit_target_type(target_type)
    normalized_target_id = _normalize_target_id(target_id, value_name="AuditRecord target_id")
    try:
        normalized_before, canonical_before = _canonicalize_secure_json(
            before,
            value_name="AuditRecord before",
        )
        normalized_after, canonical_after = _canonicalize_secure_json(
            after,
            value_name="AuditRecord after",
        )
        normalized_reason = _normalize_safe_text(
            reason,
            value_name="AuditRecord reason",
            maximum_length=2000,
            required=actor_user is not None,
        )
        normalized_request_id = _normalize_safe_text(
            request_id,
            value_name="AuditRecord request_id",
            maximum_length=255,
            required=True,
        )
    except SensitiveAuditData as error:
        raise SensitiveAuditRecordValue(str(error)) from None
    except InvalidAuditValue as error:
        raise InvalidAuditRecord(str(error)) from None

    _require_persisted_actor(actor_user, error_type=InvalidAuditRecord)
    _require_persisted_sync_run(sync_run, error_type=InvalidAuditRecord)
    if actor_user is None and sync_run is None:
        raise InvalidAuditRecord("AuditRecord requires an actor_user or SyncRun.")
    ip_hash = _hash_ip_address(ip_address)

    with transaction.atomic():
        current_sync_run = _load_sync_run(sync_run)
        audit_key = _build_audit_key(
            actor_user=actor_user,
            sync_run=current_sync_run,
            action=normalized_action,
            target_type=normalized_target_type,
            target_id=normalized_target_id,
            canonical_before=canonical_before,
            canonical_after=canonical_after,
            reason=normalized_reason,
            request_id=normalized_request_id,
        )
        try:
            with transaction.atomic():
                record = AuditRecord.objects.create(
                    actor_user=actor_user,
                    sync_run=current_sync_run,
                    action=normalized_action,
                    target_type=normalized_target_type,
                    target_id=normalized_target_id,
                    before=normalized_before,
                    after=normalized_after,
                    reason=normalized_reason,
                    request_id=normalized_request_id,
                    ip_hash=ip_hash,
                    audit_key=audit_key,
                )
                return AuditRecordWriteResult(record=record, created=True)
        except IntegrityError as error:
            try:
                existing = AuditRecord.objects.get(audit_key=audit_key)
            except AuditRecord.DoesNotExist:
                raise error from None
            _verify_existing_audit_record(
                record=existing,
                actor_user=actor_user,
                sync_run=current_sync_run,
                action=normalized_action,
                target_type=normalized_target_type,
                target_id=normalized_target_id,
                before=normalized_before,
                after=normalized_after,
                reason=normalized_reason,
                request_id=normalized_request_id,
            )
            return AuditRecordWriteResult(record=existing, created=False)


def _normalize_data_change_target_type(target_type: str) -> str:
    normalized = target_type.strip()
    allowed = {value for value, _ in DataChange.TargetType.choices}
    if normalized not in allowed:
        raise InvalidDataChange("target_type must be a supported DataChange target.")
    return normalized


def _normalize_audit_target_type(target_type: str) -> str:
    normalized = target_type.strip()
    allowed = {value for value, _ in AuditRecord.TargetType.choices}
    if normalized not in allowed:
        raise InvalidAuditRecord("target_type must be a supported AuditRecord target.")
    return normalized


def _normalize_audit_action(action: str) -> str:
    normalized = action.strip()
    allowed = {value for value, _ in AuditRecord.Action.choices}
    if normalized not in allowed:
        raise InvalidAuditRecord("action must be a supported AuditRecord action.")
    return normalized


def _normalize_target_id(target_id: uuid.UUID, *, value_name: str) -> uuid.UUID:
    if not isinstance(target_id, uuid.UUID):
        if value_name.startswith("DataChange"):
            raise InvalidDataChange(f"{value_name} must be a UUID.")
        raise InvalidAuditRecord(f"{value_name} must be a UUID.")
    return target_id


def _normalize_data_change_field_name(field_name: str) -> str:
    normalized = field_name.strip()
    if not normalized or len(normalized) > 100:
        raise InvalidDataChange("field_name must contain 1 to 100 characters.")
    if is_sensitive_field_name(normalized):
        raise SensitiveDataChangeValue("field_name must not identify a credential field.")
    return normalized


def _canonicalize_secure_json(value: object, *, value_name: str) -> tuple[object, str]:
    normalized = normalize_json_without_credentials(value, value_name=value_name)
    try:
        canonical = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise InvalidAuditValue(f"{value_name} must be valid JSON data.") from error
    return normalized, canonical


def _normalize_safe_text(
    value: str,
    *,
    value_name: str,
    maximum_length: int,
    required: bool,
) -> str:
    if not isinstance(value, str):
        raise InvalidAuditValue(f"{value_name} must be text.")
    normalized = value.strip()
    if required and not normalized:
        raise InvalidAuditValue(f"{value_name} must not be empty.")
    if len(normalized) > maximum_length:
        raise InvalidAuditValue(f"{value_name} is too long.")
    safe_value = normalize_json_without_credentials(normalized, value_name=value_name)
    if not isinstance(safe_value, str):
        raise InvalidAuditValue(f"{value_name} must be text.")
    return safe_value


def _aware_data_change_timestamp(value: datetime | None) -> datetime:
    result = value or timezone.now()
    if timezone.is_naive(result):
        raise InvalidDataChange("DataChange changed_at must be timezone-aware.")
    return result


def _require_persisted_actor(
    actor_user: User | None,
    *,
    error_type: type[InvalidDataChange] | type[InvalidAuditRecord],
) -> None:
    if actor_user is not None and actor_user._state.adding:
        raise error_type("actor_user must be saved before recording audit history.")


def _require_persisted_sync_run(
    sync_run: SyncRun | None,
    *,
    error_type: type[InvalidDataChange] | type[InvalidAuditRecord],
) -> None:
    if sync_run is not None and sync_run._state.adding:
        raise error_type("sync_run must be saved before recording audit history.")


def _require_persisted_source_evidence(
    source_evidence: SourceEvidence | None,
    *,
    error_type: type[InvalidDataChange] | type[InvalidAuditRecord],
) -> None:
    if source_evidence is not None and source_evidence._state.adding:
        raise error_type("source_evidence must be saved before recording audit history.")


def _load_sync_run(sync_run: SyncRun | None) -> SyncRun | None:
    if sync_run is None:
        return None
    return SyncRun.objects.select_related("source").get(pk=sync_run.pk)


def _load_source_evidence(source_evidence: SourceEvidence | None) -> SourceEvidence | None:
    if source_evidence is None:
        return None
    return SourceEvidence.objects.select_related("raw_data_record__source").get(
        pk=source_evidence.pk
    )


def _validate_change_evidence_reference(
    *,
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    target_type: str,
    target_id: uuid.UUID,
    field_name: str,
) -> None:
    if source_evidence is None:
        return
    if (
        source_evidence.target_type != target_type
        or source_evidence.target_id != target_id
        or source_evidence.field_name not in ("", field_name)
    ):
        raise InvalidDataChange(
            "SourceEvidence must describe the same target and field as DataChange."
        )
    if sync_run is None:
        return
    if source_evidence.raw_data_record.source_id != sync_run.source_id:
        raise InvalidDataChange("SyncRun and SourceEvidence must trace to the same DataSource.")
    if not RawDataObservation.objects.filter(
        sync_run=sync_run,
        raw_data_record=source_evidence.raw_data_record,
    ).exists():
        raise InvalidDataChange("SyncRun must have observed the SourceEvidence raw data record.")


def _build_change_key(
    *,
    target_type: str,
    target_id: uuid.UUID,
    field_name: str,
    canonical_old_value: str,
    canonical_new_value: str,
    rule_version: str,
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    origin_key: str,
) -> str:
    if actor_user is not None:
        source_identity = {
            "actor_user_id": str(actor_user.pk),
            "kind": "manual",
            "origin_key": origin_key,
        }
    elif source_evidence is not None:
        source_identity = {
            "kind": "source_evidence",
            "source_evidence_id": str(source_evidence.pk),
        }
    else:
        if sync_run is None:
            raise InvalidDataChange(
                "Automatic DataChange records require a SyncRun or SourceEvidence."
            )
        source_identity = {
            "kind": "sync_run",
            "sync_run_id": str(sync_run.pk),
        }
    identity = {
        "field_name": field_name,
        "new_value": canonical_new_value,
        "old_value": canonical_old_value,
        "rule_version": rule_version,
        "source": source_identity,
        "target_id": str(target_id),
        "target_type": target_type,
    }
    serialized = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(serialized).hexdigest()


def _build_audit_key(
    *,
    actor_user: User | None,
    sync_run: SyncRun | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    canonical_before: str,
    canonical_after: str,
    reason: str,
    request_id: str,
) -> str:
    identity = {
        "action": action,
        "actor_user_id": str(actor_user.pk) if actor_user is not None else None,
        "after": canonical_after,
        "before": canonical_before,
        "reason": reason,
        "request_id": request_id,
        "sync_run_id": str(sync_run.pk) if sync_run is not None else None,
        "target_id": str(target_id),
        "target_type": target_type,
    }
    serialized = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(serialized).hexdigest()


def _hash_ip_address(ip_address: str | None) -> str:
    if ip_address is None or not ip_address.strip():
        return ""
    try:
        normalized = ipaddress.ip_address(ip_address.strip()).compressed
    except ValueError:
        raise InvalidAuditRecord("ip_address must be a valid IPv4 or IPv6 address.") from None
    digest = salted_hmac(
        "earnings-radar.audit.ip-hash.v1",
        normalized,
        algorithm="sha256",
    ).hexdigest()
    return f"v1:{digest}"


def _verify_existing_data_change(
    *,
    change: DataChange,
    target_type: str,
    target_id: uuid.UUID,
    field_name: str,
    old_value: object,
    new_value: object,
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    origin_key: str,
    rule_version: str,
) -> None:
    sync_run_must_match = actor_user is not None or source_evidence is None
    if (
        change.target_type != target_type
        or change.target_id != target_id
        or change.field_name != field_name
        or change.old_value != old_value
        or change.new_value != new_value
        or change.source_evidence_id
        != (source_evidence.pk if source_evidence is not None else None)
        or (
            sync_run_must_match
            and change.sync_run_id != (sync_run.pk if sync_run is not None else None)
        )
        or change.actor_user_id != (actor_user.pk if actor_user is not None else None)
        or change.reason != reason
        or change.origin_key != origin_key
        or change.rule_version != rule_version
    ):
        raise DataChangeIntegrityError(
            "An existing DataChange row has the same key but different immutable data."
        )


def _verify_existing_audit_record(
    *,
    record: AuditRecord,
    actor_user: User | None,
    sync_run: SyncRun | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    before: object,
    after: object,
    reason: str,
    request_id: str,
) -> None:
    if (
        record.actor_user_id != (actor_user.pk if actor_user is not None else None)
        or record.sync_run_id != (sync_run.pk if sync_run is not None else None)
        or record.action != action
        or record.target_type != target_type
        or record.target_id != target_id
        or record.before != before
        or record.after != after
        or record.reason != reason
        or record.request_id != request_id
    ):
        raise AuditRecordIntegrityError(
            "An existing AuditRecord row has the same key but different immutable data."
        )
