import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import RawDataObservation, RawDataRecord, SyncRun
from audit.services.sync_runs import sanitize_error_summary

_URL_VALIDATOR = URLValidator(schemes=("http", "https"))
_NON_ALPHANUMERIC_RE = re.compile(r"[^a-z0-9]")
_SENSITIVE_KEY_MARKERS = (
    "apikey",
    "accesstoken",
    "authorization",
    "credential",
    "password",
    "secret",
    "signature",
    "token",
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
    normalized_method = method.strip().upper()
    if not normalized_method:
        raise InvalidRawDataRequest("Request method must not be empty.")
    _, canonical_url = _sanitize_urls(source_url)
    descriptor = {
        "identity": _redact_identity(dict(request_identity or {})),
        "method": normalized_method,
        "url": canonical_url,
    }
    try:
        serialized = json.dumps(
            descriptor,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as error:
        raise InvalidRawDataRequest(
            "request_identity must contain JSON-compatible data."
        ) from error
    return hashlib.sha256(serialized).hexdigest()


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
    stored_url, _ = _sanitize_urls(source_url)
    request_fingerprint = build_request_fingerprint(
        method=request_method,
        source_url=source_url,
        request_identity=request_identity,
    )
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


def _sanitize_urls(source_url: str) -> tuple[str, str]:
    try:
        _URL_VALIDATOR(source_url)
    except ValidationError as error:
        raise InvalidRawDataRequest("source_url must be a valid HTTP(S) URL.") from error

    parts = urlsplit(source_url)
    if parts.username is not None or parts.password is not None:
        raise InvalidRawDataRequest("source_url must not contain user credentials.")
    query_pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=1000)
    sanitized_pairs = [
        (key, "[REDACTED]" if _is_sensitive_key(key) else value) for key, value in query_pairs
    ]
    normalized_base = (parts.scheme.lower(), parts.netloc.lower(), parts.path, "", "")
    stored_url = urlunsplit((*normalized_base[:3], urlencode(sanitized_pairs), ""))
    canonical_url = urlunsplit((*normalized_base[:3], urlencode(sorted(sanitized_pairs)), ""))
    return stored_url, canonical_url


def _redact_identity(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidRawDataRequest("request_identity keys must be strings.")
            result[key] = "[REDACTED]" if _is_sensitive_key(key) else _redact_identity(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_identity(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise InvalidRawDataRequest("request_identity must contain JSON-compatible data.")


def _is_sensitive_key(key: str) -> bool:
    normalized = _NON_ALPHANUMERIC_RE.sub("", key.lower())
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


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
