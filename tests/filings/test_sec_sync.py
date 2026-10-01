from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from threading import Barrier, Event

import pytest
from django.db import IntegrityError, close_old_connections, transaction

from audit.constants import RAW_DATA_PAYLOAD_DB_LIMIT_BYTES
from audit.models import DataSource, RawDataObservation, RawDataRecord, SourceEvidence, SyncRun
from companies.models import Company, SecurityListing
from earnings.models import MonitoringPoolSnapshot
from earnings.services import EARNINGS_MONITORING_POOL_SELECTOR_VERSION, select_monitoring_pool
from filings.models import Filing, FilingDocument
from filings.sync import SecSyncError, SecSyncResult, _sec_run_ownership, sync_sec_filings
from indexes.models import IndexMembership, MarketIndex
from providers.http import TransportRequest, TransportResponse
from providers.sec_edgar import SecEdgarProvider
from tests.filings.test_sec_provider_and_parsing import index_body, submissions_body

AS_OF = date(2026, 9, 30)


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
    source = DataSource.objects.create(
        key="sec-official",
        name="SEC official",
        source_type=DataSource.SourceType.SEC,
        base_url="https://data.sec.gov",
        is_official=True,
        provider_adapter="sec-edgar",
    )
    return company, source, snapshot


def _run(
    *, source: DataSource, snapshot: MonitoringPoolSnapshot, transport: FixtureTransport, key: str
) -> SecSyncResult:
    provider = SecEdgarProvider(user_agent="Earnings Radar test@example.org", transport=transport)
    return sync_sec_filings(
        source=source,
        provider=provider,
        pool_as_of=snapshot.as_of_date,
        pool_selector_version=snapshot.selector_version,
        pool_hash=snapshot.pool_hash,
        idempotency_key=key,
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
