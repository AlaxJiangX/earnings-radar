from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from django.db import transaction

from audit.models import DataSource, RawDataObservation, RawDataRecord, SyncRun
from audit.services import (
    mark_raw_data_parse_failed,
    mark_raw_data_parsed,
    mark_sync_run_failed,
    mark_sync_run_succeeded,
    record_raw_data_observation,
    start_sync_run_with_result,
    update_sync_run_counts,
)
from indexes.constituents import ALLOWED_INDEX_CODES, IndexConstituentSnapshot
from providers.base import Provider
from providers.types import ProviderCapability, ProviderRequest

INDEX_SNAPSHOT_JOB_TYPE = "indexes.constituent_snapshot"


class IndexSnapshotIngestionError(RuntimeError):
    pass


class InvalidIndexSnapshotSource(IndexSnapshotIngestionError):
    pass


class InvalidIndexSnapshotConfiguration(IndexSnapshotIngestionError):
    pass


class IndexSnapshotSyncAlreadyRunning(IndexSnapshotIngestionError):
    pass


class IndexSnapshotPreviousRunFailed(IndexSnapshotIngestionError):
    pass


class IndexSnapshotIngestionIntegrityError(IndexSnapshotIngestionError):
    pass


class IndexSnapshotParseError(IndexSnapshotIngestionError):
    pass


class UnsafeIndexSnapshot(IndexSnapshotIngestionError):
    pass


class IndexSnapshotParser(Protocol):
    def __call__(
        self,
        raw_content: bytes,
        *,
        expected_index_code: str,
    ) -> IndexConstituentSnapshot: ...


@dataclass(frozen=True, slots=True)
class IndexSnapshotIngestionResult:
    sync_run: SyncRun
    raw_data_record: RawDataRecord
    snapshot: IndexConstituentSnapshot
    run_created: bool
    raw_record_created: bool
    observation_created: bool


def ingest_index_constituent_snapshot(
    *,
    source: DataSource,
    provider: Provider,
    request: ProviderRequest,
    parser: IndexSnapshotParser,
    idempotency_key: str,
    parser_version: str,
    code_version: str = "",
) -> IndexSnapshotIngestionResult:
    """Fetch, persist, then parse one offline-safe index snapshot.

    This orchestration boundary deliberately stops before SecurityListing
    reconciliation or IndexMembership writes.  A source-specific parser is
    required so a future live Provider can preserve its upstream raw bytes
    before normalization.
    """
    normalized_parser_version = _normalize_version(
        parser_version,
        value_name="parser_version",
        required=True,
    )
    normalized_code_version = _normalize_version(
        code_version,
        value_name="code_version",
        required=False,
    )
    current_source = _validate_source(source=source, provider=provider)
    if request.capability is not ProviderCapability.INDEX_CONSTITUENTS:
        raise InvalidIndexSnapshotSource(
            "Index snapshot ingestion requires the index_constituents capability."
        )
    index_code = _request_index_code(request)

    start_result = start_sync_run_with_result(
        job_type=INDEX_SNAPSHOT_JOB_TYPE,
        source=current_source,
        scope=request.scope,
        idempotency_key=idempotency_key,
        code_version=normalized_code_version,
        parser_version=normalized_parser_version,
        started_at=request.request_started_at,
    )
    sync_run = start_result.sync_run
    if not start_result.created:
        return _replay_completed_run(
            sync_run=sync_run,
            request=request,
            parser=parser,
            parser_version=normalized_parser_version,
            code_version=normalized_code_version,
            index_code=index_code,
        )

    try:
        provider_result = provider.fetch(request)
        with transaction.atomic():
            ingest_result = record_raw_data_observation(
                sync_run=sync_run,
                source_url=provider_result.source_url,
                payload=provider_result.raw_content,
                request_method=provider_result.request_method,
                request_identity=provider_result.request_identity,
                fetched_at=provider_result.fetched_at,
                observed_at=provider_result.fetched_at,
                http_status=provider_result.http_status,
                content_type=provider_result.content_type,
                encoding="",
            )
            raw_data_record = ingest_result.record
            update_sync_run_counts(sync_run.pk, fetched_delta=1)

        if raw_data_record.request_fingerprint != provider_result.request_fingerprint:
            raise IndexSnapshotIngestionIntegrityError(
                "Persisted request fingerprint does not match the Provider result."
            )

        try:
            snapshot = parser(
                bytes(raw_data_record.payload),
                expected_index_code=index_code,
            )
        except Exception:
            parse_error = IndexSnapshotParseError("Index snapshot parsing failed.")
            with transaction.atomic():
                mark_raw_data_parse_failed(
                    raw_data_record.pk,
                    parser_version=normalized_parser_version,
                    parse_error="Index snapshot parsing failed.",
                )
                _fail_running_sync_run(sync_run=sync_run, error=parse_error)
            raise parse_error from None

        if not snapshot.entries:
            empty_error = UnsafeIndexSnapshot("Index snapshot is empty; domain writes are blocked.")
            with transaction.atomic():
                mark_raw_data_parsed(
                    raw_data_record.pk,
                    parser_version=normalized_parser_version,
                )
                _fail_running_sync_run(sync_run=sync_run, error=empty_error)
            raise empty_error

        with transaction.atomic():
            mark_raw_data_parsed(
                raw_data_record.pk,
                parser_version=normalized_parser_version,
            )
            update_sync_run_counts(
                sync_run.pk,
                created_delta=int(ingest_result.record_created),
                skipped_delta=int(not ingest_result.record_created),
            )
            finished_run = mark_sync_run_succeeded(sync_run.pk)
        return IndexSnapshotIngestionResult(
            sync_run=finished_run,
            raw_data_record=raw_data_record,
            snapshot=snapshot,
            run_created=True,
            raw_record_created=ingest_result.record_created,
            observation_created=ingest_result.observation_created,
        )
    except Exception as error:
        _fail_running_sync_run(sync_run=sync_run, error=error)
        raise


