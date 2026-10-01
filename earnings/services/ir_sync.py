"""IR confirmation SyncRun scope, idempotency and zero-network replay.

Stage 4.5B is fixture-first: this module only orchestrates a Fake/fixture
provider and persisted raw bytes.  It never implements a real IR transport and
never re-evaluates the current allowlist for replay (ADR-022 §2 / §10.2).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from audit.models import (
    DataSource,
    RawDataObservation,
    SyncRun,
)
from audit.services import (
    SyncRunStartContextMismatch,
    SyncRunStartResult,
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    start_sync_run_with_result,
    update_sync_run_counts,
)
from audit.services.raw_data import record_replay_raw_data_observation
from companies.models import Company
from earnings.ir_parsing import InvestorRelationsParser
from earnings.services.ir_confirmation import (
    InvestorRelationsEvaluationResult,
    evaluate_investor_relations_observation,
)
from earnings.services.ir_ingestion import (
    InvestorRelationsIngestionResult,
    ingest_investor_relations_payload,
    process_persisted_investor_relations_payload,
)
from providers.base import Provider
from providers.types import ProviderCapability, ProviderRequest, validate_provider_key

IR_CONFIRMATION_JOB_TYPE = "earnings.ir_confirmation"
IR_SCOPE_VERSION = "ir-confirmation-scope-v1"
IR_REPLAY_CONTRACT_VERSION = "ir-confirmation-replay-v1"
IR_REQUEST_IDEMPOTENCY_PREFIX = "ir-confirmation-request:v1:"
IR_REPLAY_IDEMPOTENCY_PREFIX = "ir-confirmation-replay:v1:"
IR_FIXTURE_SOURCE_URL_BASE = "https://ir.example.test"
IR_MAX_SCOPE_COMPANIES = 50

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_TERMINAL_SOURCE_STATUSES = frozenset(
    {SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL, SyncRun.Status.FAILED}
)


class InvestorRelationsSyncError(ValueError):
    """Base class for invalid IR sync identity or orchestration input."""


class InvalidInvestorRelationsSyncIdentity(InvestorRelationsSyncError):
    pass


class InvestorRelationsRunBusy(RuntimeError):
    """Another caller already owns the same running IR identity."""

    def __init__(self, message: str, *, sync_run: SyncRun) -> None:
        super().__init__(message)
        self.sync_run = sync_run


class InvestorRelationsRunContextMismatch(RuntimeError):
    """An existing IR identity has immutable context different from the request."""

    def __init__(self, message: str, *, sync_run: SyncRun) -> None:
        super().__init__(message)
        self.sync_run = sync_run


class InvestorRelationsReplayError(InvestorRelationsSyncError):
    """Persisted raw replay cannot proceed under the frozen contract."""


@dataclass(frozen=True, slots=True)
class InvestorRelationsSyncResult:
    sync_run: SyncRun
    fetched_payloads: int
    observations_created: int
    observations_reused: int
    decisions_created: int
    item_failures: int
    evaluation_failures: int
    ingestions: tuple[InvestorRelationsIngestionResult, ...]
    evaluations: tuple[InvestorRelationsEvaluationResult, ...]


@dataclass(frozen=True, slots=True)
class InvestorRelationsReplayResult:
    sync_run: SyncRun
    source_sync_run: SyncRun
    replay_input_digest: str
    replayed_payloads: int
    observations_created: int
    observations_reused: int
    decisions_created: int
    item_failures: int
    reused_terminal_run: bool
    evaluations: tuple[InvestorRelationsEvaluationResult, ...]


def build_investor_relations_sync_scope(
    *,
    provider_key: str,
    company_ids: Sequence[uuid.UUID | str],
    source_keys: Sequence[str],
    window_kind: str = "ingestion",
) -> dict[str, object]:
    """Build the canonical frozen scope for one IR confirmation run."""

    normalized_provider_key = validate_provider_key(provider_key)
    normalized_company_ids = _normalize_company_ids(company_ids)
    normalized_source_keys = _normalize_source_keys(source_keys)
    normalized_window_kind = _normalize_window_kind(window_kind)
    scope: dict[str, object] = {
        "capability": ProviderCapability.INVESTOR_RELATIONS.value,
        "provider_key": normalized_provider_key,
        "scope_version": IR_SCOPE_VERSION,
        "company_ids": list(normalized_company_ids),
        "source_keys": list(normalized_source_keys),
        "scope_digest": _build_scope_digest(
            scope_version=IR_SCOPE_VERSION,
            company_ids=normalized_company_ids,
            source_keys=normalized_source_keys,
        ),
        "window_kind": normalized_window_kind,
    }
    return scope


def build_investor_relations_idempotency_key(
    *,
    source_key: str,
    provider_key: str,
    scope_digest: str,
    request_id: str,
) -> str:
    """Build the deterministic identity of one explicit IR request."""

    normalized_source_key = _normalize_source_key(source_key)
    normalized_provider_key = validate_provider_key(provider_key)
    normalized_digest = _normalize_digest(scope_digest, value_name="scope_digest")
    normalized_request_id = _normalize_request_id(request_id)
    return IR_REQUEST_IDEMPOTENCY_PREFIX + _canonical_sha256(
        {
            "provider_key": normalized_provider_key,
            "request_id": normalized_request_id,
            "scope_digest": normalized_digest,
            "source_key": normalized_source_key,
        }
    )


def start_investor_relations_sync_run(
    *,
    source: DataSource,
    provider_key: str,
    company_ids: Sequence[uuid.UUID | str],
    source_keys: Sequence[str],
    request_id: str,
    provider_version: str,
    parser_version: str = "",
    code_version: str = "",
    started_at: datetime | None = None,
) -> SyncRunStartResult:
    """Create or safely inspect one explicit fixture-first IR run."""

    normalized_provider_key = validate_provider_key(provider_key)
    current_source = _load_ir_source(source, provider_key=normalized_provider_key)
    scope = build_investor_relations_sync_scope(
        provider_key=normalized_provider_key,
        company_ids=company_ids,
        source_keys=source_keys,
    )
    _validate_scope_companies(scope)
    idempotency_key = build_investor_relations_idempotency_key(
        source_key=current_source.key,
        provider_key=normalized_provider_key,
        scope_digest=_normalize_digest(scope["scope_digest"], value_name="scope_digest"),
        request_id=request_id,
    )
    normalized_provider_version = _require_text(
        provider_version,
        value_name="provider_version",
        maximum_length=100,
    )
    try:
        result = start_sync_run_with_result(
            job_type=IR_CONFIRMATION_JOB_TYPE,
            source=current_source,
            scope=scope,
            idempotency_key=idempotency_key,
            code_version=code_version,
            parser_version=parser_version,
            started_at=started_at,
            provider_version=normalized_provider_version,
            require_provider_version=True,
        )
    except SyncRunStartContextMismatch as error:
        existing = SyncRun.objects.filter(
            job_type=IR_CONFIRMATION_JOB_TYPE,
            source=current_source,
            idempotency_key=idempotency_key,
        ).first()
        if existing is None:
            raise
        raise InvestorRelationsRunContextMismatch(str(error), sync_run=existing) from error
    if result.created:
        return result
    if result.sync_run.status == SyncRun.Status.RUNNING:
        raise InvestorRelationsRunBusy(
            "An IR confirmation run with this identity is already running.",
            sync_run=result.sync_run,
        )
    return result


def execute_investor_relations_sync(
    *,
    sync_run: SyncRun,
    provider: Provider,
    parser: InvestorRelationsParser,
    request_started_at: datetime | None = None,
) -> InvestorRelationsSyncResult:
    """Fetch fixture payloads, persist raw lineage and evaluate IR observations."""

    current_run = _load_running_ir_run(sync_run)
    if current_run.run_mode != SyncRun.RunMode.INGESTION:
        raise InvestorRelationsSyncError("Live IR sync requires an ingestion SyncRun.")
    scope = _validate_persisted_ir_scope(current_run.scope, window_kind="ingestion")
    if provider.provider_key != scope["provider_key"]:
        raise InvestorRelationsSyncError("provider_key does not match the frozen SyncRun scope.")
    if provider.provider_version != current_run.provider_version:
        raise InvestorRelationsSyncError(
            "provider_version does not match the persisted SyncRun provider context."
        )
    if parser.parser_version != current_run.parser_version and current_run.parser_version:
        raise InvestorRelationsSyncError(
            "parser_version does not match the persisted SyncRun parser context."
        )

    ingestions: list[InvestorRelationsIngestionResult] = []
    evaluations: list[InvestorRelationsEvaluationResult] = []
    fetched = 0
    observations_created = 0
    observations_reused = 0
    decisions_created = 0
    item_failures = 0
    evaluation_failures = 0
    last_error: Exception | None = None
    started_at = request_started_at or timezone.now()

    for company_id, source_key in _scope_entries(scope):
        request = ProviderRequest(
            capability=ProviderCapability.INVESTOR_RELATIONS,
            scope={
                "company_id": company_id,
                "scope_digest": scope["scope_digest"],
                "source_key": source_key,
            },
            request_started_at=started_at,
            source_url=f"{IR_FIXTURE_SOURCE_URL_BASE}/{source_key}/{company_id}",
            request_identity={"company_id": company_id, "source_key": source_key},
        )
        try:
            descriptor = provider.describe_request(request)
            result = provider.fetch(request)
        except Exception as error:
            last_error = error
            evaluation_failures += 1
            continue
        try:
            ingestion = ingest_investor_relations_payload(
                sync_run=current_run,
                parser=parser,
                raw_content=result.raw_content,
                source_key=source_key,
                provider_key=provider.provider_key,
                provider_version=provider.provider_version,
                source_url=result.source_url,
                fetched_at=result.fetched_at,
                request_identity=result.request_identity,
                http_status=result.http_status,
                content_type=result.content_type,
                request_descriptor=descriptor,
            )
        except Exception as error:
            last_error = error
            evaluation_failures += 1
            continue
        fetched += 1
        item_failures += len(ingestion.item_failures)
        observations_created += ingestion.observations_created
        observations_reused += ingestion.observations_reused
        ingestions.append(ingestion)
        for observation in ingestion.observations:
            try:
                evaluation = evaluate_investor_relations_observation(
                    observation=observation,
                    sync_run=current_run,
                )
            except Exception as error:
                last_error = error
                evaluation_failures += 1
                continue
            evaluations.append(evaluation)
            if evaluation.decision_created:
                decisions_created += 1

    update_sync_run_counts(
        current_run.pk,
        fetched_delta=fetched,
        created_delta=observations_created,
        updated_delta=decisions_created,
        skipped_delta=observations_reused,
        failed_delta=item_failures + evaluation_failures,
    )
    finished = _finalize_run(
        current_run.pk,
        fetched=fetched,
        created=observations_created + decisions_created,
        failed=item_failures + evaluation_failures,
    )
    if fetched == 0 and last_error is not None:
        raise InvestorRelationsSyncError("Every IR scope entry failed.") from last_error
    return InvestorRelationsSyncResult(
        sync_run=finished,
        fetched_payloads=fetched,
        observations_created=observations_created,
        observations_reused=observations_reused,
        decisions_created=decisions_created,
        item_failures=item_failures,
        evaluation_failures=evaluation_failures,
        ingestions=tuple(ingestions),
        evaluations=tuple(evaluations),
    )


def start_investor_relations_replay_sync_run(
    *,
    source_run: SyncRun,
    parser_version: str,
    started_at: datetime | None = None,
) -> tuple[SyncRunStartResult, str]:
    """Start (or reuse) a zero-network replay of a terminal IR ingestion run."""

    source = _validate_replay_source(source_run)
    digest = build_investor_relations_replay_input_digest(source)
    scope = _replay_scope(source.scope)
    normalized_parser_version = _require_text(
        parser_version,
        value_name="parser_version",
        maximum_length=100,
    )
    idempotency_key = IR_REPLAY_IDEMPOTENCY_PREFIX + _canonical_sha256(
        {
            "parser_version": normalized_parser_version,
            "replay_contract_version": IR_REPLAY_CONTRACT_VERSION,
            "replay_input_digest": digest,
            "source_sync_run_id": str(source.pk),
        }
    )
    try:
        result = start_sync_run_with_result(
            job_type=IR_CONFIRMATION_JOB_TYPE,
            source=source.source,
            scope=scope,
            idempotency_key=idempotency_key,
            parser_version=normalized_parser_version,
            started_at=started_at,
            run_mode=SyncRun.RunMode.REPLAY,
            replay_source_sync_run=source,
            replay_contract_version=IR_REPLAY_CONTRACT_VERSION,
            replay_input_digest=digest,
            provider_version=source.provider_version,
            require_provider_version=True,
        )
    except SyncRunStartContextMismatch as error:
        existing = SyncRun.objects.filter(
            job_type=IR_CONFIRMATION_JOB_TYPE,
            source=source.source,
            idempotency_key=idempotency_key,
        ).first()
        if existing is None:
            raise
        raise InvestorRelationsRunContextMismatch(str(error), sync_run=existing) from error
    return result, digest


def build_investor_relations_replay_input_digest(source_run: SyncRun) -> str:
    """Build the deterministic manifest digest of the source run's raw evidence."""

    source = _validate_replay_source(source_run)
    observations = list(
        RawDataObservation.objects.select_related("raw_data_record").filter(sync_run_id=source.pk)
    )
    if source.status == SyncRun.Status.SUCCEEDED and source.fetched_count != len(observations):
        raise InvestorRelationsReplayError(
            "Source fetch count does not match persisted raw observations."
        )
    manifest: list[dict[str, object]] = []
    for observation in sorted(
        observations,
        key=lambda item: (
            item.raw_data_record.request_fingerprint,
            item.raw_data_record.content_hash,
            item.raw_data_record.source_url,
            item.raw_data_record.payload_size_bytes,
        ),
    ):
        record = observation.raw_data_record
        if record.source_id != source.source_id:
            raise InvestorRelationsReplayError(
                "Source raw evidence belongs to a different DataSource."
            )
        if record.content_hash != hashlib.sha256(bytes(record.payload)).hexdigest():
            raise InvestorRelationsReplayError(
                "Source raw evidence content hash does not match persisted bytes."
            )
        manifest.append(
            {
                "content_hash": record.content_hash,
                "content_type": record.content_type,
                "encoding": record.encoding,
                "http_status": record.http_status,
                "payload_size_bytes": record.payload_size_bytes,
                "request_fingerprint": record.request_fingerprint,
                "source_url": record.source_url,
            }
        )
    return _canonical_sha256({"raw_manifest": manifest})


