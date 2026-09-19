from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID
from zoneinfo import ZoneInfo

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
from earnings.models import (
    EarningsDateChange,
    EarningsDateChangeKind,
    EarningsDateField,
    EarningsDateHistoryPrecision,
    EarningsDatePrecision,
    EarningsEvent,
    ReleaseSession,
)

if TYPE_CHECKING:
    from accounts.models import User


EARNINGS_DATE_CHANGE_RULE_VERSION = "earnings-date-change-v1"
MARKET_TIMEZONE = ZoneInfo("America/New_York")

_DATE_FIELD_COLUMNS: dict[str, tuple[str, str, str]] = {
    EarningsDateField.ESTIMATED_RELEASE: (
        "estimated_release_at",
        "estimated_release_date",
        "estimated_release_precision",
    ),
    EarningsDateField.CONFIRMED_RELEASE: (
        "confirmed_release_at",
        "confirmed_release_date",
        "confirmed_release_precision",
    ),
    EarningsDateField.EARNINGS_RELEASE: (
        "earnings_release_at",
        "earnings_release_date",
        "earnings_release_precision",
    ),
    EarningsDateField.CONFERENCE_CALL: (
        "conference_call_at",
        "conference_call_date",
        "conference_call_precision",
    ),
}
_FIELD_ORDER: tuple[str, ...] = (
    EarningsDateField.ESTIMATED_RELEASE,
    EarningsDateField.CONFIRMED_RELEASE,
    EarningsDateField.EARNINGS_RELEASE,
    EarningsDateField.CONFERENCE_CALL,
    EarningsDateField.RELEASE_SESSION,
)
_ALLOWED_FIELDS: frozenset[str] = frozenset(_FIELD_ORDER)
_PRECISION_RANK: dict[str, int] = {
    EarningsDatePrecision.UNKNOWN: 0,
    EarningsDatePrecision.DATE_ONLY: 1,
    EarningsDatePrecision.EXACT_DATETIME: 2,
}


class EarningsDateChangeServiceError(ValueError):
    pass


class InvalidEarningsDateValue(EarningsDateChangeServiceError):
    pass


class EarningsDateChangeIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _NormalizedValue:
    precision: str
    date_value: date | None
    datetime_value: datetime | None
    session_value: str | None
    market_date: date | None
    canonical_value: dict[str, str] | None


@dataclass(frozen=True, slots=True)
class _WriteContext:
    source_evidence: SourceEvidence | None
    sync_run: SyncRun | None
    actor_user: User | None
    reason: str
    request_id: str
    ip_address: str | None


@dataclass(frozen=True, slots=True)
class EarningsScheduleWriteResult:
    earnings_event: EarningsEvent
    changed: bool
    data_changes: tuple[DataChangeWriteResult, ...]
    date_changes: tuple[EarningsDateChange, ...]
    audit_record: AuditRecord | None


