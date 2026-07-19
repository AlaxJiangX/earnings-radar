from __future__ import annotations

from pathlib import Path

import pytest

from audit.models import DataSource, RawDataObservation, RawDataRecord, SyncRun
from audit.services import mark_sync_run_failed, start_sync_run, update_sync_run_counts
from indexes.constituents import (
    IndexConstituentSnapshot,
    parse_index_constituent_snapshot,
)
from indexes.sync_services import (
    INDEX_SNAPSHOT_JOB_TYPE,
    IndexSnapshotIngestionIntegrityError,
    IndexSnapshotParseError,
    IndexSnapshotPreviousRunFailed,
    IndexSnapshotSyncAlreadyRunning,
    InvalidIndexSnapshotConfiguration,
    InvalidIndexSnapshotSource,
    UnsafeIndexSnapshot,
    ingest_index_constituent_snapshot,
)
from providers.exceptions import ProviderTimeoutError
from providers.testing import (
    FIXTURE_REQUEST_STARTED_AT,
    FakeProvider,
    FakeProviderScenario,
    FixtureIndexConstituentProvider,
)
from providers.types import ProviderCapability, ProviderRequest, ProviderResult

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "providers"
    / "index_constituents"
    / "sp500.json"
)
PARSER_VERSION = "fixture-canonical-v1"


class CountingFixtureIndexConstituentProvider(FixtureIndexConstituentProvider):
    def __init__(self, *, fixtures: dict[str, bytes]) -> None:
        super().__init__(fixtures=fixtures)
        self.fetch_count = 0

    def _fetch(self, request: ProviderRequest) -> ProviderResult:
        self.fetch_count += 1
        return super()._fetch(request)


@pytest.fixture
def index_source(db: object) -> DataSource:
    del db
    return DataSource.objects.create(
        key="fixture-index",
        name="Fixture index snapshots",
        source_type=DataSource.SourceType.INDEX,
        base_url="https://fixture-index.test",
        provider_adapter=FixtureIndexConstituentProvider.provider_key,
        license_notes="Artificial offline fixture only.",
    )


def _request(
    *,
    capability: ProviderCapability = ProviderCapability.INDEX_CONSTITUENTS,
    method: str = "GET",
    source_url: str = "https://fixture-index.test/SP500/constituents?date=2026-07-15",
    request_identity: dict[str, object] | None = None,
) -> ProviderRequest:
    return ProviderRequest(
        capability=capability,
        scope={"index_code": "SP500"},
        request_started_at=FIXTURE_REQUEST_STARTED_AT,
        source_url=source_url,
        method=method,
        request_identity=request_identity or {"index_code": "SP500"},
    )


def _provider(
    raw_content: bytes | None = None,
) -> CountingFixtureIndexConstituentProvider:
    return CountingFixtureIndexConstituentProvider(
        fixtures={"SP500": raw_content if raw_content is not None else FIXTURE_PATH.read_bytes()}
    )


@pytest.mark.django_db
def test_ingestion_persists_raw_observation_before_parsing(
    index_source: DataSource,
) -> None:
    parser_saw_pending_raw = False

    def parser(raw_content: bytes, *, expected_index_code: str) -> IndexConstituentSnapshot:
        nonlocal parser_saw_pending_raw
        raw_record = RawDataRecord.objects.get()
        parser_saw_pending_raw = (
            raw_record.parser_status == RawDataRecord.ParserStatus.PENDING
            and RawDataObservation.objects.filter(raw_data_record=raw_record).count() == 1
        )
        return parse_index_constituent_snapshot(
            raw_content,
            expected_index_code=expected_index_code,
        )

    result = ingest_index_constituent_snapshot(
        source=index_source,
        provider=_provider(),
        request=_request(),
        parser=parser,
        idempotency_key="SP500:2026-07-15",
        parser_version=PARSER_VERSION,
        code_version="test-v1",
    )

    result.raw_data_record.refresh_from_db()
    assert parser_saw_pending_raw is True
    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.fetched_count == 1
    assert result.sync_run.created_count == 1
    assert result.sync_run.skipped_count == 0
    assert result.raw_data_record.parser_status == RawDataRecord.ParserStatus.PARSED
    assert result.raw_data_record.parser_version == PARSER_VERSION
    assert result.snapshot.index_code == "SP500"
    assert result.run_created is True
    assert result.raw_record_created is True
    assert result.observation_created is True