def execute_investor_relations_replay(
    *,
    source_run: SyncRun,
    parser: InvestorRelationsParser,
) -> InvestorRelationsReplayResult:
    """Replay persisted IR raw payloads without any provider/network call."""

    start_result, digest = start_investor_relations_replay_sync_run(
        source_run=source_run,
        parser_version=parser.parser_version,
    )
    replay_run = start_result.sync_run
    if not start_result.created:
        if replay_run.status != SyncRun.Status.RUNNING:
            return InvestorRelationsReplayResult(
                sync_run=replay_run,
                source_sync_run=source_run,
                replay_input_digest=digest,
                replayed_payloads=0,
                observations_created=0,
                observations_reused=0,
                decisions_created=0,
                item_failures=0,
                reused_terminal_run=True,
                evaluations=(),
            )
        raise InvestorRelationsRunBusy(
            "An IR replay with this identity is already running.",
            sync_run=replay_run,
        )

    source = _validate_replay_source(source_run)
    raw_observations = list(
        RawDataObservation.objects.select_related("raw_data_record")
        .filter(sync_run_id=source.pk)
        .order_by(
            "raw_data_record__request_fingerprint",
            "raw_data_record__content_hash",
            "raw_data_record__source_url",
            "raw_data_record__payload_size_bytes",
        )
    )
    evaluations: list[InvestorRelationsEvaluationResult] = []
    observations_created = 0
    observations_reused = 0
    decisions_created = 0
    item_failures = 0
    failures = 0

    for source_observation in raw_observations:
        raw_record = source_observation.raw_data_record
        replay_observation = record_replay_raw_data_observation(
            sync_run=replay_run,
            raw_data_record=raw_record,
        ).observation
        try:
            (
                _attempt,
                _source_key,
                observations,
                parse_failures,
                created_count,
                reused_count,
            ) = process_persisted_investor_relations_payload(
                sync_run=replay_run,
                parser=parser,
                raw_record=raw_record,
                raw_observation=replay_observation,
                source_key=None,
                provider_key=source.source.provider_adapter,
                provider_version=source.provider_version or "",
            )
        except Exception:
            failures += 1
            continue
        observations_created += created_count
        observations_reused += reused_count
        item_failures += len(parse_failures)
        for observation in observations:
            try:
                evaluation = evaluate_investor_relations_observation(
                    observation=observation,
                    sync_run=replay_run,
                )
            except Exception:
                failures += 1
                continue
            evaluations.append(evaluation)
            if evaluation.decision_created:
                decisions_created += 1

    with transaction.atomic():
        current = SyncRun.objects.select_for_update().get(pk=replay_run.pk)
        current.replayed_count = RawDataObservation.objects.filter(sync_run_id=current.pk).count()
        current.created_count = observations_created
        current.updated_count = decisions_created
        current.skipped_count = observations_reused
        current.failed_count = item_failures + failures
        current.heartbeat_at = timezone.now()
        current.save(
            update_fields=(
                "replayed_count",
                "created_count",
                "updated_count",
                "skipped_count",
                "failed_count",
                "heartbeat_at",
            )
        )
    total_failed = item_failures + failures
    if total_failed == 0 and source.status == SyncRun.Status.SUCCEEDED:
        finished = mark_sync_run_succeeded(replay_run.pk)
    elif observations_created or observations_reused or evaluations:
        finished = mark_sync_run_partial(
            replay_run.pk,
            error_summary="IR replay completed with failed items.",
        )
    else:
        finished = mark_sync_run_failed(
            replay_run.pk,
            error_summary="IR replay produced no normalized observations.",
        )
    return InvestorRelationsReplayResult(
        sync_run=finished,
        source_sync_run=source,
        replay_input_digest=digest,
        replayed_payloads=len(raw_observations),
        observations_created=observations_created,
        observations_reused=observations_reused,
        decisions_created=decisions_created,
        item_failures=item_failures,
        reused_terminal_run=False,
        evaluations=tuple(evaluations),
    )


