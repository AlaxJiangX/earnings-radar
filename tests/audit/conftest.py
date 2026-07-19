import hashlib

import pytest

from audit.models import DataSource, RawDataObservation, RawDataRecord, SyncRun
from audit.services import start_sync_run


@pytest.fixture
def data_source(db: object) -> DataSource:
    del db
    return DataSource.objects.create(
        key="fixture-source",
        name="Fixture source",
        source_type=DataSource.SourceType.MANUAL,
        base_url="https://example.test/",
        is_official=False,
        provider_adapter="",
        license_notes="Test fixture only; no external access.",
    )


@pytest.fixture
def sync_run(data_source: DataSource) -> SyncRun:
    return start_sync_run(
        job_type="fixture.raw-data",
        source=data_source,
        scope={"fixture": True},
        idempotency_key="fixture.raw-data:initial",
        code_version="test-code",
        parser_version="test-parser",
    )


@pytest.fixture
def raw_data_record(sync_run: SyncRun) -> RawDataRecord:
    payload = b'{"fixture": true}'
    return RawDataRecord.objects.create(
        source=sync_run.source,
        first_sync_run=sync_run,
        source_url="https://example.test/data",
        request_fingerprint=hashlib.sha256(b"fixture-request").hexdigest(),
        fetched_at=sync_run.started_at,
        http_status=200,
        content_type="application/json",
        encoding="utf-8",
        content_hash=hashlib.sha256(payload).hexdigest(),
        payload=payload,
        payload_size_bytes=len(payload),
    )


@pytest.fixture
def raw_data_observation(
    sync_run: SyncRun,
    raw_data_record: RawDataRecord,
) -> RawDataObservation:
    return RawDataObservation.objects.create(
        sync_run=sync_run,
        raw_data_record=raw_data_record,
        observed_at=sync_run.started_at,
    )