def _validate_source(*, source: DataSource, provider: Provider) -> DataSource:
    if source._state.adding or source.pk is None:
        raise InvalidIndexSnapshotSource("DataSource must be saved before use.")
    try:
        current_source = DataSource.objects.get(pk=source.pk)
    except DataSource.DoesNotExist as error:
        raise InvalidIndexSnapshotSource("DataSource no longer exists.") from error
    if current_source.source_type != DataSource.SourceType.INDEX:
        raise InvalidIndexSnapshotSource("DataSource must use the index source type.")
    if not current_source.is_enabled:
        raise InvalidIndexSnapshotSource("DataSource must be enabled.")
    provider_key = getattr(provider, "provider_key", "")
    if not provider_key or current_source.provider_adapter != provider_key:
        raise InvalidIndexSnapshotSource(
            "DataSource provider_adapter does not match the Provider identity."
        )
    return current_source


def _normalize_version(value: str, *, value_name: str, required: bool) -> str:
    normalized = value.strip()
    if required and not normalized:
        raise InvalidIndexSnapshotConfiguration(f"{value_name} must not be empty.")
    if len(normalized) > 100:
        raise InvalidIndexSnapshotConfiguration(
            f"{value_name} must contain at most 100 characters."
        )
    return normalized


def _request_index_code(request: ProviderRequest) -> str:
    raw_code = dict(request.scope).get("index_code")
    if not isinstance(raw_code, str) or not raw_code.strip():
        raise InvalidIndexSnapshotSource("Index snapshot scope requires a non-empty index_code.")
    index_code = raw_code.strip().upper()
    if index_code not in ALLOWED_INDEX_CODES:
        raise InvalidIndexSnapshotSource("Index snapshot scope uses an unsupported index_code.")
    return index_code


def _replay_completed_run(
    *,
    sync_run: SyncRun,
    request: ProviderRequest,
    parser: IndexSnapshotParser,
    parser_version: str,
    code_version: str,
    index_code: str,
) -> IndexSnapshotIngestionResult:
    if sync_run.status == SyncRun.Status.RUNNING:
        raise IndexSnapshotSyncAlreadyRunning(
            "An index snapshot sync with this idempotency key is already running."
        )
    if sync_run.status != SyncRun.Status.SUCCEEDED:
        raise IndexSnapshotPreviousRunFailed(
            "The existing index snapshot sync did not succeed; use a new retry key."
        )
    if (
        dict(sync_run.scope) != dict(request.scope)
        or sync_run.parser_version != parser_version
        or sync_run.code_version != code_version
    ):
        raise IndexSnapshotIngestionIntegrityError(
            "The existing SyncRun context does not match this replay."
        )

    observations = list(
        RawDataObservation.objects.select_related("raw_data_record").filter(sync_run=sync_run)
    )
    if len(observations) != 1:
        raise IndexSnapshotIngestionIntegrityError(
            "A completed index snapshot SyncRun must have exactly one raw observation."
        )
    raw_data_record = observations[0].raw_data_record
    if hashlib.sha256(bytes(raw_data_record.payload)).hexdigest() != raw_data_record.content_hash:
        raise IndexSnapshotIngestionIntegrityError(
            "The completed SyncRun raw record has an inconsistent content hash."
        )
    try:
        snapshot = parser(
            bytes(raw_data_record.payload),
            expected_index_code=index_code,
        )
    except Exception:
        raise IndexSnapshotIngestionIntegrityError(
            "The completed SyncRun raw record no longer parses deterministically."
        ) from None
    if not snapshot.entries:
        raise IndexSnapshotIngestionIntegrityError(
            "The completed SyncRun unexpectedly contains an empty snapshot."
        )
    return IndexSnapshotIngestionResult(
        sync_run=sync_run,
        raw_data_record=raw_data_record,
        snapshot=snapshot,
        run_created=False,
        raw_record_created=False,
        observation_created=False,
    )


def _fail_running_sync_run(*, sync_run: SyncRun, error: Exception) -> None:
    with transaction.atomic():
        current_run = SyncRun.objects.select_for_update().get(pk=sync_run.pk)
        if current_run.status != SyncRun.Status.RUNNING:
            return
        update_sync_run_counts(current_run.pk, failed_delta=1)
        mark_sync_run_failed(
            current_run.pk,
            error_summary=f"Index snapshot ingestion failed ({type(error).__name__}).",
        )