def _load_ir_source(source: DataSource, *, provider_key: str) -> DataSource:
    if source._state.adding or source.pk is None:
        raise InvestorRelationsSyncError("source must be saved before use.")
    try:
        current = DataSource.objects.get(pk=source.pk)
    except DataSource.DoesNotExist as error:
        raise InvestorRelationsSyncError("source no longer exists.") from error
    if current.source_type != DataSource.SourceType.INVESTOR_RELATIONS:
        raise InvestorRelationsSyncError("source must use the investor_relations source type.")
    if current.provider_adapter != provider_key:
        raise InvestorRelationsSyncError("provider_key must match the source provider_adapter.")
    if not current.is_enabled:
        raise InvestorRelationsSyncError("source must be enabled.")
    return current


def _validate_scope_companies(scope: Mapping[str, object]) -> None:
    raw_ids = scope.get("company_ids")
    if not isinstance(raw_ids, list):
        raise InvestorRelationsSyncError("scope company_ids must be a list.")
    company_ids = [uuid.UUID(str(value)) for value in raw_ids]
    companies = Company.objects.in_bulk(company_ids)
    for company_id in company_ids:
        company = companies.get(company_id)
        if company is None:
            raise InvestorRelationsSyncError(f"scope company {company_id} does not exist.")
        if not company.investor_relations_url.strip():
            raise InvestorRelationsSyncError(
                f"scope company {company_id} has no investor_relations_url."
            )