@pytest.mark.django_db
def test_same_idempotency_key_replays_without_fetching_or_duplicate_history(
    index_source: DataSource,
) -> None:
    provider = _provider()
    first = ingest_index_constituent_snapshot(
        source=index_source,
        provider=provider,
        request=_request(),
        parser=parse_index_constituent_snapshot,
        idempotency_key="SP500:2026-07-15",
        parser_version=PARSER_VERSION,
    )
    replay = ingest_index_constituent_snapshot(
        source=index_source,
        provider=provider,
        request=_request(),
        parser=parse_index_constituent_snapshot,
        idempotency_key="SP500:2026-07-15",
        parser_version=PARSER_VERSION,
    )

    assert provider.fetch_count == 1
    assert replay.sync_run.pk == first.sync_run.pk
    assert replay.raw_data_record.pk == first.raw_data_record.pk
    assert replay.snapshot == first.snapshot
    assert replay.run_created is False
    assert SyncRun.objects.count() == 1
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "changed_request",
    (
        _request(source_url="https://fixture-index.test/SP500/other?date=2026-07-15"),
        _request(method="POST"),
        _request(request_identity={"index_code": "SP500", "format": "csv"}),
    ),
)
def test_same_idempotency_key_rejects_changed_request_context_before_fetch(
    index_source: DataSource,
    changed_request: ProviderRequest,
) -> None:
    provider = _provider()
    ingest_index_constituent_snapshot(
        source=index_source,
        provider=provider,
        request=_request(),
        parser=parse_index_constituent_snapshot,
        idempotency_key="SP500:request-context",
        parser_version=PARSER_VERSION,
        code_version="test-v1",
    )

    with pytest.raises(IndexSnapshotIngestionIntegrityError, match="request context"):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=provider,
            request=changed_request,
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:request-context",
            parser_version=PARSER_VERSION,
            code_version="test-v1",
        )

    assert provider.fetch_count == 1
    assert SyncRun.objects.count() == 1


@pytest.mark.django_db
def test_new_run_with_same_payload_reuses_raw_record_and_adds_observation(
    index_source: DataSource,
) -> None:
    provider = _provider()
    first = ingest_index_constituent_snapshot(
        source=index_source,
        provider=provider,
        request=_request(),
        parser=parse_index_constituent_snapshot,
        idempotency_key="SP500:2026-07-15:first",
        parser_version=PARSER_VERSION,
    )
    second = ingest_index_constituent_snapshot(
        source=index_source,
        provider=provider,
        request=_request(),
        parser=parse_index_constituent_snapshot,
        idempotency_key="SP500:2026-07-15:second",
        parser_version=PARSER_VERSION,
    )

    assert provider.fetch_count == 2
    assert second.raw_data_record.pk == first.raw_data_record.pk
    assert second.raw_record_created is False
    assert second.observation_created is True
    assert second.sync_run.created_count == 0
    assert second.sync_run.skipped_count == 1
    assert SyncRun.objects.count() == 2
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 2


@pytest.mark.django_db
def test_older_successful_run_replays_after_shared_raw_record_uses_new_parser_version(
    index_source: DataSource,
) -> None:
    provider = _provider()
    first = ingest_index_constituent_snapshot(
        source=index_source,
        provider=provider,
        request=_request(),
        parser=parse_index_constituent_snapshot,
        idempotency_key="SP500:parser-v1",
        parser_version="parser-v1",
    )
    ingest_index_constituent_snapshot(
        source=index_source,
        provider=provider,
        request=_request(),
        parser=parse_index_constituent_snapshot,
        idempotency_key="SP500:parser-v2",
        parser_version="parser-v2",
    )

    replay = ingest_index_constituent_snapshot(
        source=index_source,
        provider=provider,
        request=_request(),
        parser=parse_index_constituent_snapshot,
        idempotency_key="SP500:parser-v1",
        parser_version="parser-v1",
    )

    first.raw_data_record.refresh_from_db()
    assert first.raw_data_record.parser_version == "parser-v2"
    assert replay.sync_run.pk == first.sync_run.pk
    assert replay.snapshot == first.snapshot
    assert provider.fetch_count == 2


