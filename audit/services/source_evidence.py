import hashlib
import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import RawDataObservation, RawDataRecord, SourceEvidence, SyncRun
from audit.security import (
    InvalidAuditValue,
    SensitiveAuditData,
    is_sensitive_field_name,
    normalize_json_without_credentials,
)

_CONFIDENCE_QUANTUM = Decimal("0.0001")


class InvalidSourceEvidence(ValueError):
    pass


class InvalidEvidenceReference(InvalidSourceEvidence):
    pass


class SensitiveEvidenceValue(InvalidSourceEvidence):
    pass


class SourceEvidenceIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourceEvidenceWriteResult:
    evidence: SourceEvidence
    created: bool


@dataclass(frozen=True, slots=True)
class SourceEvidenceReference:
    """A persisted evidence row together with its verified observation run."""

    evidence: SourceEvidence
    sync_run: SyncRun


def resolve_source_evidence_reference(
    *,
    source_evidence: SourceEvidence,
    sync_run: SyncRun | None,
    target_type: str,
    target_id: uuid.UUID,
    field_names: Collection[str] = (),
) -> SourceEvidenceReference:
    """Reload and validate evidence before a domain service relies on it.

    Domain services must use this instead of trusting a caller-owned model
    instance.  The database row is the source of truth for its target, raw
    record and observation provenance.
    """

    if source_evidence._state.adding or source_evidence.pk is None:
        raise InvalidEvidenceReference("SourceEvidence must be saved before use.")
    if sync_run is not None and (sync_run._state.adding or sync_run.pk is None):
        raise InvalidEvidenceReference("SyncRun must be saved before use.")

    try:
        evidence = SourceEvidence.objects.select_related(
            "raw_data_record__source",
            "sync_run__source",
        ).get(pk=source_evidence.pk)
    except SourceEvidence.DoesNotExist as error:
        raise InvalidEvidenceReference("SourceEvidence must exist before use.") from error

    if evidence.target_type != target_type or evidence.target_id != target_id:
        raise InvalidEvidenceReference("SourceEvidence must reference the same domain target.")
    if evidence.field_name and field_names and evidence.field_name not in field_names:
        raise InvalidEvidenceReference("SourceEvidence must describe a changed domain field.")

    if sync_run is None:
        current_sync_run = evidence.sync_run
    else:
        try:
            current_sync_run = SyncRun.objects.select_related("source").get(pk=sync_run.pk)
        except SyncRun.DoesNotExist as error:
            raise InvalidEvidenceReference("SyncRun must exist before use.") from error
        if evidence.sync_run_id != current_sync_run.pk:
            raise InvalidEvidenceReference(
                "SourceEvidence and SyncRun must refer to the same persisted run."
            )

    if evidence.raw_data_record.source_id != current_sync_run.source_id:
        raise InvalidEvidenceReference(
            "SourceEvidence RawDataRecord and SyncRun must trace to the same DataSource."
        )
    if not RawDataObservation.objects.filter(
        sync_run=current_sync_run,
        raw_data_record=evidence.raw_data_record,
    ).exists():
        raise InvalidEvidenceReference(
            "SyncRun must have observed the SourceEvidence RawDataRecord."
        )
    return SourceEvidenceReference(evidence=evidence, sync_run=current_sync_run)


