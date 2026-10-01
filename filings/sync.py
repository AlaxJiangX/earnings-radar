"""Frozen-pool, raw-first SEC metadata synchronization."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from django.db import connection, transaction
from django.utils import timezone

from audit.models import DataSource, RawDataRecord, SyncRun
from audit.services import (
    mark_raw_data_parse_failed,
    mark_raw_data_parsed,
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    record_raw_data_observation,
    start_sync_run_with_result,
    update_sync_run_counts,
)
from companies.models import Company
from companies.services import normalize_cik
from earnings.services.monitoring_pool import resolve_monitoring_pool_snapshot_contract
from filings.parsing import PARSER_VERSION, SecMetadataError, parse_filing_index, parse_submissions
from filings.services import filing_is_complete, record_filing
from providers.base import Provider
from providers.sec_edgar import (
    SEC_PROVIDER_KEY,
    filing_index_request,
    submissions_request,
)
from providers.types import ProviderCapability, ProviderRequest

SEC_JOB_TYPE = "filings.sec_edgar"


class SecSyncError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SecSyncResult:
    sync_run: SyncRun
    run_created: bool


@contextmanager
def _sec_run_ownership() -> Iterator[None]:
    if connection.vendor != "postgresql":
        raise SecSyncError("SEC synchronization requires PostgreSQL.")
    digest = hashlib.sha256(f"sec-filings:v1:global:{SEC_JOB_TYPE}".encode()).digest()
    lock_key = int.from_bytes(digest[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_key])
        acquired = bool(cursor.fetchone()[0])
    if not acquired:
        raise SecSyncError("Another SEC synchronization owns the global SEC filing job.")
    owner_connection = connection.connection
    try:
        yield
    finally:
        if connection.connection is not owner_connection:
            raise SecSyncError("SEC synchronization lost its database lock connection.")
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_key])


def sync_sec_filings(
    *,
    source: DataSource,
    provider: Provider,
    pool_as_of: date,
    pool_selector_version: str,
    pool_hash: str,
    idempotency_key: str,
) -> SecSyncResult:
    """Fetch only Companies authorized by one persisted monitoring-pool snapshot."""

    if (
        provider.provider_key != SEC_PROVIDER_KEY
        or ProviderCapability.SEC_EDGAR not in provider.capabilities
    ):
        raise SecSyncError("SEC sync requires the approved SEC Provider.")
    current_source = DataSource.objects.get(pk=source.pk)
    if (
        current_source.source_type != DataSource.SourceType.SEC
        or current_source.provider_adapter != provider.provider_key
        or not current_source.is_enabled
        or not current_source.is_official
    ):
        raise SecSyncError("SEC DataSource configuration is invalid.")
    with _sec_run_ownership():
        pool = resolve_monitoring_pool_snapshot_contract(
            as_of=pool_as_of, selector_version=pool_selector_version, pool_hash=pool_hash
        )
        scope = {
            "monitoring_pool_as_of": pool.snapshot.as_of_date.isoformat(),
            "selector_version": pool.snapshot.selector_version,
            "monitoring_pool_hash": pool.snapshot.pool_hash,
        }
        start = start_sync_run_with_result(
            job_type=SEC_JOB_TYPE,
            source=current_source,
            scope=scope,
            idempotency_key=idempotency_key,
            parser_version=PARSER_VERSION,
            provider_version=provider.provider_version,
            require_provider_version=True,
        )
        if not start.created:
            if start.sync_run.status == SyncRun.Status.SUCCEEDED:
                return SecSyncResult(sync_run=start.sync_run, run_created=False)
            raise SecSyncError("Existing SEC run is not successful; retry with a new key.")

        run = start.sync_run
        failures = 0
        failure_details: list[str] = []
        processed = 0
        for member in pool.members:
            try:
                company = Company.objects.get(pk=member.company_id)
                cik = normalize_cik(company.cik)
                if cik is None:
                    update_sync_run_counts(run.pk, skipped_delta=1)
                    continue
                if company.cik != cik:
                    raise SecSyncError("Persisted Company CIK is not canonical.")
                request = submissions_request(cik=cik, started_at=timezone.now())
                raw = _fetch_raw(run=run, provider=provider, request=request)
                try:
                    filings = parse_submissions(bytes(raw.payload), cik=cik)
                except SecMetadataError:
                    mark_raw_data_parse_failed(
                        raw.pk,
                        parser_version=PARSER_VERSION,
                        parse_error="SEC submissions parse failed.",
                    )
                    raise
                mark_raw_data_parsed(raw.pk, parser_version=PARSER_VERSION)
                processed += 1
                for filing_metadata in filings:
                    try:
                        if filing_is_complete(metadata=filing_metadata, company_id=company.pk):
                            update_sync_run_counts(run.pk, skipped_delta=1)
                            continue
                        index_request = filing_index_request(
                            cik=cik,
                            accession_number=filing_metadata.accession_number,
                            started_at=timezone.now(),
                        )
                        index_raw = _fetch_raw(run=run, provider=provider, request=index_request)
                        try:
                            documents = parse_filing_index(
                                bytes(index_raw.payload),
                                cik=cik,
                                accession_number=filing_metadata.accession_number,
                                primary_document=filing_metadata.primary_document,
                            )
                        except SecMetadataError:
                            mark_raw_data_parse_failed(
                                index_raw.pk,
                                parser_version=PARSER_VERSION,
                                parse_error="SEC filing directory parse failed.",
                            )
                            raise
                        mark_raw_data_parsed(index_raw.pk, parser_version=PARSER_VERSION)
                        written = record_filing(
                            company=company,
                            metadata=filing_metadata,
                            documents=documents,
                            submissions_raw=raw,
                            directory_raw=index_raw,
                            sync_run=run,
                        )
                        update_sync_run_counts(
                            run.pk,
                            created_delta=int(written.filing_created) + written.documents_created,
                            skipped_delta=int(not written.filing_created),
                        )
                    except Exception as error:
                        failures += 1
                        if len(failure_details) < 10:
                            failure_details.append(
                                f"company={company.pk}, "
                                f"accession={filing_metadata.accession_number}: "
                                f"{type(error).__name__}"
                            )
                        update_sync_run_counts(run.pk, failed_delta=1)
            except Exception as error:
                failures += 1
                if len(failure_details) < 10:
                    failure_details.append(f"company={member.company_id}: {type(error).__name__}")
                update_sync_run_counts(run.pk, failed_delta=1)
        if failures:
            summary = (
                f"SEC metadata processing failed for {failures} item(s): "
                + "; ".join(failure_details)
            )[:2000]
            if processed:
                finished = mark_sync_run_partial(run.pk, error_summary=summary)
            else:
                finished = mark_sync_run_failed(run.pk, error_summary=summary)
        else:
            finished = mark_sync_run_succeeded(run.pk)
        return SecSyncResult(sync_run=finished, run_created=True)


def _fetch_raw(*, run: SyncRun, provider: Provider, request: ProviderRequest) -> RawDataRecord:
    descriptor = provider.describe_request(request)
    result = provider.fetch(request)
    with transaction.atomic():
        ingested = record_raw_data_observation(
            sync_run=run,
            source_url=descriptor.source_url.stored,
            payload=result.raw_content,
            request_method=descriptor.method,
            request_identity=descriptor.identity,
            fetched_at=result.fetched_at,
            observed_at=result.fetched_at,
            http_status=result.http_status,
            content_type=result.content_type,
            request_descriptor=descriptor,
        )
        update_sync_run_counts(run.pk, fetched_delta=1)
    if ingested.record.request_fingerprint != descriptor.fingerprint:
        raise SecSyncError("Persisted SEC request fingerprint is inconsistent.")
    return ingested.record