@pytest.mark.django_db
def test_parse_failure_keeps_raw_payload_and_marks_run_failed(
    index_source: DataSource,
) -> None:
    provider = _provider(b'{"index_code":"SP500","as_of_date":"2026-07-15"}')

    with pytest.raises(IndexSnapshotParseError, match="parsing failed"):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=provider,
            request=_request(),
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:parse-failure",
            parser_version=PARSER_VERSION,
        )

    raw_record = RawDataRecord.objects.get()
    sync_run = SyncRun.objects.get()
    assert raw_record.parser_status == RawDataRecord.ParserStatus.FAILED
    assert raw_record.parser_version == PARSER_VERSION
    assert raw_record.parse_error == "Index snapshot parsing failed."
    assert sync_run.status == SyncRun.Status.FAILED
    assert sync_run.fetched_count == 1
    assert sync_run.failed_count == 1
    assert "IndexSnapshotParseError" in sync_run.error_summary
    assert RawDataObservation.objects.filter(sync_run=sync_run).count() == 1


@pytest.mark.django_db
def test_provider_failure_marks_run_failed_without_raw_record(
    index_source: DataSource,
) -> None:
    index_source.provider_adapter = FakeProvider.provider_key
    index_source.save(update_fields=("provider_adapter",))

    with pytest.raises(ProviderTimeoutError):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=FakeProvider(FakeProviderScenario.TIMEOUT),
            request=_request(),
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:provider-timeout",
            parser_version=PARSER_VERSION,
        )

    sync_run = SyncRun.objects.get()
    assert sync_run.status == SyncRun.Status.FAILED
    assert sync_run.fetched_count == 0
    assert sync_run.failed_count == 1
    assert RawDataRecord.objects.count() == 0
    assert RawDataObservation.objects.count() == 0


@pytest.mark.django_db
def test_empty_snapshot_is_retained_but_domain_writes_are_blocked(
    index_source: DataSource,
) -> None:
    provider = _provider(b'{"index_code":"SP500","as_of_date":"2026-07-15","constituents":[]}')

    with pytest.raises(UnsafeIndexSnapshot, match="empty"):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=provider,
            request=_request(),
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:empty",
            parser_version=PARSER_VERSION,
        )

    raw_record = RawDataRecord.objects.get()
    sync_run = SyncRun.objects.get()
    assert raw_record.parser_status == RawDataRecord.ParserStatus.PARSED
    assert sync_run.status == SyncRun.Status.FAILED
    assert sync_run.fetched_count == 1
    assert sync_run.failed_count == 1


@pytest.mark.django_db
def test_terminal_write_failure_rolls_back_parse_state_and_success_counts(
    index_source: DataSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_finish(_sync_run_id: object) -> SyncRun:
        raise RuntimeError("fixture terminal write failure")

    monkeypatch.setattr(
        "indexes.sync_services.mark_sync_run_succeeded",
        fail_to_finish,
    )

    with pytest.raises(RuntimeError, match="terminal write failure"):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=_provider(),
            request=_request(),
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:terminal-failure",
            parser_version=PARSER_VERSION,
        )

    raw_record = RawDataRecord.objects.get()
    sync_run = SyncRun.objects.get()
    assert raw_record.parser_status == RawDataRecord.ParserStatus.PENDING
    assert sync_run.status == SyncRun.Status.FAILED
    assert sync_run.fetched_count == 1
    assert sync_run.created_count == 0
    assert sync_run.skipped_count == 0
    assert sync_run.failed_count == 1


