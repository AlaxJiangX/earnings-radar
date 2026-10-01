from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from threading import Barrier, Event

import pytest
from django.db import IntegrityError, close_old_connections, transaction

from accounts.models import User
from audit.constants import RAW_DATA_PAYLOAD_DB_LIMIT_BYTES
from audit.models import DataSource, RawDataObservation, RawDataRecord, SourceEvidence, SyncRun
from companies.models import Company, SecurityListing
from companies.services import update_company
from earnings.models import MonitoringPoolSnapshot
from earnings.services import (
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
    resolve_monitoring_pool_snapshot_contract,
    select_monitoring_pool,
)
from filings.models import Filing, FilingDocument
from filings.sync import SecSyncError, SecSyncResult, _sec_run_ownership, sync_sec_filings
from indexes.models import IndexMembership, MarketIndex
from providers.http import HttpTransport, TransportRequest, TransportResponse
from providers.sec_edgar import SecEdgarProvider
from tests.filings.sec_sync_helpers import FilingSpec, SecFilingTransport
from tests.filings.test_sec_provider_and_parsing import index_body, submissions_body

AS_OF = date(2026, 9, 30)
_SCOPE_POOL_HASH = "a" * 64


class FixtureTransport:
    def __init__(
        self,
        *,
        broken_index: bool = False,
        broken_submissions: bool = False,
        submissions_payload: bytes | None = None,
    ) -> None:
        self.requests: list[TransportRequest] = []
        self.broken_index = broken_index
        self.broken_submissions = broken_submissions
        self.submissions_payload = submissions_payload

    def send(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        if request.url.endswith("index.json"):
            body = b"{bad" if self.broken_index else index_body()
        else:
            body = (
                b"{bad"
                if self.broken_submissions
                else self.submissions_payload
                if self.submissions_payload is not None
                else submissions_body()
            )
        return TransportResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=body,
            fetched_at=datetime.now(UTC),
        )


def _setup(*, cik: str | None = "0000001234") -> tuple[Company, DataSource, MonitoringPoolSnapshot]:
    company = Company.objects.create(legal_name="SEC Example", display_name="SEC Example", cik=cik)
    listing = SecurityListing.objects.create(
        company=company,
        ticker="SECTEST",
        exchange="NYSE",
        security_name="SEC Example common",
        security_type="common_stock",
        effective_from=date(2026, 1, 1),
    )
    index, _ = MarketIndex.objects.get_or_create(
        code="SP500",
        defaults={"name": "S&P 500", "index_group": "LARGE", "is_enabled": True},
    )
    IndexMembership.objects.create(
        index=index,
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )
    snapshot = select_monitoring_pool(
        as_of=AS_OF,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        enabled_index_codes=("SP500",),
    ).snapshot
    source = _source(key="sec-official")
    return company, source, snapshot


def _source(*, key: str | None = None) -> DataSource:
    return DataSource.objects.create(
        key=key or f"sec-official-{uuid.uuid4().hex[:8]}",
        name="SEC official",
        source_type=DataSource.SourceType.SEC,
        base_url="https://data.sec.gov",
        is_official=True,
        provider_adapter="sec-edgar",
    )


def _run(
    *,
    source: DataSource,
    snapshot: MonitoringPoolSnapshot,
    transport: FixtureTransport,
    key: str,
    company_ids: tuple[uuid.UUID, ...] | None = None,
    on_filing_persisted: Callable[[Filing, SyncRun], None] | None = None,
) -> SecSyncResult:
    if company_ids is None:
        company_ids = tuple(
            member.company_id
            for member in resolve_monitoring_pool_snapshot_contract(
                as_of=snapshot.as_of_date,
                selector_version=snapshot.selector_version,
                pool_hash=snapshot.pool_hash,
            ).members
        )
    provider = SecEdgarProvider(user_agent="Earnings Radar test@example.org", transport=transport)
    return sync_sec_filings(
        source=source,
        provider=provider,
        company_ids=company_ids,
        pool_as_of=snapshot.as_of_date,
        pool_selector_version=snapshot.selector_version,
        pool_hash=snapshot.pool_hash,
        idempotency_key=key,
        on_filing_persisted=on_filing_persisted,
    )