def record_source_evidence(
    *,
    raw_data_record: RawDataRecord,
    sync_run: SyncRun,
    target_type: str,
    target_id: uuid.UUID,
    field_name: str,
    raw_value: object,
    normalized_value: object,
    confidence: Decimal | int | float | str,
    normalizer_version: str,
) -> SourceEvidenceWriteResult:
    normalized_target_type = _normalize_target_type(target_type)
    normalized_target_id = _normalize_target_id(target_id)
    normalized_field_name = _normalize_field_name(field_name)
    normalized_raw_value, _ = _canonicalize_json(raw_value, value_name="raw_value")
    normalized_domain_value, canonical_domain_value = _canonicalize_json(
        normalized_value,
        value_name="normalized_value",
    )
    normalized_confidence = _normalize_confidence(confidence)
    normalized_version = _normalize_version(normalizer_version)

    with transaction.atomic():
        current_record = RawDataRecord.objects.select_related("source").get(pk=raw_data_record.pk)
        observation = (
            RawDataObservation.objects.select_related("sync_run")
            .filter(
                raw_data_record=current_record,
                sync_run_id=sync_run.pk,
            )
            .first()
        )
        if observation is None:
            raise InvalidEvidenceReference(
                "SourceEvidence requires a RawDataObservation for the SyncRun and raw record."
            )
        if observation.sync_run.source_id != current_record.source_id:
            raise InvalidEvidenceReference(
                "The SyncRun source must match the RawDataRecord source."
            )
        if timezone.is_naive(observation.observed_at):
            raise InvalidEvidenceReference(
                "The RawDataObservation timestamp must be timezone-aware."
            )

        evidence_key = _build_evidence_key(
            raw_data_record_id=current_record.pk,
            target_type=normalized_target_type,
            target_id=normalized_target_id,
            field_name=normalized_field_name,
            canonical_normalized_value=canonical_domain_value,
            normalizer_version=normalized_version,
        )
        try:
            with transaction.atomic():
                evidence = SourceEvidence.objects.create(
                    raw_data_record=current_record,
                    sync_run=observation.sync_run,
                    target_type=normalized_target_type,
                    target_id=normalized_target_id,
                    field_name=normalized_field_name,
                    raw_value=normalized_raw_value,
                    normalized_value=normalized_domain_value,
                    is_official=current_record.source.is_official,
                    confidence=normalized_confidence,
                    observed_at=observation.observed_at,
                    normalizer_version=normalized_version,
                    evidence_key=evidence_key,
                )
                return SourceEvidenceWriteResult(evidence=evidence, created=True)
        except IntegrityError as error:
            try:
                evidence = SourceEvidence.objects.select_related("raw_data_record").get(
                    evidence_key=evidence_key
                )
            except SourceEvidence.DoesNotExist:
                raise error from None
            _verify_existing_identity(
                evidence=evidence,
                raw_data_record_id=current_record.pk,
                target_type=normalized_target_type,
                target_id=normalized_target_id,
                field_name=normalized_field_name,
                normalized_value=normalized_domain_value,
                normalizer_version=normalized_version,
            )
            return SourceEvidenceWriteResult(evidence=evidence, created=False)


def _normalize_target_type(target_type: str) -> str:
    normalized = target_type.strip()
    allowed = {value for value, _ in SourceEvidence.TargetType.choices}
    if normalized not in allowed:
        raise InvalidSourceEvidence("target_type must be a supported SourceEvidence target.")
    return normalized


def _normalize_target_id(target_id: uuid.UUID) -> uuid.UUID:
    if not isinstance(target_id, uuid.UUID):
        raise InvalidSourceEvidence("target_id must be a UUID.")
    return target_id


def _normalize_field_name(field_name: str) -> str:
    normalized = field_name.strip()
    if len(normalized) > 100:
        raise InvalidSourceEvidence("field_name must be at most 100 characters.")
    if normalized and is_sensitive_field_name(normalized):
        raise SensitiveEvidenceValue("field_name must not identify a credential field.")
    return normalized


def _normalize_version(normalizer_version: str) -> str:
    normalized = normalizer_version.strip()
    if not normalized:
        raise InvalidSourceEvidence("normalizer_version must not be empty.")
    if len(normalized) > 100:
        raise InvalidSourceEvidence("normalizer_version must be at most 100 characters.")
    return normalized


def _normalize_confidence(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, bool):
        raise InvalidSourceEvidence("confidence must be a number between 0 and 1.")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise InvalidSourceEvidence("confidence must be a number between 0 and 1.") from error
    if not normalized.is_finite() or not Decimal("0") <= normalized <= Decimal("1"):
        raise InvalidSourceEvidence("confidence must be a number between 0 and 1.")
    return normalized.quantize(_CONFIDENCE_QUANTUM)


def _canonicalize_json(value: object, *, value_name: str) -> tuple[object, str]:
    try:
        normalized = normalize_json_without_credentials(value, value_name=value_name)
    except SensitiveAuditData as error:
        raise SensitiveEvidenceValue(str(error)) from None
    except InvalidAuditValue as error:
        raise InvalidSourceEvidence(str(error)) from None
    try:
        canonical = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise InvalidSourceEvidence(f"{value_name} must be valid JSON data.") from error
    return normalized, canonical


def _build_evidence_key(
    *,
    raw_data_record_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    field_name: str,
    canonical_normalized_value: str,
    normalizer_version: str,
) -> str:
    identity = {
        "field_name": field_name,
        "normalized_value": canonical_normalized_value,
        "normalizer_version": normalizer_version,
        "raw_data_record_id": str(raw_data_record_id),
        "target_id": str(target_id),
        "target_type": target_type,
    }
    serialized = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(serialized).hexdigest()


def _verify_existing_identity(
    *,
    evidence: SourceEvidence,
    raw_data_record_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    field_name: str,
    normalized_value: object,
    normalizer_version: str,
) -> None:
    if (
        evidence.raw_data_record_id != raw_data_record_id
        or evidence.target_type != target_type
        or evidence.target_id != target_id
        or evidence.field_name != field_name
        or evidence.normalized_value != normalized_value
        or evidence.normalizer_version != normalizer_version
    ):
        raise SourceEvidenceIntegrityError(
            "An existing SourceEvidence row has the same key but different identity data."
        )