def update_earnings_schedule(
    *,
    earnings_event: EarningsEvent,
    changes: Mapping[str, object],
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
    changed_at: datetime | None = None,
) -> EarningsScheduleWriteResult:
    """Apply an audited mutation to the controlled EarningsEvent schedule fields.

    Date fields accept ``None``, ``date``, aware ``datetime`` or a mapping with
    exactly ``precision`` and ``value`` keys. The mapping form makes invalid
    precision/value combinations explicit and rejects them.
    """

    normalized_changes = _normalize_changes(changes)
    timestamp = _normalize_timestamp(changed_at)
    event_id = _require_event_id(earnings_event)

    with transaction.atomic():
        try:
            current = EarningsEvent.objects.select_for_update().get(pk=event_id)
        except EarningsEvent.DoesNotExist as error:
            raise EarningsDateChangeServiceError(
                "EarningsEvent must exist before updating its schedule."
            ) from error

        changed_values: list[tuple[str, _NormalizedValue, _NormalizedValue, str]] = []
        for field_name in _FIELD_ORDER:
            new_value = normalized_changes.get(field_name)
            if new_value is None:
                continue
            old_value = _normalize_current_value(current, field_name)
            if old_value.canonical_value == new_value.canonical_value:
                continue
            change_kind = _determine_change_kind(
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
            )
            changed_values.append((field_name, old_value, new_value, change_kind))

        if not changed_values:
            return EarningsScheduleWriteResult(
                earnings_event=current,
                changed=False,
                data_changes=(),
                date_changes=(),
                audit_record=None,
            )

        context = _resolve_write_context(
            earnings_event=current,
            field_names=tuple(item[0] for item in changed_values),
            source_evidence=source_evidence,
            sync_run=sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )

        _apply_current_state(current=current, changed_values=changed_values)
        data_changes: list[DataChangeWriteResult] = []
        date_changes: list[EarningsDateChange] = []

        for changed_field_name, old_value, new_value, change_kind in changed_values:
            data_change_result = record_data_change(
                target_type=DataChange.TargetType.EARNINGS_EVENT,
                target_id=current.pk,
                field_name=changed_field_name,
                old_value=old_value.canonical_value,
                new_value=new_value.canonical_value,
                rule_version=EARNINGS_DATE_CHANGE_RULE_VERSION,
                source_evidence=_evidence_for_field(context, changed_field_name),
                sync_run=context.sync_run,
                actor_user=context.actor_user,
                reason=context.reason,
                origin_key=context.request_id if context.actor_user is not None else "",
                changed_at=timestamp,
            )
            if data_change_result.change is None or data_change_result.skipped:
                raise EarningsDateChangeIntegrityError(
                    "A changed schedule value must produce a DataChange."
                )
            data_changes.append(data_change_result)
            date_changes.append(
                _persist_date_change(
                    earnings_event=current,
                    field_name=changed_field_name,
                    change_kind=change_kind,
                    old_value=old_value,
                    new_value=new_value,
                    data_change_result=data_change_result,
                    detected_at=timestamp,
                )
            )

        audit_record = _record_operation_audit(
            context=context,
            earnings_event=current,
            changed_values=tuple(changed_values),
        )
        return EarningsScheduleWriteResult(
            earnings_event=current,
            changed=True,
            data_changes=tuple(data_changes),
            date_changes=tuple(date_changes),
            audit_record=audit_record,
        )


def _require_event_id(earnings_event: EarningsEvent) -> UUID:
    if earnings_event._state.adding or earnings_event.pk is None:
        raise EarningsDateChangeServiceError("EarningsEvent must be saved before use.")
    return earnings_event.pk


