"""Persistence and identity primitives for offline earnings-calendar replay.

This module deliberately stops at replay foundation.  It validates the
persisted source-run contract, builds deterministic evidence identities, and
starts a lineage-linked replay ``SyncRun``.  It does not fetch providers or
execute parsing/normalization orchestration.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from django.db import transaction
from django.utils import timezone

from audit.models import DataSource, RawDataObservation, SyncRun
from audit.security import AuditSecurityError, normalize_json_without_credentials
from audit.services import SyncRunStartContextMismatch, start_sync_run_with_result
from earnings.services.calendar_pagination import EARNINGS_CALENDAR_WINDOW_JOB_TYPE
from earnings.services.calendar_run_ownership import (
    EarningsCalendarRunBusy,
    assert_calendar_run_ownership,
    calendar_run_ownership,
)
from earnings.services.calendar_sync_identity import (
    EARNINGS_CALENDAR_SCOPE_FIELDS,
    EarningsCalendarWindowKind,
    InvalidEarningsCalendarSyncIdentity,
    build_earnings_calendar_sync_scope,
)

EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION = "1"
EARNINGS_CALENDAR_REPLAY_IDEMPOTENCY_PREFIX = "earnings-calendar-replay:v1:"
_REPLAY_DIGEST_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SOURCE_STATUSES = frozenset(
    {SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL, SyncRun.Status.FAILED}
)


class EarningsCalendarReplayFoundationError(ValueError):
    """Base error for invalid replay foundation input."""


class InvalidEarningsCalendarReplay(EarningsCalendarReplayFoundationError):
    """The source, contract, or replay identity is invalid."""


class ReplayProviderContextUnavailable(InvalidEarningsCalendarReplay):
    """The source run lacks persisted provider-version provenance."""


class EarningsCalendarReplayContextMismatch(RuntimeError):
    """An existing replay identity has immutable context different from the request."""


class EarningsCalendarReplayCountMismatch(RuntimeError):
    """A persisted replay count is ahead of its durable evidence facts."""


@dataclass(frozen=True, slots=True)
class EarningsCalendarReplayStartResult:
    sync_run: SyncRun
    created: bool
    replay_input_digest: str
    idempotency_key: str


def validate_earnings_calendar_replay_source(
    *,
    source_sync_run: SyncRun | uuid.UUID,
    source: DataSource,
    job_type: str = EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
) -> SyncRun:
    """Reload and validate a terminal ingestion run as an offline replay source."""

    if not isinstance(source, DataSource) or source._state.adding or source.pk is None:
        raise InvalidEarningsCalendarReplay("source must be saved before replay.")
    source_run_id = _saved_uuid(source_sync_run, value_name="source_sync_run")
    try:
        persisted = SyncRun.objects.select_related("source").get(pk=source_run_id)
    except SyncRun.DoesNotExist as error:
        raise InvalidEarningsCalendarReplay("source SyncRun does not exist.") from error

    _require_ingestion_source(persisted)
    if persisted.source_id != source.pk:
        raise InvalidEarningsCalendarReplay("Replay source DataSource does not match the request.")
    if persisted.job_type != job_type:
        raise InvalidEarningsCalendarReplay("Replay source SyncRun has the wrong job type.")
    if persisted.status not in _ALLOWED_SOURCE_STATUSES:
        raise InvalidEarningsCalendarReplay(
            "Replay source SyncRun must be terminal and replayable."
        )
    scope = _validate_earnings_calendar_scope(persisted.scope, allow_replay=False)
    if persisted.source.source_type != DataSource.SourceType.EARNINGS_CALENDAR:
        raise InvalidEarningsCalendarReplay(
            "Replay source must use the earnings_calendar source type."
        )
    if persisted.source.provider_adapter != scope["provider_key"]:
        raise InvalidEarningsCalendarReplay(
            "Replay source provider_adapter does not match the persisted scope."
        )
    _require_provider_version(persisted)
    _validate_source_observations(persisted)
    return persisted


def validate_earnings_calendar_replay_pool_contract(
    *,
    source_sync_run: SyncRun,
    monitoring_pool_as_of: date,
    monitoring_pool_hash: str,
    selector_version: str,
) -> None:
    """Validate Option A's persisted monitoring-pool contract without a selector call."""

    source_run = _load_sync_run(source_sync_run)
    _require_ingestion_source(source_run)
    source = _validate_earnings_calendar_scope(source_run.scope, allow_replay=False)
    try:
        expected = build_earnings_calendar_sync_scope(
            provider_key=cast(str, source["provider_key"]),
            window_kind=cast(str, source["window_kind"]),
            window_start=date.fromisoformat(cast(str, source["window_start"])),
            window_end=date.fromisoformat(cast(str, source["window_end"])),
            monitoring_pool_as_of=monitoring_pool_as_of,
            monitoring_pool_hash=monitoring_pool_hash,
            selector_version=selector_version,
        )
    except (TypeError, ValueError, InvalidEarningsCalendarSyncIdentity) as error:
        raise InvalidEarningsCalendarReplay(
            "Replay monitoring-pool contract is invalid."
        ) from error
    for field in ("monitoring_pool_as_of", "monitoring_pool_hash", "selector_version"):
        if source[field] != expected[field]:
            raise InvalidEarningsCalendarReplay(
                f"Replay monitoring-pool contract differs for {field}."
            )