@pytest.mark.django_db
@pytest.mark.parametrize("source_change", ["disabled", "wrong_type", "wrong_provider"])
def test_invalid_source_is_rejected_before_starting_run(
    index_source: DataSource,
    source_change: str,
) -> None:
    if source_change == "disabled":
        index_source.is_enabled = False
        index_source.save(update_fields=("is_enabled",))
    elif source_change == "wrong_type":
        index_source.source_type = DataSource.SourceType.MANUAL
        index_source.save(update_fields=("source_type",))
    else:
        index_source.provider_adapter = "another-provider"
        index_source.save(update_fields=("provider_adapter",))

    with pytest.raises(InvalidIndexSnapshotSource):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=_provider(),
            request=_request(),
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:invalid-source",
            parser_version=PARSER_VERSION,
        )

    assert SyncRun.objects.count() == 0
    assert RawDataRecord.objects.count() == 0


@pytest.mark.django_db
def test_wrong_capability_is_rejected_before_starting_run(
    index_source: DataSource,
) -> None:
    with pytest.raises(InvalidIndexSnapshotSource, match="capability"):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=_provider(),
            request=_request(capability=ProviderCapability.EARNINGS_CALENDAR),
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:wrong-capability",
            parser_version=PARSER_VERSION,
        )

    assert SyncRun.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("parser_version", "code_version"),
    [("", ""), ("x" * 101, ""), (PARSER_VERSION, "x" * 101)],
)
def test_invalid_version_configuration_is_rejected_before_starting_run(
    index_source: DataSource,
    parser_version: str,
    code_version: str,
) -> None:
    provider = _provider()

    with pytest.raises(InvalidIndexSnapshotConfiguration):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=provider,
            request=_request(),
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:invalid-version",
            parser_version=parser_version,
            code_version=code_version,
        )

    assert provider.fetch_count == 0
    assert SyncRun.objects.count() == 0


@pytest.mark.django_db
def test_replay_rejects_different_code_version_without_fetching(
    index_source: DataSource,
) -> None:
    provider = _provider()
    ingest_index_constituent_snapshot(
        source=index_source,
        provider=provider,
        request=_request(),
        parser=parse_index_constituent_snapshot,
        idempotency_key="SP500:code-version",
        parser_version=PARSER_VERSION,
        code_version="code-v1",
    )

    with pytest.raises(IndexSnapshotIngestionIntegrityError, match="context"):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=provider,
            request=_request(),
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:code-version",
            parser_version=PARSER_VERSION,
            code_version="code-v2",
        )

    assert provider.fetch_count == 1
    assert SyncRun.objects.count() == 1


@pytest.mark.django_db
def test_existing_running_run_blocks_duplicate_worker(
    index_source: DataSource,
) -> None:
    existing = start_sync_run(
        job_type=INDEX_SNAPSHOT_JOB_TYPE,
        source=index_source,
        scope=_request().scope,
        idempotency_key="SP500:running",
        parser_version=PARSER_VERSION,
        started_at=FIXTURE_REQUEST_STARTED_AT,
    )
    provider = _provider()

    with pytest.raises(IndexSnapshotSyncAlreadyRunning, match="already running"):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=provider,
            request=_request(),
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:running",
            parser_version=PARSER_VERSION,
        )

    existing.refresh_from_db()
    assert existing.status == SyncRun.Status.RUNNING
    assert provider.fetch_count == 0
    assert RawDataRecord.objects.count() == 0


@pytest.mark.django_db
def test_failed_run_requires_new_retry_key(
    index_source: DataSource,
) -> None:
    existing = start_sync_run(
        job_type=INDEX_SNAPSHOT_JOB_TYPE,
        source=index_source,
        scope=_request().scope,
        idempotency_key="SP500:failed",
        parser_version=PARSER_VERSION,
        started_at=FIXTURE_REQUEST_STARTED_AT,
    )
    update_sync_run_counts(existing.pk, failed_delta=1)
    mark_sync_run_failed(existing.pk, error_summary="Fixture prior failure.")
    provider = _provider()

    with pytest.raises(IndexSnapshotPreviousRunFailed, match="new retry key"):
        ingest_index_constituent_snapshot(
            source=index_source,
            provider=provider,
            request=_request(),
            parser=parse_index_constituent_snapshot,
            idempotency_key="SP500:failed",
            parser_version=PARSER_VERSION,
        )

    assert provider.fetch_count == 0
    assert SyncRun.objects.count() == 1
    assert RawDataRecord.objects.count() == 0