def _load_running_ir_run(sync_run: SyncRun) -> SyncRun:
    if sync_run._state.adding or sync_run.pk is None:
        raise InvestorRelationsSyncError("sync_run must be saved before use.")
    try:
        current = SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvestorRelationsSyncError("sync_run no longer exists.") from error
    if current.job_type != IR_CONFIRMATION_JOB_TYPE:
        raise InvestorRelationsSyncError("sync_run must use the IR confirmation job type.")
    if current.status != SyncRun.Status.RUNNING:
        raise InvestorRelationsSyncError("sync_run must be running.")
    if current.source.source_type != DataSource.SourceType.INVESTOR_RELATIONS:
        raise InvestorRelationsSyncError(
            "sync_run source must use the investor_relations source type."
        )
    if not current.provider_version:
        raise InvestorRelationsSyncError("sync_run must persist provider_version provenance.")
    return current


def _validate_persisted_ir_scope(
    scope: object,
    *,
    window_kind: str,
) -> dict[str, object]:
    if not isinstance(scope, dict):
        raise InvestorRelationsSyncError("IR SyncRun scope must be a JSON object.")
    if scope.get("capability") != ProviderCapability.INVESTOR_RELATIONS.value:
        raise InvestorRelationsSyncError("IR SyncRun scope capability is invalid.")
    if scope.get("scope_version") != IR_SCOPE_VERSION:
        raise InvestorRelationsSyncError("IR SyncRun scope_version is invalid.")
    if scope.get("window_kind") != window_kind:
        raise InvestorRelationsSyncError("IR SyncRun window_kind is invalid.")
    raw_company_ids = scope.get("company_ids")
    raw_source_keys = scope.get("source_keys")
    if not isinstance(raw_company_ids, list) or not isinstance(raw_source_keys, list):
        raise InvestorRelationsSyncError(
            "IR SyncRun scope must contain company_ids and source_keys lists."
        )
    company_ids = _normalize_company_ids(raw_company_ids)
    source_keys = _normalize_source_keys(raw_source_keys)
    expected_digest = _build_scope_digest(
        scope_version=IR_SCOPE_VERSION,
        company_ids=company_ids,
        source_keys=source_keys,
    )
    persisted_digest = _normalize_digest(scope.get("scope_digest"), value_name="scope_digest")
    if persisted_digest != expected_digest:
        raise InvestorRelationsSyncError("IR SyncRun scope digest does not match its facts.")
    normalized = dict(scope)
    normalized["company_ids"] = list(company_ids)
    normalized["source_keys"] = list(source_keys)
    normalized["scope_digest"] = expected_digest
    return normalized