def build_earnings_calendar_replay_input_digest(
    *,
    source_sync_run: SyncRun | uuid.UUID,
    parser_version: str,
    replay_contract_version: str = EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
    observations: Iterable[RawDataObservation] | None = None,
) -> str:
    """Build a stable SHA-256 digest for the source run's raw evidence set."""

    source_run = _load_sync_run(source_sync_run)
    _require_ingestion_source(source_run)
    _validate_earnings_calendar_scope(source_run.scope, allow_replay=False)
    provider_version = _require_provider_version(source_run)
    normalized_parser_version = _required_text(parser_version, "parser_version", maximum=100)
    normalized_contract_version = _required_text(
        replay_contract_version,
        "replay_contract_version",
        maximum=100,
    )
    evidence = _load_ordered_evidence(source_run, observations=observations)
    evidence_items = [_build_evidence_item(source_run, observation) for observation in evidence]
    identity = {
        "contract_version": normalized_contract_version,
        "parser_version": normalized_parser_version,
        "provider_version": provider_version,
        "source_run_id": str(source_run.pk),
        "source_scope": source_run.scope,
        "evidence": evidence_items,
    }
    try:
        normalized = normalize_json_without_credentials(identity, value_name="replay input digest")
    except AuditSecurityError as error:
        raise InvalidEarningsCalendarReplay(str(error)) from None
    serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


def load_earnings_calendar_replay_evidence(
    *,
    source_sync_run: SyncRun | uuid.UUID,
) -> tuple[RawDataObservation, ...]:
    """Load the canonical, hash-validated raw evidence manifest for replay."""

    source_run = _load_sync_run(source_sync_run)
    _require_ingestion_source(source_run)
    _validate_earnings_calendar_scope(source_run.scope, allow_replay=False)
    _require_provider_version(source_run)
    _validate_source_observations(source_run)
    evidence = _load_ordered_evidence(source_run)
    for observation in evidence:
        _build_evidence_item(source_run, observation)
    return evidence


