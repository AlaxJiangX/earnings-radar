import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import RawDataObservation, RawDataRecord, SyncRun
from audit.security import (
    AuditSecurityError,
    ProviderRequestContextDescriptor,
    build_safe_request_descriptor,
    ensure_payload_has_no_credentials,
    sanitize_error_summary,
    sanitize_url,
)


class InvalidRawDataRequest(ValueError):
    pass


class InvalidRawDataTimestamp(ValueError):
    pass


class PayloadTooLarge(ValueError):
    pass


class RawDataIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RawDataIngestResult:
    record: RawDataRecord
    observation: RawDataObservation
    record_created: bool
    observation_created: bool


def build_request_fingerprint(
    *,
    method: str,
    source_url: str,
    request_identity: Mapping[str, object] | None = None,
) -> str:
    try:
        descriptor = build_safe_request_descriptor(
            method=method,
            source_url=source_url,
            request_identity=request_identity,
        )
    except AuditSecurityError as error:
        raise InvalidRawDataRequest(str(error)) from None
    return descriptor.fingerprint


def record_raw_data_observation(
    *,
    sync_run: SyncRun,
    source_url: str,
    payload: bytes,
    request_method: str = "GET",
    request_identity: Mapping[str, object] | None = None,
    fetched_at: datetime | None = None,
    observed_at: datetime | None = None,
    http_status: int | None = None,
    content_type: str = "",
    encoding: str = "",
    request_descriptor: ProviderRequestContextDescriptor | None = None,
) -> RawDataIngestResult:
    if not isinstance(payload, bytes):
        raise TypeError("Raw payload must be bytes.")
    payload_size = len(payload)
    if payload_size > settings.RAW_DATA_MAX_PAYLOAD_BYTES:
        raise PayloadTooLarge(
            f"Raw payload is {payload_size} bytes; limit is "
            f"{settings.RAW_DATA_MAX_PAYLOAD_BYTES} bytes."
        )
    if http_status is not None and not 100 <= http_status <= 599:
        raise InvalidRawDataRequest("HTTP status must be between 100 and 599.")

    fetched_timestamp = _aware_timestamp(fetched_at)
    observed_timestamp = _aware_timestamp(observed_at or fetched_timestamp)
    try:
        stored_url = sanitize_url(source_url).stored
        ensure_payload_has_no_credentials(payload)
    except AuditSecurityError as error:
        raise InvalidRawDataRequest(str(error)) from None
    if request_descriptor is None:
        request_fingerprint = build_request_fingerprint(
            method=request_method,
            source_url=source_url,
            request_identity=request_identity,
        )
    else:
        if stored_url != request_descriptor.source_url.stored:
            raise InvalidRawDataRequest(
                "Raw data source URL does not match the trusted request descriptor."
            )
        if request_method.strip().upper() != request_descriptor.method:
            raise InvalidRawDataRequest(
                "Raw data method does not match the trusted request descriptor."
            )
        request_fingerprint = request_descriptor.fingerprint
    content_hash = hashlib.sha256(payload).hexdigest()

    with transaction.atomic():
        current_run = (
            SyncRun.objects.select_for_update().select_related("source").get(pk=sync_run.pk)
        )
        if current_run.status != SyncRun.Status.RUNNING:
            raise InvalidRawDataRequest("Raw data can only be recorded for a running SyncRun.")

        record, record_created = _get_or_create_record(
            sync_run=current_run,
            source_url=stored_url,
            request_fingerprint=request_fingerprint,
            fetched_at=fetched_timestamp,
            http_status=http_status,
            content_type=content_type,
            encoding=encoding,
            content_hash=content_hash,
            payload=payload,
            payload_size=payload_size,
        )
        if not record_created and (
            record.payload_size_bytes != payload_size or bytes(record.payload) != payload
        ):
            raise RawDataIntegrityError(
                "An existing raw record has the same identity but different payload bytes."
            )

        observation, observation_created = _get_or_create_observation(
            sync_run=current_run,
            raw_data_record=record,
            observed_at=observed_timestamp,
        )
        return RawDataIngestResult(
            record=record,
            observation=observation,
            record_created=record_created,
            observation_created=observation_created,
        )


