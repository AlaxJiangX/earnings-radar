"""Canonical identity and safe start helpers for earnings calendar SyncRuns.

This service owns the earnings-specific scope and idempotency contract.  It
does not calculate the monitoring pool, acquire task-level locks, fetch
provider data, or run pagination.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from typing import cast

from audit.models import DataSource, SyncRun
from audit.security import AuditSecurityError, normalize_json_without_credentials
from audit.services import SyncRunStartResult, start_sync_run_with_result
from earnings.services.calendar_pagination import EARNINGS_CALENDAR_WINDOW_JOB_TYPE
from providers.exceptions import ProviderValidationError
from providers.types import ProviderCapability, validate_provider_key

EARNINGS_CALENDAR_SCHEDULED_IDEMPOTENCY_PREFIX = "earnings-calendar-scheduled:v1:"
EARNINGS_CALENDAR_REQUEST_IDEMPOTENCY_PREFIX = "earnings-calendar-request:v1:"
EARNINGS_CALENDAR_SCOPE_FIELDS = (
    "capability",
    "provider_key",
    "window_kind",
    "window_start",
    "window_end",
    "monitoring_pool_as_of",
    "monitoring_pool_hash",
    "selector_version",
)
MAX_SOURCE_KEY_LENGTH = 64
MAX_SCHEDULE_BUCKET_LENGTH = 100
MAX_SELECTOR_VERSION_LENGTH = 100
MAX_REQUEST_ID_LENGTH = 255
MAX_OPTIONAL_VERSION_LENGTH = 100

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_WINDOW_KINDS = frozenset({"manual", "backfill", "retry"})


class EarningsCalendarSyncIdentityError(ValueError):
    """Base class for invalid earnings calendar sync identity input."""


class InvalidEarningsCalendarSyncIdentity(EarningsCalendarSyncIdentityError):
    """Raised when a scope or idempotency identity cannot be constructed safely."""


class EarningsCalendarSyncRunError(RuntimeError):
    """Base class for a scheduled run that cannot be owned by this caller."""

    def __init__(self, message: str, *, sync_run: SyncRun) -> None:
        super().__init__(message)
        self.sync_run = sync_run
        self.created = False


class EarningsCalendarSyncRunAlreadyRunning(EarningsCalendarSyncRunError):
    """Raised when another caller already owns the same scheduled identity."""


class EarningsCalendarSyncRunRetryRequired(EarningsCalendarSyncRunError):
    """Raised when a failed/partial run requires a new explicit request identity."""


class EarningsCalendarSyncRunContextMismatch(EarningsCalendarSyncRunError):
    """Raised when an existing idempotency key has different immutable context."""


class EarningsCalendarWindowKind(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    BACKFILL = "backfill"
    RETRY = "retry"


def build_earnings_calendar_sync_scope(
    *,
    provider_key: str,
    window_kind: EarningsCalendarWindowKind | str,
    window_start: date,
    window_end: date,
    monitoring_pool_as_of: date,
    monitoring_pool_hash: str,
    selector_version: str,
) -> dict[str, object]:
    """Build the canonical, credential-free scope for one logical window."""

    normalized_provider_key = _normalize_provider_key(provider_key)
    normalized_window_kind = _normalize_window_kind(window_kind)
    normalized_window_start = _require_date(window_start, value_name="window_start")
    normalized_window_end = _require_date(window_end, value_name="window_end")
    if normalized_window_start > normalized_window_end:
        raise InvalidEarningsCalendarSyncIdentity("window_start must not be later than window_end.")
    normalized_pool_as_of = _require_date(
        monitoring_pool_as_of,
        value_name="monitoring_pool_as_of",
    )
    normalized_pool_hash = _normalize_pool_hash(monitoring_pool_hash)
    normalized_selector_version = _normalize_bounded_text(
        selector_version,
        value_name="selector_version",
        maximum_length=MAX_SELECTOR_VERSION_LENGTH,
    )

    scope: dict[str, object] = {
        "capability": ProviderCapability.EARNINGS_CALENDAR.value,
        "provider_key": normalized_provider_key,
        "window_kind": normalized_window_kind.value,
        "window_start": normalized_window_start.isoformat(),
        "window_end": normalized_window_end.isoformat(),
        "monitoring_pool_as_of": normalized_pool_as_of.isoformat(),
        "monitoring_pool_hash": normalized_pool_hash,
        "selector_version": normalized_selector_version,
    }
    return _normalize_identity_mapping(scope, value_name="Earnings calendar SyncRun scope")


def build_scheduled_earnings_calendar_idempotency_key(
    *,
    source_key: str,
    provider_key: str,
    window_start: date,
    window_end: date,
    monitoring_pool_as_of: date,
    monitoring_pool_hash: str,
    selector_version: str,
    schedule_bucket: str,
) -> str:
    """Build the deterministic identity of one scheduled logical window."""

    normalized_source_key = _normalize_bounded_text(
        source_key,
        value_name="source_key",
        maximum_length=MAX_SOURCE_KEY_LENGTH,
    )
    normalized_schedule_bucket = _normalize_bounded_text(
        schedule_bucket,
        value_name="schedule_bucket",
        maximum_length=MAX_SCHEDULE_BUCKET_LENGTH,
    )
    scope = build_earnings_calendar_sync_scope(
        provider_key=provider_key,
        window_kind=EarningsCalendarWindowKind.SCHEDULED,
        window_start=window_start,
        window_end=window_end,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
    )
    return _build_idempotency_key(
        prefix=EARNINGS_CALENDAR_SCHEDULED_IDEMPOTENCY_PREFIX,
        identity={
            "schedule_bucket": normalized_schedule_bucket,
            "scope": scope,
            "source_key": normalized_source_key,
        },
    )


def build_manual_earnings_calendar_idempotency_key(
    *,
    source_key: str,
    provider_key: str,
    window_kind: EarningsCalendarWindowKind | str,
    window_start: date,
    window_end: date,
    monitoring_pool_as_of: date,
    monitoring_pool_hash: str,
    selector_version: str,
    request_id: str,
) -> str:
    """Build explicit manual, backfill, or retry identity from a caller request id."""

    normalized_source_key = _normalize_bounded_text(
        source_key,
        value_name="source_key",
        maximum_length=MAX_SOURCE_KEY_LENGTH,
    )
    normalized_request_id = _normalize_bounded_text(
        request_id,
        value_name="request_id",
        maximum_length=MAX_REQUEST_ID_LENGTH,
    )
    normalized_window_kind = _normalize_window_kind(window_kind)
    if normalized_window_kind.value not in _REQUEST_WINDOW_KINDS:
        raise InvalidEarningsCalendarSyncIdentity(
            "manual idempotency requires manual, backfill, or retry window_kind."
        )
    scope = build_earnings_calendar_sync_scope(
        provider_key=provider_key,
        window_kind=normalized_window_kind,
        window_start=window_start,
        window_end=window_end,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
    )
    return _build_idempotency_key(
        prefix=EARNINGS_CALENDAR_REQUEST_IDEMPOTENCY_PREFIX,
        identity={
            "request_id": normalized_request_id,
            "scope": scope,
            "source_key": normalized_source_key,
        },
    )


def start_scheduled_earnings_calendar_sync_run(
    *,
    source: DataSource,
    provider_key: str,
    window_start: date,
    window_end: date,
    monitoring_pool_as_of: date,
    monitoring_pool_hash: str,
    selector_version: str,
    schedule_bucket: str,
    code_version: str = "",
    parser_version: str = "",
    started_at: datetime | None = None,
) -> SyncRunStartResult:
    """Create or safely inspect the caller-owned scheduled SyncRun."""

    normalized_provider_key = _normalize_provider_key(provider_key)
    current_source = _load_enabled_earnings_calendar_source(
        source,
        provider_key=normalized_provider_key,
    )
    normalized_code_version = _normalize_optional_version(
        code_version,
        value_name="code_version",
    )
    normalized_parser_version = _normalize_optional_version(
        parser_version,
        value_name="parser_version",
    )
    expected_scope = build_earnings_calendar_sync_scope(
        provider_key=normalized_provider_key,
        window_kind=EarningsCalendarWindowKind.SCHEDULED,
        window_start=window_start,
        window_end=window_end,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
    )
    idempotency_key = build_scheduled_earnings_calendar_idempotency_key(
        source_key=current_source.key,
        provider_key=normalized_provider_key,
        window_start=window_start,
        window_end=window_end,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
        schedule_bucket=schedule_bucket,
    )
    result = start_sync_run_with_result(
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        source=current_source,
        scope=expected_scope,
        idempotency_key=idempotency_key,
        code_version=normalized_code_version,
        parser_version=normalized_parser_version,
        started_at=started_at,
    )
    if result.created:
        return result

    existing = result.sync_run
    _verify_existing_run_context(
        sync_run=existing,
        source=current_source,
        scope=expected_scope,
        provider_key=normalized_provider_key,
        code_version=normalized_code_version,
        parser_version=normalized_parser_version,
    )
    if existing.status == SyncRun.Status.RUNNING:
        raise EarningsCalendarSyncRunAlreadyRunning(
            "An earnings calendar scheduled run with this identity is already running.",
            sync_run=existing,
        )
    if existing.status == SyncRun.Status.SUCCEEDED:
        return SyncRunStartResult(sync_run=existing, created=False)
    raise EarningsCalendarSyncRunRetryRequired(
        "The existing earnings calendar scheduled run did not succeed; "
        "use a new explicit request identity.",
        sync_run=existing,
    )


def _load_enabled_earnings_calendar_source(
    source: DataSource,
    *,
    provider_key: str,
) -> DataSource:
    if not isinstance(source, DataSource):
        raise InvalidEarningsCalendarSyncIdentity("source must be a DataSource.")
    if source._state.adding or source.pk is None:
        raise InvalidEarningsCalendarSyncIdentity("source must be saved before use.")
    try:
        current_source = DataSource.objects.get(pk=source.pk)
    except DataSource.DoesNotExist as error:
        raise InvalidEarningsCalendarSyncIdentity("source no longer exists.") from error
    if current_source.source_type != DataSource.SourceType.EARNINGS_CALENDAR:
        raise InvalidEarningsCalendarSyncIdentity(
            "source must use the earnings_calendar source type."
        )
    if not current_source.is_enabled:
        raise InvalidEarningsCalendarSyncIdentity("source must be enabled.")
    if current_source.provider_adapter != provider_key:
        raise InvalidEarningsCalendarSyncIdentity(
            "provider_key must match the source provider_adapter."
        )
    return current_source


def _verify_existing_run_context(
    *,
    sync_run: SyncRun,
    source: DataSource,
    scope: Mapping[str, object],
    provider_key: str,
    code_version: str,
    parser_version: str,
) -> None:
    existing_scope = sync_run.scope if isinstance(sync_run.scope, dict) else None
    if (
        sync_run.source_id != source.pk
        or sync_run.job_type != EARNINGS_CALENDAR_WINDOW_JOB_TYPE
        or existing_scope != dict(scope)
        or existing_scope.get("provider_key") != provider_key
        or sync_run.code_version != code_version
        or sync_run.parser_version != parser_version
    ):
        raise EarningsCalendarSyncRunContextMismatch(
            "The existing SyncRun context does not match this scheduled identity.",
            sync_run=sync_run,
        )


def _build_idempotency_key(
    *,
    prefix: str,
    identity: Mapping[str, object],
) -> str:
    normalized_identity = _normalize_identity_mapping(
        identity,
        value_name="Earnings calendar idempotency identity",
    )
    serialized = json.dumps(
        normalized_identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return f"{prefix}{hashlib.sha256(serialized).hexdigest()}"


def _normalize_identity_mapping(
    value: Mapping[str, object],
    *,
    value_name: str,
) -> dict[str, object]:
    try:
        normalized = normalize_json_without_credentials(dict(value), value_name=value_name)
    except AuditSecurityError as error:
        raise InvalidEarningsCalendarSyncIdentity(str(error)) from None
    if not isinstance(normalized, dict):
        raise InvalidEarningsCalendarSyncIdentity(f"{value_name} must be a JSON object.")
    return cast(dict[str, object], normalized)


def _normalize_provider_key(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsCalendarSyncIdentity("provider_key must be a string.")
    try:
        return validate_provider_key(value.strip())
    except ProviderValidationError as error:
        raise InvalidEarningsCalendarSyncIdentity(str(error)) from None


def _normalize_window_kind(
    value: EarningsCalendarWindowKind | str,
) -> EarningsCalendarWindowKind:
    if isinstance(value, EarningsCalendarWindowKind):
        return value
    if not isinstance(value, str):
        raise InvalidEarningsCalendarSyncIdentity("window_kind must be a string.")
    try:
        return EarningsCalendarWindowKind(value.strip().lower())
    except ValueError as error:
        raise InvalidEarningsCalendarSyncIdentity(
            "window_kind must be scheduled, manual, backfill, or retry."
        ) from error


def _require_date(value: object, *, value_name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise InvalidEarningsCalendarSyncIdentity(f"{value_name} must be a date.")
    return value


def _normalize_pool_hash(value: object) -> str:
    if not isinstance(value, str) or not _SHA256_HEX_RE.fullmatch(value):
        raise InvalidEarningsCalendarSyncIdentity(
            "monitoring_pool_hash must be 64 lowercase hexadecimal characters."
        )
    return value


def _normalize_bounded_text(
    value: object,
    *,
    value_name: str,
    maximum_length: int,
) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsCalendarSyncIdentity(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidEarningsCalendarSyncIdentity(f"{value_name} must not be empty.")
    if len(normalized) > maximum_length:
        raise InvalidEarningsCalendarSyncIdentity(
            f"{value_name} must contain at most {maximum_length} characters."
        )
    return normalized


def _normalize_optional_version(value: object, *, value_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsCalendarSyncIdentity(f"{value_name} must be a string.")
    normalized = value.strip()
    if len(normalized) > MAX_OPTIONAL_VERSION_LENGTH:
        raise InvalidEarningsCalendarSyncIdentity(
            f"{value_name} must contain at most {MAX_OPTIONAL_VERSION_LENGTH} characters."
        )
    return normalized