def build_earnings_calendar_replay_idempotency_key(
    *,
    source: DataSource,
    source_sync_run: SyncRun | uuid.UUID,
    replay_input_digest: str,
    parser_version: str,
    replay_contract_version: str = EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
) -> str:
    """Build one deterministic replay identity; repeated equivalent requests reuse it."""

    if not isinstance(source, DataSource) or source._state.adding or source.pk is None:
        raise InvalidEarningsCalendarReplay("source must be saved before replay.")
    source_run_id = _saved_uuid(source_sync_run, value_name="source_sync_run")
    normalized_digest = _required_digest(replay_input_digest, "replay_input_digest")
    normalized_parser_version = _required_text(parser_version, "parser_version", maximum=100)
    normalized_contract_version = _required_text(
        replay_contract_version,
        "replay_contract_version",
        maximum=100,
    )
    source_run = _load_sync_run(source_run_id)
    if source.pk != source_run.source_id:
        raise InvalidEarningsCalendarReplay("source DataSource does not match source SyncRun.")
    scope = _validate_earnings_calendar_scope(source_run.scope, allow_replay=False)
    identity = {
        "run_mode": SyncRun.RunMode.REPLAY,
        "source_id": str(source_run.source_id),
        "job_type": EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        "source_sync_run_id": str(source_run_id),
        "logical_window": {
            field: scope[field]
            for field in (
                "provider_key",
                "window_start",
                "window_end",
                "monitoring_pool_as_of",
                "monitoring_pool_hash",
                "selector_version",
            )
        },
        "replay_contract_version": normalized_contract_version,
        "replay_input_digest": normalized_digest,
        "parser_version": normalized_parser_version,
    }
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(serialized.encode("ascii")).hexdigest()
    return f"{EARNINGS_CALENDAR_REPLAY_IDEMPOTENCY_PREFIX}{digest}"


def start_earnings_calendar_replay_sync_run(
    *,
    source: DataSource,
    source_sync_run: SyncRun | uuid.UUID,
    parser_version: str | None = None,
    replay_contract_version: str = EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
    monitoring_pool_as_of: date | None = None,
    monitoring_pool_hash: str | None = None,
    selector_version: str | None = None,
    code_version: str = "",
    started_at: datetime | None = None,
    resume_stale_before: datetime | None = None,
) -> EarningsCalendarReplayStartResult:
    """Create or reuse a lineage-linked replay run; no provider or parser work is performed."""

    source_run = validate_earnings_calendar_replay_source(
        source_sync_run=source_sync_run,
        source=source,
    )
    if resume_stale_before is not None and timezone.is_naive(resume_stale_before):
        raise InvalidEarningsCalendarReplay("resume_stale_before must be timezone-aware.")
    source_scope = _validate_earnings_calendar_scope(source_run.scope, allow_replay=False)
    if monitoring_pool_as_of is None:
        monitoring_pool_as_of = date.fromisoformat(cast(str, source_scope["monitoring_pool_as_of"]))
    if monitoring_pool_hash is None:
        monitoring_pool_hash = cast(str, source_scope["monitoring_pool_hash"])
    if selector_version is None:
        selector_version = cast(str, source_scope["selector_version"])
    validate_earnings_calendar_replay_pool_contract(
        source_sync_run=source_run,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
    )
    effective_parser_version = (
        source_run.parser_version if parser_version is None else parser_version
    )
    effective_parser_version = _required_text(
        effective_parser_version,
        "parser_version",
        maximum=100,
    )
    normalized_contract_version = _required_text(
        replay_contract_version,
        "replay_contract_version",
        maximum=100,
    )
    digest = build_earnings_calendar_replay_input_digest(
        source_sync_run=source_run,
        parser_version=effective_parser_version,
        replay_contract_version=normalized_contract_version,
    )
    idempotency_key = build_earnings_calendar_replay_idempotency_key(
        source=source,
        source_sync_run=source_run,
        replay_input_digest=digest,
        parser_version=effective_parser_version,
        replay_contract_version=normalized_contract_version,
    )
    replay_scope = dict(source_scope)
    replay_scope["window_kind"] = EarningsCalendarWindowKind.REPLAY.value
    with calendar_run_ownership(
        source_id=source.pk,
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    ):
        try:
            result = start_sync_run_with_result(
                job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
                source=source,
                scope=replay_scope,
                idempotency_key=idempotency_key,
                code_version=code_version,
                parser_version=effective_parser_version,
                started_at=started_at,
                run_mode=SyncRun.RunMode.REPLAY,
                replay_source_sync_run=source_run,
                replay_contract_version=normalized_contract_version,
                replay_input_digest=digest,
                provider_version=source_run.provider_version,
            )
        except SyncRunStartContextMismatch as error:
            raise EarningsCalendarReplayContextMismatch(str(error)) from error
        if not result.created:
            _validate_existing_replay_context(
                result.sync_run,
                source=source,
                source_run=source_run,
                scope=replay_scope,
                parser_version=effective_parser_version,
                replay_contract_version=normalized_contract_version,
                replay_input_digest=digest,
            )
            if result.sync_run.status == SyncRun.Status.RUNNING and (
                resume_stale_before is None or result.sync_run.heartbeat_at > resume_stale_before
            ):
                raise EarningsCalendarRunBusy("This offline replay identity is already running.")
    return EarningsCalendarReplayStartResult(
        sync_run=result.sync_run,
        created=result.created,
        replay_input_digest=digest,
        idempotency_key=idempotency_key,
    )