def _replay_scope(scope: object) -> dict[str, object]:
    normalized = _validate_persisted_ir_scope(scope, window_kind="ingestion")
    normalized["window_kind"] = "replay"
    return normalized


def _validate_replay_source(source_run: SyncRun) -> SyncRun:
    if source_run._state.adding or source_run.pk is None:
        raise InvestorRelationsReplayError("source_run must be saved before replay.")
    try:
        source = SyncRun.objects.select_related("source").get(pk=source_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvestorRelationsReplayError("source_run no longer exists.") from error
    if source.job_type != IR_CONFIRMATION_JOB_TYPE:
        raise InvestorRelationsReplayError("source_run has the wrong job type.")
    if source.run_mode != SyncRun.RunMode.INGESTION:
        raise InvestorRelationsReplayError("source_run must be an ingestion run.")
    if source.status not in _TERMINAL_SOURCE_STATUSES:
        raise InvestorRelationsReplayError("source_run must be terminal before replay.")
    if source.source.source_type != DataSource.SourceType.INVESTOR_RELATIONS:
        raise InvestorRelationsReplayError(
            "source_run must use the investor_relations source type."
        )
    if not source.provider_version:
        raise InvestorRelationsReplayError(
            "source_run has no persisted provider_version provenance."
        )
    _validate_persisted_ir_scope(source.scope, window_kind="ingestion")
    return source


def _scope_entries(scope: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    raw_company_ids = scope.get("company_ids")
    raw_source_keys = scope.get("source_keys")
    if not isinstance(raw_company_ids, list) or not isinstance(raw_source_keys, list):
        raise InvestorRelationsSyncError("IR SyncRun scope is missing frozen entries.")
    company_ids = [str(value) for value in raw_company_ids]
    source_keys = [str(value) for value in raw_source_keys]
    return tuple(
        (company_id, source_key) for company_id in company_ids for source_key in source_keys
    )


def _normalize_company_ids(
    values: Sequence[uuid.UUID | str],
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InvalidInvestorRelationsSyncIdentity("company_ids must be a sequence.")
    normalized: set[str] = set()
    for value in values:
        try:
            company_id = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise InvalidInvestorRelationsSyncIdentity(
                "company_ids must contain UUID values."
            ) from None
        normalized.add(str(company_id))
    if not normalized:
        raise InvalidInvestorRelationsSyncIdentity("company_ids must not be empty.")
    if len(normalized) > IR_MAX_SCOPE_COMPANIES:
        raise InvalidInvestorRelationsSyncIdentity(
            f"company_ids must contain at most {IR_MAX_SCOPE_COMPANIES} companies."
        )
    return tuple(sorted(normalized))


def _normalize_source_keys(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InvalidInvestorRelationsSyncIdentity("source_keys must be a sequence.")
    normalized = {_normalize_source_key(value) for value in values}
    if not normalized:
        raise InvalidInvestorRelationsSyncIdentity("source_keys must not be empty.")
    return tuple(sorted(normalized))


def _normalize_source_key(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsSyncIdentity("source_key must be a string.")
    normalized = value.strip()
    if not _SOURCE_KEY_RE.fullmatch(normalized):
        raise InvalidInvestorRelationsSyncIdentity(
            "source_key must be a stable 1-64 character source identifier."
        )
    return normalized


def _normalize_window_kind(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsSyncIdentity("window_kind must be a string.")
    normalized = value.strip().lower()
    if normalized not in ("ingestion", "replay"):
        raise InvalidInvestorRelationsSyncIdentity("window_kind must be ingestion or replay.")
    return normalized


def _normalize_digest(value: object, *, value_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_HEX_RE.fullmatch(value.strip()):
        raise InvalidInvestorRelationsSyncIdentity(
            f"{value_name} must be a lowercase SHA-256 digest."
        )
    return value.strip()


def _normalize_request_id(value: object) -> str:
    return _require_text(value, value_name="request_id", maximum_length=255)


def _require_text(value: object, *, value_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidInvestorRelationsSyncIdentity(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidInvestorRelationsSyncIdentity(f"{value_name} must not be empty.")
    if len(normalized) > maximum_length:
        raise InvalidInvestorRelationsSyncIdentity(
            f"{value_name} must contain at most {maximum_length} characters."
        )
    return normalized


def _build_scope_digest(
    *,
    scope_version: str,
    company_ids: tuple[str, ...],
    source_keys: tuple[str, ...],
) -> str:
    return _canonical_sha256(
        {
            "company_ids": list(company_ids),
            "scope_version": scope_version,
            "source_keys": list(source_keys),
        }
    )


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    serialized = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def _finalize_run(
    sync_run_id: uuid.UUID,
    *,
    fetched: int,
    created: int,
    failed: int,
) -> SyncRun:
    if failed == 0:
        return mark_sync_run_succeeded(sync_run_id)
    if fetched or created:
        return mark_sync_run_partial(
            sync_run_id,
            error_summary="IR confirmation completed with failed items.",
        )
    return mark_sync_run_failed(
        sync_run_id,
        error_summary="IR confirmation produced no usable payloads.",
    )
