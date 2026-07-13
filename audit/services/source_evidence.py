import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import RawDataObservation, RawDataRecord, SourceEvidence, SyncRun

_CONFIDENCE_QUANTUM = Decimal("0.0001")
_NON_ALPHANUMERIC_RE = re.compile(r"[^a-z0-9]")
_SENSITIVE_KEY_SUFFIXES = (
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "cookie",
    "password",
    "refreshtoken",
    "secret",
    "session",
    "sessionid",
    "signature",
    "token",
)
_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(?:"
    r"\bauthorization\b\s*[:=]\s*(?:bearer|basic)?\s*[^\s,;}]+"
    r"|\bbearer\s+[^\s,;}]+"
    r"|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)\b"
    r"\s*[:=]\s*[^\s,;}]+"
    r")"
)


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
    if normalized and _is_sensitive_key(normalized):
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
    normalized = _normalize_json_value(value, path=value_name)
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


def _normalize_json_value(value: object, *, path: str) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidSourceEvidence(f"{path} JSON object keys must be strings.")
            if _is_sensitive_key(key):
                raise SensitiveEvidenceValue(f"{path} contains a credential-like key.")
            result[key] = _normalize_json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [
            _normalize_json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)
        ]
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidSourceEvidence(f"{path} must not contain NaN or infinity.")
        return value
    if isinstance(value, str):
        if _SENSITIVE_TEXT_RE.search(value):
            raise SensitiveEvidenceValue(f"{path} contains credential-like text.")
        return value
    raise InvalidSourceEvidence(f"{path} must contain JSON-compatible data.")


def _is_sensitive_key(key: str) -> bool:
    normalized = _NON_ALPHANUMERIC_RE.sub("", key.lower())
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


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