def reconcile_earnings_calendar_replayed_count(sync_run_id: uuid.UUID) -> SyncRun:
    """Rebuild replay progress from durable replay observations under row lock.

    ``replayed_count`` counts replay-linked raw observations.  This is the
    durable unit that proves which source evidence entered the replay path;
    parse attempts and normalized rows always depend on that observation.
    """

    with transaction.atomic():
        sync_run = SyncRun.objects.select_for_update().get(pk=sync_run_id)
        _require_replay_run(sync_run)
        if sync_run.fetched_count:
            raise EarningsCalendarReplayCountMismatch(
                "A replay run cannot contain provider fetch progress."
            )
        observation_count = RawDataObservation.objects.filter(sync_run_id=sync_run.pk).count()
        if sync_run.replayed_count > observation_count:
            raise EarningsCalendarReplayCountMismatch(
                "replayed_count is ahead of persisted replay observations."
            )
        if sync_run.replayed_count != observation_count:
            sync_run.replayed_count = observation_count
            sync_run.save(update_fields=("replayed_count",))
        return sync_run


def retire_stale_earnings_calendar_replay_run(
    sync_run: SyncRun,
    *,
    cutoff: datetime,
) -> SyncRun:
    """Finish one stale replay from persisted replay facts while preserving source history."""

    if timezone.is_naive(cutoff):
        raise InvalidEarningsCalendarReplay("stale cutoff must be timezone-aware.")
    _require_replay_run(sync_run)
    if sync_run.status != SyncRun.Status.RUNNING:
        return sync_run
    with transaction.atomic():
        current = SyncRun.objects.select_for_update().get(pk=sync_run.pk)
        _require_replay_run(current)
        assert_calendar_run_ownership(
            source_id=current.source_id,
            job_type=current.job_type,
        )
        if current.status != SyncRun.Status.RUNNING:
            return current
        if current.heartbeat_at > cutoff:
            raise RuntimeError("Replay SyncRun is still within its heartbeat grace period.")
        if current.fetched_count:
            raise EarningsCalendarReplayCountMismatch(
                "A replay run cannot contain provider fetch progress."
            )
        observations = RawDataObservation.objects.filter(sync_run_id=current.pk).count()
        if current.replayed_count > observations:
            raise EarningsCalendarReplayCountMismatch(
                "replayed_count is ahead of persisted replay observations."
            )
        current.replayed_count = observations
        current.failed_count += 1
        current.status = SyncRun.Status.PARTIAL if observations else SyncRun.Status.FAILED
        current.finished_at = timezone.now()
        current.heartbeat_at = current.finished_at
        current.error_summary = (
            "Offline replay run lost ownership and exceeded its heartbeat threshold."
        )
        current.save(
            update_fields=(
                "replayed_count",
                "failed_count",
                "status",
                "finished_at",
                "heartbeat_at",
                "error_summary",
            )
        )
        return current