def mark_raw_data_parsed(
    raw_data_record_id: uuid.UUID,
    *,
    parser_version: str,
) -> RawDataRecord:
    normalized_version = _require_parser_version(parser_version)
    with transaction.atomic():
        record = RawDataRecord.objects.select_for_update().get(pk=raw_data_record_id)
        record.parser_status = RawDataRecord.ParserStatus.PARSED
        record.parser_version = normalized_version
        record.parse_error = ""
        record.save(update_fields=("parser_status", "parser_version", "parse_error"))
        return record


def mark_raw_data_parse_failed(
    raw_data_record_id: uuid.UUID,
    *,
    parser_version: str,
    parse_error: str,
) -> RawDataRecord:
    normalized_version = _require_parser_version(parser_version)
    sanitized_error = sanitize_error_summary(parse_error)
    if not sanitized_error:
        raise ValueError("A failed parse requires a non-empty error summary.")
    with transaction.atomic():
        record = RawDataRecord.objects.select_for_update().get(pk=raw_data_record_id)
        record.parser_status = RawDataRecord.ParserStatus.FAILED
        record.parser_version = normalized_version
        record.parse_error = sanitized_error
        record.save(update_fields=("parser_status", "parser_version", "parse_error"))
        return record


def mark_raw_data_unsupported(
    raw_data_record_id: uuid.UUID,
    *,
    parser_version: str,
) -> RawDataRecord:
    normalized_version = _require_parser_version(parser_version)
    with transaction.atomic():
        record = RawDataRecord.objects.select_for_update().get(pk=raw_data_record_id)
        record.parser_status = RawDataRecord.ParserStatus.UNSUPPORTED
        record.parser_version = normalized_version
        record.parse_error = ""
        record.save(update_fields=("parser_status", "parser_version", "parse_error"))
        return record


def _get_or_create_record(
    *,
    sync_run: SyncRun,
    source_url: str,
    request_fingerprint: str,
    fetched_at: datetime,
    http_status: int | None,
    content_type: str,
    encoding: str,
    content_hash: str,
    payload: bytes,
    payload_size: int,
) -> tuple[RawDataRecord, bool]:
    try:
        with transaction.atomic():
            record = RawDataRecord.objects.create(
                source=sync_run.source,
                first_sync_run=sync_run,
                source_url=source_url,
                request_fingerprint=request_fingerprint,
                fetched_at=fetched_at,
                http_status=http_status,
                content_type=content_type[:255],
                encoding=encoding[:64],
                content_hash=content_hash,
                payload=payload,
                payload_size_bytes=payload_size,
            )
            return record, True
    except IntegrityError as error:
        try:
            record = RawDataRecord.objects.get(
                source=sync_run.source,
                request_fingerprint=request_fingerprint,
                content_hash=content_hash,
            )
        except RawDataRecord.DoesNotExist:
            raise error from None
        return record, False


def _get_or_create_observation(
    *,
    sync_run: SyncRun,
    raw_data_record: RawDataRecord,
    observed_at: datetime,
) -> tuple[RawDataObservation, bool]:
    try:
        with transaction.atomic():
            observation = RawDataObservation.objects.create(
                sync_run=sync_run,
                raw_data_record=raw_data_record,
                observed_at=observed_at,
            )
            return observation, True
    except IntegrityError as error:
        try:
            observation = RawDataObservation.objects.get(
                sync_run=sync_run,
                raw_data_record=raw_data_record,
            )
        except RawDataObservation.DoesNotExist:
            raise error from None
        return observation, False


def _aware_timestamp(value: datetime | None) -> datetime:
    result = value or timezone.now()
    if timezone.is_naive(result):
        raise InvalidRawDataTimestamp("Raw data timestamps must be timezone-aware.")
    return result


def _require_parser_version(parser_version: str) -> str:
    normalized = parser_version.strip()
    if not normalized:
        raise ValueError("parser_version must not be empty.")
    return normalized[:100]