def _run_with_scope(
    *,
    source: DataSource,
    company_ids: tuple[uuid.UUID, ...],
    transport: HttpTransport,
    key: str,
    on_filing_persisted: Callable[[Filing, SyncRun], None] | None = None,
) -> SecSyncResult:
    provider = SecEdgarProvider(user_agent="Earnings Radar test@example.org", transport=transport)
    return sync_sec_filings(
        source=source,
        provider=provider,
        company_ids=company_ids,
        pool_as_of=AS_OF,
        pool_selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        pool_hash=_SCOPE_POOL_HASH,
        idempotency_key=key,
        on_filing_persisted=on_filing_persisted,
    )


def _submissions_payload_for_cik(cik: str) -> bytes:
    return json.dumps(
        {
            "cik": int(cik),
            "filings": {
                "recent": {
                    "accessionNumber": [f"{cik}-26-000001"],
                    "form": ["4"],
                    "acceptanceDateTime": ["2026-03-09T16:30:00"],
                    "reportDate": [""],
                    "primaryDocument": ["ignored.htm"],
                }
            },
        }
    ).encode()


class CikScopedTransport:
    """Return one no-target submissions payload per requested CIK."""

    def __init__(self) -> None:
        self.requests: list[TransportRequest] = []

    def send(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        cik = request.url.rsplit("/CIK", 1)[1][:10]
        return TransportResponse(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=_submissions_payload_for_cik(cik),
            fetched_at=datetime.now(UTC),
        )


@pytest.mark.django_db(transaction=True)
def test_frozen_pool_sync_raw_first_provenance_and_repeated_runs() -> None:
    company, source, snapshot = _setup()
    Company.objects.create(legal_name="Outside", display_name="Outside", cik="0000007777")
    transport = FixtureTransport()
    first = _run(source=source, snapshot=snapshot, transport=transport, key="first")
    assert first.sync_run.status == SyncRun.Status.SUCCEEDED
    assert first.sync_run.fetched_count == 2
    assert len(transport.requests) == 2
    assert all("7777" not in request.url for request in transport.requests)
    filing = Filing.objects.get()
    assert filing.company_id == company.pk
    assert filing.accession_number == "0000001234-26-000001"
    assert filing.documents.count() == 2
    assert filing.source_evidence is not None
    assert filing.source_evidence.sync_run_id == first.sync_run.pk
    assert filing.source_evidence.raw_data_record.first_sync_run_id == first.sync_run.pk
    assert RawDataObservation.objects.filter(sync_run=first.sync_run).count() == 2
    assert all(document.source_evidence_id for document in filing.documents.all())

    second = _run(source=source, snapshot=snapshot, transport=transport, key="second")
    assert second.sync_run.status == SyncRun.Status.SUCCEEDED
    assert second.sync_run.fetched_count == 1
    assert Filing.objects.count() == 1
    assert FilingDocument.objects.count() == 2
    assert RawDataRecord.objects.count() == 2
    assert RawDataObservation.objects.count() == 3
    assert SourceEvidence.objects.filter(target_id=filing.pk).count() >= 1

    replay = _run(source=source, snapshot=snapshot, transport=transport, key="second")
    assert replay.run_created is False
    assert len(transport.requests) == 3


@pytest.mark.django_db(transaction=True)
def test_cik_change_during_fetch_rejects_wrong_filing_ownership() -> None:
    company, source, snapshot = _setup()
    actor = User.objects.create_user(email="sec-reviewer@example.org", password="fixture-only")

    class CikChangingTransport(FixtureTransport):
        def send(self, request: TransportRequest) -> TransportResponse:
            response = super().send(request)
            if request.url.endswith("index.json"):
                update_company(
                    company=company,
                    changes={"cik": "0000005678"},
                    actor_user=actor,
                    reason="Verified issuer identity correction.",
                    request_id="sec-cik-correction",
                )
            return response

    transport = CikChangingTransport()
    result = _run(source=source, snapshot=snapshot, transport=transport, key="cik-drift")
    assert Company.objects.get(pk=company.pk).cik == "0000005678"
    assert result.sync_run.status == SyncRun.Status.PARTIAL
    assert result.sync_run.failed_count == 1
    assert "FilingIntegrityError" in result.sync_run.error_summary
    assert Filing.objects.count() == 0
    assert FilingDocument.objects.count() == 0
    assert SourceEvidence.objects.count() == 0
    assert RawDataRecord.objects.count() == 2
    assert RawDataObservation.objects.filter(sync_run=result.sync_run).count() == 2
    assert all(
        "0000001234" in request.url or "/1234/" in request.url for request in transport.requests
    )


@pytest.mark.django_db(transaction=True)
def test_missing_primary_retries_directory_then_recovers_without_duplicates() -> None:
    _, source, snapshot = _setup()
    first = _run(source=source, snapshot=snapshot, transport=FixtureTransport(), key="initial")
    assert first.sync_run.status == SyncRun.Status.SUCCEEDED
    filing = Filing.objects.get()
    # Model-only fixture damage simulates an incomplete persisted directory record.
    FilingDocument.objects.filter(filing=filing, filename=filing.primary_document).delete()
    assert list(filing.documents.values_list("filename", flat=True)) == ["exhibit.htm"]

    failed_transport = FixtureTransport(broken_index=True)
    failed = _run(source=source, snapshot=snapshot, transport=failed_transport, key="retry-bad")
    assert failed.sync_run.status == SyncRun.Status.PARTIAL
    assert failed.sync_run.failed_count == 1
    assert failed.sync_run.fetched_count == 2
    assert len(failed_transport.requests) == 2
    assert list(filing.documents.values_list("filename", flat=True)) == ["exhibit.htm"]

    recovered_transport = FixtureTransport()
    recovered = _run(source=source, snapshot=snapshot, transport=recovered_transport, key="recover")
    assert recovered.sync_run.status == SyncRun.Status.SUCCEEDED
    assert recovered.sync_run.fetched_count == 2
    assert recovered.sync_run.created_count == 1
    assert Filing.objects.count() == 1
    assert FilingDocument.objects.count() == 2
    primary = FilingDocument.objects.get(filing=filing, filename=filing.primary_document)
    assert primary.source_evidence is not None
    assert primary.source_evidence.sync_run_id == recovered.sync_run.pk
    assert RawDataObservation.objects.filter(
        sync_run=recovered.sync_run, raw_data_record=primary.source_evidence.raw_data_record
    ).exists()
    assert FilingDocument.objects.filter(filing=filing, filename="exhibit.htm").count() == 1

    completed_transport = FixtureTransport()
    complete = _run(source=source, snapshot=snapshot, transport=completed_transport, key="done")
    assert complete.sync_run.status == SyncRun.Status.SUCCEEDED
    assert complete.sync_run.fetched_count == 1
    assert len(completed_transport.requests) == 1
    assert FilingDocument.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_missing_cik_is_skipped_without_ticker_fallback() -> None:
    _, source, snapshot = _setup(cik=None)
    transport = FixtureTransport()
    result = _run(source=source, snapshot=snapshot, transport=transport, key="missing-cik")
    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.skipped_count == 1
    assert not transport.requests
    assert not Filing.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("broken_index,broken_submissions", [(True, False), (False, True)])
def test_malformed_sec_metadata_preserves_raw_and_fails_run(
    broken_index: bool, broken_submissions: bool
) -> None:
    _, source, snapshot = _setup()
    transport = FixtureTransport(broken_index=broken_index, broken_submissions=broken_submissions)
    result = _run(source=source, snapshot=snapshot, transport=transport, key="malformed")
    assert result.sync_run.status in {SyncRun.Status.FAILED, SyncRun.Status.PARTIAL}
    assert result.sync_run.failed_count == 1
    assert RawDataRecord.objects.count() == len(transport.requests)
    assert not Filing.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_database_accession_and_document_uniqueness() -> None:
    _, source, snapshot = _setup()
    _run(source=source, snapshot=snapshot, transport=FixtureTransport(), key="unique")
    filing = Filing.objects.get()
    document = FilingDocument.objects.first()
    assert document is not None
    with pytest.raises(IntegrityError), transaction.atomic():
        Filing.objects.create(
            id=uuid.uuid4(),
            company=filing.company,
            accession_number=filing.accession_number,
            form_type=filing.form_type,
            accepted_at=filing.accepted_at,
            primary_document=filing.primary_document,
            filing_url=filing.filing_url,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        FilingDocument.objects.create(
            filing=filing,
            filename=document.filename,
            document_type=document.document_type,
            url=document.url,
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_accession_inserts_use_database_uniqueness() -> None:
    company = Company.objects.create(legal_name="Race", display_name="Race")
    gate = Barrier(2)

    def insert() -> bool:
        close_old_connections()
        try:
            gate.wait(timeout=5)
            Filing.objects.create(
                company=company,
                accession_number="0000001234-26-000001",
                form_type="10-Q",
                accepted_at=datetime(2026, 10, 1, tzinfo=UTC),
                primary_document="quarter.htm",
                filing_url="https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/quarter.htm",
            )
        except IntegrityError:
            return False
        finally:
            close_old_connections()
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: insert(), range(2)))
    assert sorted(results) == [False, True]
    assert Filing.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_oversized_submissions_fail_without_truncated_raw_record() -> None:
    _, source, snapshot = _setup()
    transport = FixtureTransport(submissions_payload=b"x" * (RAW_DATA_PAYLOAD_DB_LIMIT_BYTES + 1))
    result = _run(source=source, snapshot=snapshot, transport=transport, key="oversized")
    assert result.sync_run.status == SyncRun.Status.FAILED
    assert result.sync_run.failed_count == 1
    assert not RawDataRecord.objects.exists()
    assert not Filing.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_no_target_forms_succeeds_without_directory_fetch() -> None:
    _, source, snapshot = _setup()
    payload = submissions_body().replace(b'"10-Q"', b'"4"')
    transport = FixtureTransport(submissions_payload=payload)
    result = _run(source=source, snapshot=snapshot, transport=transport, key="no-target")
    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.fetched_count == 1
    assert len(transport.requests) == 1
    assert not Filing.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_global_sec_job_lock_rejects_parallel_sync() -> None:
    _, source, snapshot = _setup()
    locked = Event()
    release = Event()

    def hold_lock() -> None:
        close_old_connections()
        try:
            with _sec_run_ownership():
                locked.set()
                assert release.wait(timeout=5)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(hold_lock)
        try:
            assert locked.wait(timeout=5)
            with pytest.raises(SecSyncError, match="global SEC filing job"):
                _run(source=source, snapshot=snapshot, transport=FixtureTransport(), key="parallel")
        finally:
            release.set()
            future.result(timeout=5)
    assert not SyncRun.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_caller_scope_preserves_order_and_skips_monitoring_pool_lookup() -> None:
    first = Company.objects.create(legal_name="First", display_name="First", cik="0000001234")
    Company.objects.create(legal_name="Second", display_name="Second", cik="0000005678")
    excluded = Company.objects.create(
        legal_name="Excluded", display_name="Excluded", cik="0000009999"
    )
    source = _source()
    transport = CikScopedTransport()

    result = _run_with_scope(
        source=source,
        company_ids=(excluded.pk, first.pk),
        transport=transport,
        key="caller-scope",
    )

    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.fetched_count == 2
    assert result.sync_run.scope == {
        "monitoring_pool_as_of": AS_OF.isoformat(),
        "selector_version": EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        "monitoring_pool_hash": _SCOPE_POOL_HASH,
    }
    assert [request.url for request in transport.requests] == [
        "https://data.sec.gov/submissions/CIK0000009999.json",
        "https://data.sec.gov/submissions/CIK0000001234.json",
    ]
    assert all("0000005678" not in request.url for request in transport.requests)
    assert MonitoringPoolSnapshot.objects.count() == 0
    assert MarketIndex.objects.count() == 0
    assert Filing.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_empty_company_scope_succeeds_without_fetch() -> None:
    source = _source()
    transport = FixtureTransport()

    result = _run_with_scope(
        source=source,
        company_ids=(),
        transport=transport,
        key="empty-scope",
    )

    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.fetched_count == 0
    assert transport.requests == []


@pytest.mark.django_db(transaction=True)
def test_duplicate_company_ids_fail_closed_without_starting_a_run() -> None:
    company = Company.objects.create(
        legal_name="Duplicate", display_name="Duplicate", cik="0000001234"
    )
    source = _source()
    transport = FixtureTransport()
    provider = SecEdgarProvider(user_agent="Earnings Radar test@example.org", transport=transport)

    with pytest.raises(SecSyncError, match="duplicate Companies"):
        sync_sec_filings(
            source=source,
            provider=provider,
            company_ids=(company.pk, company.pk),
            pool_as_of=AS_OF,
            pool_selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
            pool_hash=_SCOPE_POOL_HASH,
            idempotency_key="duplicate-scope",
        )

    assert not SyncRun.objects.exists()
    assert transport.requests == []


@pytest.mark.django_db(transaction=True)
def test_on_filing_persisted_hook_runs_before_run_finalization() -> None:
    company = Company.objects.create(legal_name="Hook", display_name="Hook", cik="0000001234")
    source = _source()
    transport = SecFilingTransport(FilingSpec(form="10-Q", period_of_report="2026-03-31"))
    seen: list[tuple[uuid.UUID, uuid.UUID, str]] = []

    def hook(filing: Filing, sync_run: SyncRun) -> None:
        persisted = SyncRun.objects.get(pk=sync_run.pk)
        seen.append((filing.pk, sync_run.pk, persisted.status))

    result = _run_with_scope(
        source=source,
        company_ids=(company.pk,),
        transport=transport,
        key="hook-live",
        on_filing_persisted=hook,
    )

    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    filing = Filing.objects.get()
    assert seen == [(filing.pk, result.sync_run.pk, SyncRun.Status.RUNNING)]


@pytest.mark.django_db(transaction=True)
def test_on_filing_persisted_hook_not_called_for_complete_skip() -> None:
    _, source, snapshot = _setup()
    first = _run(source=source, snapshot=snapshot, transport=FixtureTransport(), key="initial")
    assert first.sync_run.status == SyncRun.Status.SUCCEEDED
    calls: list[uuid.UUID] = []

    second = _run(
        source=source,
        snapshot=snapshot,
        transport=FixtureTransport(),
        key="skip",
        on_filing_persisted=lambda filing, sync_run: calls.append(filing.pk),
    )

    assert second.sync_run.status == SyncRun.Status.SUCCEEDED
    assert second.sync_run.skipped_count == 1
    assert calls == []


@pytest.mark.django_db(transaction=True)
def test_on_filing_persisted_hook_failure_marks_partial_and_keeps_filing() -> None:
    _, source, snapshot = _setup()

    def hook(filing: Filing, sync_run: SyncRun) -> None:
        del filing, sync_run
        raise RuntimeError("fixture hook failure")

    result = _run(
        source=source,
        snapshot=snapshot,
        transport=FixtureTransport(),
        key="hook-fail",
        on_filing_persisted=hook,
    )

    assert result.sync_run.status == SyncRun.Status.PARTIAL
    assert result.sync_run.failed_count == 1
    assert "RuntimeError" in result.sync_run.error_summary
    assert Filing.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_on_filing_persisted_hook_not_called_when_record_filing_fails() -> None:
    _, source, snapshot = _setup()
    calls: list[uuid.UUID] = []

    result = _run(
        source=source,
        snapshot=snapshot,
        transport=FixtureTransport(broken_index=True),
        key="hook-no-persist",
        on_filing_persisted=lambda filing, sync_run: calls.append(filing.pk),
    )

    assert result.sync_run.status == SyncRun.Status.PARTIAL
    assert result.sync_run.failed_count == 1
    assert not Filing.objects.exists()
    assert calls == []


@pytest.mark.django_db(transaction=True)
def test_on_filing_persisted_hook_failure_does_not_block_later_filings() -> None:
    first = Company.objects.create(legal_name="First", display_name="First", cik="0000001234")
    second = Company.objects.create(legal_name="Second", display_name="Second", cik="0000005678")
    source = _source()
    transport = SecFilingTransport(FilingSpec(form="10-Q", period_of_report="2026-03-31"))
    calls: list[uuid.UUID] = []

    def hook(filing: Filing, sync_run: SyncRun) -> None:
        del sync_run
        calls.append(filing.pk)
        if len(calls) == 1:
            raise RuntimeError("first hook failure")

    result = _run_with_scope(
        source=source,
        company_ids=(first.pk, second.pk),
        transport=transport,
        key="hook-continue",
        on_filing_persisted=hook,
    )

    assert result.sync_run.status == SyncRun.Status.PARTIAL
    assert result.sync_run.failed_count == 1
    assert Filing.objects.count() == 2
    assert len(calls) == 2


@pytest.mark.django_db(transaction=True)
def test_successful_idempotent_run_does_not_call_hook() -> None:
    _, source, snapshot = _setup()
    calls: list[uuid.UUID] = []

    def hook(filing: Filing, sync_run: SyncRun) -> None:
        del sync_run
        calls.append(filing.pk)

    first = _run(
        source=source,
        snapshot=snapshot,
        transport=FixtureTransport(),
        key="same-key",
        on_filing_persisted=hook,
    )
    second = _run(
        source=source,
        snapshot=snapshot,
        transport=FixtureTransport(),
        key="same-key",
        on_filing_persisted=hook,
    )

    assert first.run_created is True
    assert second.run_created is False
    assert len(calls) == 1