def _validate_existing_replay_context(
    sync_run: SyncRun,
    *,
    source: DataSource,
    source_run: SyncRun,
    scope: Mapping[str, object],
    parser_version: str,
    replay_contract_version: str,
    replay_input_digest: str,
) -> None:
    if (
        sync_run.run_mode != SyncRun.RunMode.REPLAY
        or sync_run.source_id != source.pk
        or sync_run.job_type != EARNINGS_CALENDAR_WINDOW_JOB_TYPE
        or sync_run.scope != dict(scope)
        or sync_run.replay_source_sync_run_id != source_run.pk
        or sync_run.parser_version != parser_version
        or sync_run.replay_contract_version != replay_contract_version.strip()
        or sync_run.replay_input_digest != replay_input_digest
        or sync_run.provider_version != source_run.provider_version
        or sync_run.fetched_count != 0
    ):
        raise EarningsCalendarReplayContextMismatch(
            "Existing replay identity has different immutable context."
        )


def _validate_earnings_calendar_scope(value: object, *, allow_replay: bool) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(EARNINGS_CALENDAR_SCOPE_FIELDS):
        raise InvalidEarningsCalendarReplay("Earnings calendar SyncRun scope is not canonical.")
    try:
        window_kind = EarningsCalendarWindowKind(cast(str, value["window_kind"]))
        if not allow_replay and window_kind == EarningsCalendarWindowKind.REPLAY:
            raise InvalidEarningsCalendarReplay("Source SyncRun cannot have replay window_kind.")
        canonical = build_earnings_calendar_sync_scope(
            provider_key=cast(str, value["provider_key"]),
            window_kind=window_kind,
            window_start=date.fromisoformat(cast(str, value["window_start"])),
            window_end=date.fromisoformat(cast(str, value["window_end"])),
            monitoring_pool_as_of=date.fromisoformat(cast(str, value["monitoring_pool_as_of"])),
            monitoring_pool_hash=cast(str, value["monitoring_pool_hash"]),
            selector_version=cast(str, value["selector_version"]),
        )
    except (KeyError, TypeError, ValueError, InvalidEarningsCalendarSyncIdentity) as error:
        raise InvalidEarningsCalendarReplay(
            "Earnings calendar SyncRun scope is invalid."
        ) from error
    if canonical != value:
        raise InvalidEarningsCalendarReplay("Earnings calendar SyncRun scope is not canonical.")
    return canonical


def _validate_source_observations(source_run: SyncRun) -> None:
    observations = RawDataObservation.objects.filter(sync_run_id=source_run.pk)
    invalid = observations.exclude(raw_data_record__source_id=source_run.source_id)
    if invalid.exists():
        raise InvalidEarningsCalendarReplay(
            "Source SyncRun contains a raw observation from another DataSource."
        )
    observation_count = observations.count()
    if source_run.fetched_count != observation_count:
        raise EarningsCalendarReplayCountMismatch(
            "Source SyncRun fetch count does not match persisted raw observations."
        )
    if source_run.status == SyncRun.Status.SUCCEEDED and observation_count == 0:
        raise InvalidEarningsCalendarReplay(
            "A successful source run requires persisted raw evidence."
        )


def _load_ordered_evidence(
    source_run: SyncRun,
    *,
    observations: Iterable[RawDataObservation] | None = None,
) -> tuple[RawDataObservation, ...]:
    evidence_queryset = RawDataObservation.objects.select_related("raw_data_record").filter(
        sync_run_id=source_run.pk
    )
    persisted_ids = set(evidence_queryset.values_list("pk", flat=True))
    if observations is None:
        evidence = list(evidence_queryset)
    else:
        requested_ids = [
            _saved_uuid(observation, value_name="observations") for observation in observations
        ]
        if len(requested_ids) != len(set(requested_ids)):
            raise InvalidEarningsCalendarReplay("Replay observations must be unique.")
        if set(requested_ids) != persisted_ids:
            raise InvalidEarningsCalendarReplay(
                "Replay digest must cover every persisted source observation."
            )
        evidence = list(evidence_queryset.filter(pk__in=requested_ids))
    evidence.sort(
        key=lambda observation: (
            observation.raw_data_record.request_fingerprint,
            observation.raw_data_record.content_hash,
            observation.raw_data_record.source_url,
            observation.raw_data_record.payload_size_bytes,
        )
    )
    return tuple(evidence)