def _normalize_timestamp(value: datetime | None) -> datetime:
    timestamp = value or timezone.now()
    if timezone.is_naive(timestamp):
        raise InvalidEarningsDateValue("changed_at must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _normalize_changes(changes: Mapping[str, object]) -> dict[str, _NormalizedValue]:
    if not isinstance(changes, Mapping):
        raise InvalidEarningsDateValue("changes must be a mapping.")
    if not changes:
        raise InvalidEarningsDateValue("changes must contain at least one controlled field.")

    if any(not isinstance(field_name, str) for field_name in changes):
        raise InvalidEarningsDateValue("changes keys must be field-name strings.")
    unknown_fields = set(changes) - _ALLOWED_FIELDS
    if unknown_fields:
        raise InvalidEarningsDateValue(
            f"Unsupported EarningsEvent schedule fields: {sorted(unknown_fields)!r}."
        )

    normalized: dict[str, _NormalizedValue] = {}
    for field_name in _FIELD_ORDER:
        if field_name not in changes:
            continue
        raw_value = changes[field_name]
        if field_name == EarningsDateField.RELEASE_SESSION:
            normalized[field_name] = _normalize_session_input(raw_value)
        else:
            normalized[field_name] = _normalize_date_input(raw_value)
    return normalized


def _normalize_date_input(raw_value: object) -> _NormalizedValue:
    if raw_value is None:
        return _unknown_date_value()
    if isinstance(raw_value, datetime):
        return _exact_datetime_value(raw_value)
    if isinstance(raw_value, date):
        return _date_only_value(raw_value)
    if not isinstance(raw_value, Mapping):
        raise InvalidEarningsDateValue(
            "Date field values must be None, date, aware datetime, or a value mapping."
        )

    if set(raw_value) != {"precision", "value"}:
        raise InvalidEarningsDateValue(
            "Date field mappings must contain exactly 'precision' and 'value'."
        )
    precision = raw_value["precision"]
    value = raw_value["value"]
    if not isinstance(precision, str):
        raise InvalidEarningsDateValue("precision must be a string.")
    normalized_precision = precision.strip().lower()
    if normalized_precision == EarningsDatePrecision.UNKNOWN:
        if value is not None:
            raise InvalidEarningsDateValue("unknown precision requires a null value.")
        return _unknown_date_value()
    if normalized_precision == EarningsDatePrecision.DATE_ONLY:
        if isinstance(value, datetime) or not isinstance(value, date):
            raise InvalidEarningsDateValue(
                "date_only precision requires a date value, not a datetime."
            )
        return _date_only_value(value)
    if normalized_precision == EarningsDatePrecision.EXACT_DATETIME:
        if not isinstance(value, datetime):
            raise InvalidEarningsDateValue(
                "exact_datetime precision requires an aware datetime value."
            )
        return _exact_datetime_value(value)
    raise InvalidEarningsDateValue(f"Unsupported date precision {precision!r}.")


def _normalize_session_input(raw_value: object) -> _NormalizedValue:
    if not isinstance(raw_value, str):
        raise InvalidEarningsDateValue(
            "release_session must be pre_market, after_market, during_market or unknown."
        )
    normalized = raw_value.strip().lower()
    if normalized not in ReleaseSession.values:
        raise InvalidEarningsDateValue(
            "release_session must be pre_market, after_market, during_market or unknown."
        )
    return _NormalizedValue(
        precision=EarningsDateHistoryPrecision.SESSION_ONLY,
        date_value=None,
        datetime_value=None,
        session_value=normalized,
        market_date=None,
        canonical_value={
            "kind": "session",
            "precision": EarningsDateHistoryPrecision.SESSION_ONLY,
            "value": normalized,
        },
    )


def _unknown_date_value() -> _NormalizedValue:
    return _NormalizedValue(
        precision=EarningsDatePrecision.UNKNOWN,
        date_value=None,
        datetime_value=None,
        session_value=None,
        market_date=None,
        canonical_value=None,
    )


def _date_only_value(value: date) -> _NormalizedValue:
    return _NormalizedValue(
        precision=EarningsDatePrecision.DATE_ONLY,
        date_value=value,
        datetime_value=None,
        session_value=None,
        market_date=value,
        canonical_value={
            "kind": "date",
            "precision": EarningsDatePrecision.DATE_ONLY,
            "value": value.isoformat(),
        },
    )


def _exact_datetime_value(value: datetime) -> _NormalizedValue:
    if timezone.is_naive(value):
        raise InvalidEarningsDateValue("exact_datetime values must be timezone-aware.")
    utc_value = value.astimezone(UTC)
    return _NormalizedValue(
        precision=EarningsDatePrecision.EXACT_DATETIME,
        date_value=None,
        datetime_value=utc_value,
        session_value=None,
        market_date=utc_value.astimezone(MARKET_TIMEZONE).date(),
        canonical_value={
            "kind": "datetime",
            "precision": EarningsDatePrecision.EXACT_DATETIME,
            "value": _format_datetime(utc_value),
        },
    )


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _normalize_current_value(
    earnings_event: EarningsEvent,
    field_name: str,
) -> _NormalizedValue:
    if field_name == EarningsDateField.RELEASE_SESSION:
        if not earnings_event.release_session:
            raise EarningsDateChangeIntegrityError(
                "EarningsEvent.release_session must not be null."
            )
        return _normalize_session_input(earnings_event.release_session)

    columns = _DATE_FIELD_COLUMNS[field_name]
    at_value = cast(datetime | None, getattr(earnings_event, columns[0]))
    date_value = cast(date | None, getattr(earnings_event, columns[1]))
    precision = cast(str, getattr(earnings_event, columns[2]))

    if precision == EarningsDatePrecision.UNKNOWN:
        if at_value is not None or date_value is not None:
            raise EarningsDateChangeIntegrityError(
                f"{field_name} has values inconsistent with unknown precision."
            )
        return _unknown_date_value()
    if precision == EarningsDatePrecision.DATE_ONLY:
        if at_value is not None or date_value is None:
            raise EarningsDateChangeIntegrityError(
                f"{field_name} has values inconsistent with date_only precision."
            )
        return _date_only_value(date_value)
    if precision == EarningsDatePrecision.EXACT_DATETIME:
        if at_value is None or date_value is not None:
            raise EarningsDateChangeIntegrityError(
                f"{field_name} has values inconsistent with exact_datetime precision."
            )
        return _exact_datetime_value(at_value)
    raise EarningsDateChangeIntegrityError(f"{field_name} has unsupported precision {precision!r}.")


def _determine_change_kind(
    *,
    field_name: str,
    old_value: _NormalizedValue,
    new_value: _NormalizedValue,
) -> str:
    if field_name == EarningsDateField.RELEASE_SESSION:
        if (
            old_value.session_value == ReleaseSession.UNKNOWN
            and new_value.session_value != ReleaseSession.UNKNOWN
        ):
            return EarningsDateChangeKind.PRECISION_REFINEMENT
        if (
            old_value.session_value != ReleaseSession.UNKNOWN
            and new_value.session_value == ReleaseSession.UNKNOWN
        ):
            return EarningsDateChangeKind.PRECISION_REGRESSION
        return EarningsDateChangeKind.VALUE_CHANGE

    old_precision = old_value.precision
    new_precision = new_value.precision
    if old_precision == EarningsDatePrecision.UNKNOWN:
        return EarningsDateChangeKind.PRECISION_REFINEMENT
    if new_precision == EarningsDatePrecision.UNKNOWN:
        return EarningsDateChangeKind.PRECISION_REGRESSION
    if old_value.market_date != new_value.market_date:
        return EarningsDateChangeKind.VALUE_CHANGE
    if old_precision != new_precision:
        if _PRECISION_RANK[new_precision] > _PRECISION_RANK[old_precision]:
            return EarningsDateChangeKind.PRECISION_REFINEMENT
        return EarningsDateChangeKind.PRECISION_REGRESSION
    if old_value.datetime_value != new_value.datetime_value:
        return EarningsDateChangeKind.VALUE_CHANGE
    return EarningsDateChangeKind.VALUE_CHANGE


def _resolve_write_context(
    *,
    earnings_event: EarningsEvent,
    field_names: tuple[str, ...],
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    request_id: str,
    ip_address: str | None,
) -> _WriteContext:
    if actor_user is None and source_evidence is None and sync_run is None:
        raise EarningsDateChangeServiceError(
            "Automatic writes require a SyncRun or SourceEvidence."
        )
    if actor_user is not None and not reason.strip():
        raise EarningsDateChangeServiceError("Manual writes require a non-empty reason.")
    if actor_user is not None and not request_id.strip():
        raise EarningsDateChangeServiceError("Manual writes require a request_id.")

    evidence_reference: SourceEvidenceReference | None = None
    if source_evidence is not None:
        try:
            evidence_reference = resolve_source_evidence_reference(
                source_evidence=source_evidence,
                sync_run=sync_run,
                target_type=DataChange.TargetType.EARNINGS_EVENT,
                target_id=earnings_event.pk,
                field_names=field_names,
            )
        except InvalidEvidenceReference as error:
            raise EarningsDateChangeServiceError(str(error)) from None

    resolved_sync_run = (
        evidence_reference.sync_run if evidence_reference is not None else _load_sync_run(sync_run)
    )
    normalized_request_id = request_id.strip()
    if not normalized_request_id:
        if resolved_sync_run is None:
            raise EarningsDateChangeServiceError("Automatic audit records require a SyncRun.")
        normalized_request_id = f"sync-run:{resolved_sync_run.pk}"

    return _WriteContext(
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
        raise EarningsDateChangeServiceError("sync_run must be saved before use.")
    try:
        return SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise EarningsDateChangeServiceError("sync_run must exist before use.") from error


def _evidence_for_field(
    context: _WriteContext,
    field_name: str,
) -> SourceEvidence | None:
    evidence = context.source_evidence
    if evidence is None:
        return None
    if evidence.field_name not in ("", field_name):
        return None
    return evidence


def _apply_current_state(
    *,
    current: EarningsEvent,
    changed_values: list[tuple[str, _NormalizedValue, _NormalizedValue, str]],
) -> None:
    update_fields: list[str] = []
    for field_name, _old_value, new_value, _change_kind in changed_values:
        if field_name == EarningsDateField.RELEASE_SESSION:
            current.release_session = cast(str, new_value.session_value)
            update_fields.append("release_session")
            continue

        at_field, date_field, precision_field = _DATE_FIELD_COLUMNS[field_name]
        setattr(current, at_field, new_value.datetime_value)
        setattr(current, date_field, new_value.date_value)
        setattr(current, precision_field, new_value.precision)
        update_fields.extend((at_field, date_field, precision_field))

    if update_fields:
        current.save(update_fields=(*update_fields, "updated_at"))


def _persist_date_change(
    *,
    earnings_event: EarningsEvent,
    field_name: str,
    change_kind: str,
    old_value: _NormalizedValue,
    new_value: _NormalizedValue,
    data_change_result: DataChangeWriteResult,
    detected_at: datetime,
) -> EarningsDateChange:
    if data_change_result.change is None:
        raise EarningsDateChangeIntegrityError("EarningsDateChange requires a DataChange.")

    existing = cast(
        EarningsDateChange | None,
        EarningsDateChange.objects.select_related("data_change")
        .filter(data_change=data_change_result.change)
        .first(),
    )
    if existing is not None:
        _verify_existing_date_change(
            existing=existing,
            earnings_event=earnings_event,
            field_name=field_name,
            change_kind=change_kind,
            old_value=old_value,
            new_value=new_value,
        )
        return existing

    if not data_change_result.created:
        raise EarningsDateChangeIntegrityError(
            "DataChange exists without its EarningsDateChange; manual review is required."
        )

    return EarningsDateChange.objects.create(
        earnings_event=earnings_event,
        field_name=field_name,
        change_kind=change_kind,
        old_precision=old_value.precision,
        new_precision=new_value.precision,
        old_date=old_value.date_value,
        new_date=new_value.date_value,
        old_datetime=old_value.datetime_value,
        new_datetime=new_value.datetime_value,
        old_session=old_value.session_value,
        new_session=new_value.session_value,
        data_change=data_change_result.change,
        detected_at=detected_at,
    )


def _verify_existing_date_change(
    *,
    existing: EarningsDateChange,
    earnings_event: EarningsEvent,
    field_name: str,
    change_kind: str,
    old_value: _NormalizedValue,
    new_value: _NormalizedValue,
) -> None:
    if (
        existing.earnings_event_id != earnings_event.pk
        or existing.field_name != field_name
        or existing.change_kind != change_kind
        or existing.old_precision != old_value.precision
        or existing.new_precision != new_value.precision
        or existing.old_date != old_value.date_value
        or existing.new_date != new_value.date_value
        or existing.old_datetime != old_value.datetime_value
        or existing.new_datetime != new_value.datetime_value
        or existing.old_session != old_value.session_value
        or existing.new_session != new_value.session_value
    ):
        raise EarningsDateChangeIntegrityError(
            "Existing EarningsDateChange differs from the replayed mutation."
        )


def _record_operation_audit(
    *,
    context: _WriteContext,
    earnings_event: EarningsEvent,
    changed_values: tuple[tuple[str, _NormalizedValue, _NormalizedValue, str], ...],
) -> AuditRecord:
    before = {
        field_name: old_value.canonical_value
        for field_name, old_value, _new_value, _change_kind in changed_values
    }
    after = {
        field_name: new_value.canonical_value
        for field_name, _old_value, new_value, _change_kind in changed_values
    }
    if context.actor_user is not None:
        result = record_user_action(
            actor_user=context.actor_user,
            action=AuditRecord.Action.MANUAL_CORRECTION,
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
            raise EarningsDateChangeServiceError("Automatic audit records require a SyncRun.")
        result = record_system_action(
            sync_run=context.sync_run,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.EARNINGS_EVENT,
            target_id=earnings_event.pk,
            before=before,
            after=after,
            request_id=context.request_id,
        )
    return result.record