def _build_evidence_item(source_run: SyncRun, observation: RawDataObservation) -> dict[str, object]:
    if observation.sync_run_id != source_run.pk:
        raise InvalidEarningsCalendarReplay("Replay evidence must belong to the source SyncRun.")
    record = observation.raw_data_record
    if record.source_id != source_run.source_id:
        raise InvalidEarningsCalendarReplay(
            "Replay evidence DataSource does not match the source run."
        )
    payload_hash = hashlib.sha256(bytes(record.payload)).hexdigest()
    if payload_hash != record.content_hash:
        raise InvalidEarningsCalendarReplay(
            "Raw payload hash does not match persisted content_hash."
        )
    return {
        "request_fingerprint": record.request_fingerprint,
        "source_url": record.source_url,
        "content_hash": record.content_hash,
        "payload_size_bytes": record.payload_size_bytes,
        "http_status": record.http_status,
        "content_type": record.content_type,
        "encoding": record.encoding,
    }


def _load_sync_run(value: SyncRun | uuid.UUID) -> SyncRun:
    source_run_id = _saved_uuid(value, value_name="source_sync_run")
    try:
        return SyncRun.objects.select_related("source").get(pk=source_run_id)
    except SyncRun.DoesNotExist as error:
        raise InvalidEarningsCalendarReplay("source SyncRun does not exist.") from error


def _saved_uuid(value: object, *, value_name: str) -> uuid.UUID:
    if isinstance(value, (SyncRun, RawDataObservation)):
        if value._state.adding or value.pk is None:
            raise InvalidEarningsCalendarReplay(f"{value_name} must be saved before use.")
        return value.pk
    if isinstance(value, uuid.UUID):
        return value
    raise InvalidEarningsCalendarReplay(f"{value_name} must be a saved SyncRun or UUID.")


def _required_text(value: object, value_name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsCalendarReplay(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise InvalidEarningsCalendarReplay(f"{value_name} must be non-empty and bounded.")
    return normalized


def _required_digest(value: object, value_name: str) -> str:
    if not isinstance(value, str) or not _REPLAY_DIGEST_HEX_RE.fullmatch(value):
        raise InvalidEarningsCalendarReplay(f"{value_name} must be a SHA-256 hex digest.")
    return value


def _require_replay_run(sync_run: SyncRun) -> None:
    if sync_run.run_mode != SyncRun.RunMode.REPLAY:
        raise InvalidEarningsCalendarReplay("SyncRun is not an offline replay run.")
    if sync_run.replay_source_sync_run_id is None:
        raise InvalidEarningsCalendarReplay("Replay SyncRun has no source lineage.")
    if not sync_run.replay_contract_version or not _REPLAY_DIGEST_HEX_RE.fullmatch(
        sync_run.replay_input_digest
    ):
        raise InvalidEarningsCalendarReplay("Replay SyncRun metadata is incomplete.")
    _require_provider_version(sync_run)


def _require_ingestion_source(source_run: SyncRun) -> None:
    if source_run.run_mode != SyncRun.RunMode.INGESTION:
        raise InvalidEarningsCalendarReplay("A replay cannot use another replay as its source.")
    if source_run.replay_source_sync_run_id is not None:
        raise InvalidEarningsCalendarReplay(
            "An ingestion source cannot already have replay lineage."
        )


def _require_provider_version(source_run: SyncRun) -> str:
    if not isinstance(source_run.provider_version, str) or not source_run.provider_version.strip():
        raise ReplayProviderContextUnavailable(
            "Replay source SyncRun has no persisted provider version."
        )
    return source_run.provider_version.strip()
