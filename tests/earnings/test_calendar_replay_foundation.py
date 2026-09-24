from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.utils import timezone

from audit.models import (
    DataSource,
    RawDataObservation,
    RawDataParseAttempt,
    RawDataRecord,
    SyncRun,
)
from audit.services import (
    InvalidSyncRunCount,
    mark_sync_run_succeeded,
    record_raw_data_parse_attempt,
    start_sync_run_with_result,
    update_sync_run_counts,
)
from earnings.calendar_parsing import FixtureEarningsCalendarParser
from earnings.models import EarningsCalendarObservation
from earnings.services import (
    EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
    EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    EarningsCalendarPage,
    EarningsCalendarPageSource,
    EarningsCalendarReplayContextMismatch,
    EarningsCalendarReplayCountMismatch,
    EarningsCalendarRunBusy,
    EarningsCalendarRunOwnershipLost,
    InvalidEarningsCalendarReplay,
    build_earnings_calendar_replay_idempotency_key,
    build_earnings_calendar_replay_input_digest,
    build_earnings_calendar_sync_scope,
    calendar_run_ownership,
    execute_scheduled_earnings_calendar_window,
    reconcile_earnings_calendar_replayed_count,
    retire_stale_earnings_calendar_replay_run,
    start_earnings_calendar_replay_sync_run,
    validate_earnings_calendar_replay_pool_contract,
    validate_earnings_calendar_replay_source,
)
from earnings.services import calendar_replay_foundation as replay_foundation
from tests.earnings.helpers import make_calendar_observation

PROVIDER_KEY = "fixture-calendar-provider"
JOB_TYPE = EARNINGS_CALENDAR_WINDOW_JOB_TYPE
PARSER_VERSION = "fixture-parser-v1"
POOL_AS_OF = date(2026, 9, 21)
POOL_HASH = "a" * 64
SELECTOR_VERSION = "earnings-monitoring-pool-v1"


def _source(suffix: str = "source") -> DataSource:
    return DataSource.objects.create(
        key=f"fixture-replay-{suffix}-{uuid.uuid4().hex[:8]}",
        name=f"Replay source {suffix}",
        source_type=DataSource.SourceType.EARNINGS_CALENDAR,
        base_url="https://calendar.example.test/",
        provider_adapter=PROVIDER_KEY,
        license_notes="Synthetic test-only source.",
    )


def _scope(*, window_kind: str = "scheduled", pool_hash: str = POOL_HASH) -> dict[str, object]:
    return build_earnings_calendar_sync_scope(
        provider_key=PROVIDER_KEY,
        window_kind=window_kind,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 12, 20),
        monitoring_pool_as_of=POOL_AS_OF,
        monitoring_pool_hash=pool_hash,
        selector_version=SELECTOR_VERSION,
    )


def _source_run(
    source: DataSource,
    *,
    status: str = SyncRun.Status.SUCCEEDED,
    started_at: datetime | None = None,
) -> SyncRun:
    started = started_at or datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    return SyncRun.objects.create(
        job_type=JOB_TYPE,
        source=source,
        scope=_scope(),
        idempotency_key=f"source:{uuid.uuid4()}",
        status=status,
        started_at=started,
        finished_at=started if status != SyncRun.Status.RUNNING else None,
        heartbeat_at=started,
        parser_version=PARSER_VERSION,
    )


def _raw_record(source: DataSource, source_run: SyncRun, suffix: str) -> RawDataRecord:
    payload = f'{{"suffix":"{suffix}"}}'.encode()
    return RawDataRecord.objects.create(
        source=source,
        first_sync_run=source_run,
        source_url=f"https://calendar.example.test/{suffix}",
        request_fingerprint=hashlib.sha256(f"request:{suffix}".encode()).hexdigest(),
        fetched_at=source_run.started_at,
        http_status=200,
        content_type="application/json",
        encoding="utf-8",
        content_hash=hashlib.sha256(payload).hexdigest(),
        payload=payload,
        payload_size_bytes=len(payload),
    )


def _observed_source(
    suffix: str,
    *,
    count: int = 1,
) -> tuple[DataSource, SyncRun, tuple[RawDataRecord, ...], tuple[RawDataObservation, ...]]:
    source = _source(suffix)
    source_run = _source_run(source)
    records: list[RawDataRecord] = []
    observations: list[RawDataObservation] = []
    for index in range(count):
        record = _raw_record(source, source_run, f"{suffix}-{index}")
        records.append(record)
        observations.append(
            RawDataObservation.objects.create(
                sync_run=source_run,
                raw_data_record=record,
            )
        )
    if count:
        source_run.fetched_count = count
        source_run.save(update_fields=("fetched_count",))
    return source, source_run, tuple(records), tuple(observations)


def _replay_run(
    source: DataSource,
    source_run: SyncRun,
    *,
    parser_version: str = PARSER_VERSION,
    replay_contract_version: str = EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
    started_at: datetime | None = None,
) -> SyncRun:
    return start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=parser_version,
        replay_contract_version=replay_contract_version,
        started_at=started_at,
    ).sync_run


def _in_thread(operation: Callable[[], object], results: list[object]) -> None:
    close_old_connections()
    try:
        results.append(operation())
    except Exception as error:
        results.append(error)
    finally:
        for connection in connections.all():
            connection.close()


class _NoFetchPageSource:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def fetch_page(self, cursor: str | None) -> EarningsCalendarPage:
        self.calls.append(cursor)
        raise AssertionError("Replay concurrency must be rejected before provider access.")


@pytest.mark.django_db
def test_ingestion_defaults_remain_valid_and_replay_mode_is_explicit() -> None:
    source = _source("defaults")
    run = _source_run(source)

    assert run.run_mode == SyncRun.RunMode.INGESTION
    assert run.replay_source_sync_run_id is None
    assert run.replay_contract_version == ""
    assert run.replay_input_digest == ""
    assert run.replayed_count == 0
    run.full_clean()


@pytest.mark.django_db
def test_replay_source_requires_terminal_ingestion_with_matching_identity() -> None:
    source, source_run, _records, _observations = _observed_source("lineage")
    other_source = _source("other")

    assert (
        validate_earnings_calendar_replay_source(source_sync_run=source_run, source=source).pk
        == source_run.pk
    )
    with pytest.raises(InvalidEarningsCalendarReplay, match="DataSource"):
        validate_earnings_calendar_replay_source(source_sync_run=source_run, source=other_source)

    wrong_job = _source_run(source)
    wrong_job.job_type = "other.job"
    wrong_job.save(update_fields=("job_type",))
    with pytest.raises(InvalidEarningsCalendarReplay, match="wrong job type"):
        validate_earnings_calendar_replay_source(source_sync_run=wrong_job, source=source)

    running = _source_run(source, status=SyncRun.Status.RUNNING)
    with pytest.raises(InvalidEarningsCalendarReplay, match="terminal"):
        validate_earnings_calendar_replay_source(source_sync_run=running, source=source)

    replay_source = _replay_run(source, source_run)
    with pytest.raises(InvalidEarningsCalendarReplay, match="another replay"):
        validate_earnings_calendar_replay_source(
            source_sync_run=replay_source,
            source=source,
        )


@pytest.mark.django_db
def test_replay_source_count_must_match_persisted_raw_observations() -> None:
    source = _source("source-count")
    source_run = _source_run(source)
    source_run.fetched_count = 1
    source_run.save(update_fields=("fetched_count",))

    with pytest.raises(EarningsCalendarReplayCountMismatch, match="fetch count"):
        validate_earnings_calendar_replay_source(
            source_sync_run=source_run,
            source=source,
        )


@pytest.mark.django_db
def test_successful_source_without_raw_evidence_is_not_replayable() -> None:
    source = _source("empty-success")
    source_run = _source_run(source)

    with pytest.raises(InvalidEarningsCalendarReplay, match="raw evidence"):
        validate_earnings_calendar_replay_source(
            source_sync_run=source_run,
            source=source,
        )


@pytest.mark.django_db
def test_sync_run_db_constraint_rejects_self_replay_lineage() -> None:
    source = _source("self")
    run_id = uuid.uuid4()
    now = timezone.now()
    with pytest.raises(IntegrityError):
        SyncRun.objects.create(
            id=run_id,
            job_type=JOB_TYPE,
            source=source,
            scope=_scope(window_kind="replay"),
            idempotency_key="self-replay",
            run_mode=SyncRun.RunMode.REPLAY,
            replay_source_sync_run_id=run_id,
            replay_contract_version=EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
            replay_input_digest="b" * 64,
            parser_version=PARSER_VERSION,
            started_at=now,
            heartbeat_at=now,
        )


@pytest.mark.django_db
def test_replay_db_constraint_keeps_provider_fetch_count_zero() -> None:
    source = _source("fetch-constraint")
    source_run = _source_run(source)
    now = timezone.now()

    with pytest.raises(IntegrityError):
        SyncRun.objects.create(
            job_type=JOB_TYPE,
            source=source,
            scope=_scope(window_kind="replay"),
            idempotency_key="invalid-replay-fetch",
            run_mode=SyncRun.RunMode.REPLAY,
            replay_source_sync_run=source_run,
            replay_contract_version=EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
            replay_input_digest="b" * 64,
            parser_version=PARSER_VERSION,
            fetched_count=1,
            started_at=now,
            heartbeat_at=now,
        )


@pytest.mark.django_db
def test_sync_run_db_constraints_reject_invalid_mode_and_ingestion_replay_metadata() -> None:
    source, source_run, _records, _observations = _observed_source("db-invalid-ingestion")
    now = timezone.now()

    invalid_rows: tuple[dict[str, object], ...] = (
        {"run_mode": "invalid"},
        {"scope": _scope(window_kind="replay")},
        {"replay_source_sync_run": source_run},
        {"replay_contract_version": EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION},
        {"replay_input_digest": "a" * 64},
        {"replayed_count": 1},
    )
    for index, overrides in enumerate(invalid_rows):
        values: dict[str, object] = {
            "job_type": JOB_TYPE,
            "source": source,
            "scope": _scope(),
            "idempotency_key": f"invalid-ingestion-{index}",
            "run_mode": SyncRun.RunMode.INGESTION,
            "started_at": now,
            "heartbeat_at": now,
        }
        values.update(overrides)
        with pytest.raises(IntegrityError), transaction.atomic():
            SyncRun.objects.create(**values)


@pytest.mark.django_db
def test_replay_db_constraint_requires_complete_replay_metadata() -> None:
    source, source_run, _records, _observations = _observed_source("db-invalid-replay")
    now = timezone.now()

    invalid_rows: tuple[dict[str, object], ...] = (
        {"replay_source_sync_run": None},
        {"replay_contract_version": ""},
        {"replay_input_digest": ""},
        {"replay_input_digest": "not-a-digest"},
        {"parser_version": ""},
        {"parser_version": "   "},
        {"fetched_count": 1},
    )
    for index, overrides in enumerate(invalid_rows):
        values: dict[str, object] = {
            "job_type": JOB_TYPE,
            "source": source,
            "scope": _scope(window_kind="replay"),
            "idempotency_key": f"invalid-replay-{index}",
            "run_mode": SyncRun.RunMode.REPLAY,
            "replay_source_sync_run": source_run,
            "replay_contract_version": EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
            "replay_input_digest": "b" * 64,
            "parser_version": PARSER_VERSION,
            "started_at": now,
            "heartbeat_at": now,
        }
        values.update(overrides)
        with pytest.raises(IntegrityError), transaction.atomic():
            SyncRun.objects.create(**values)


@pytest.mark.django_db
def test_generic_replay_start_reloads_persisted_source_and_rejects_scope_bypass() -> None:
    source, source_run, _records, _observations = _observed_source("generic-scope")
    replay_scope = dict(source_run.scope)
    replay_scope["window_kind"] = "replay"
    wrong_scope = dict(replay_scope)
    wrong_scope["monitoring_pool_hash"] = "f" * 64

    with pytest.raises(ValueError, match="must match"):
        start_sync_run_with_result(
            job_type=JOB_TYPE,
            source=source,
            scope=wrong_scope,
            idempotency_key="generic-scope-mismatch",
            parser_version=PARSER_VERSION,
            run_mode=SyncRun.RunMode.REPLAY,
            replay_source_sync_run=source_run,
            replay_contract_version=EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
            replay_input_digest="b" * 64,
        )

    other_source = _source("generic-source-tamper")
    tampered_source_run = SyncRun.objects.get(pk=source_run.pk)
    tampered_source_run.source_id = other_source.pk
    with pytest.raises(ValueError, match="same DataSource"):
        start_sync_run_with_result(
            job_type=JOB_TYPE,
            source=other_source,
            scope=replay_scope,
            idempotency_key="generic-source-mismatch",
            parser_version=PARSER_VERSION,
            run_mode=SyncRun.RunMode.REPLAY,
            replay_source_sync_run=tampered_source_run,
            replay_contract_version=EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
            replay_input_digest="b" * 64,
        )


@pytest.mark.parametrize("status", (SyncRun.Status.RUNNING, SyncRun.Status.SKIPPED))
@pytest.mark.django_db
def test_generic_replay_start_rejects_non_replayable_source_status(
    status: str,
) -> None:
    source = _source(f"generic-status-{status}")
    source_run = _source_run(source, status=status)
    replay_scope = dict(source_run.scope)
    replay_scope["window_kind"] = "replay"

    with pytest.raises(ValueError, match="terminal and replayable"):
        start_sync_run_with_result(
            job_type=JOB_TYPE,
            source=source,
            scope=replay_scope,
            idempotency_key=f"generic-status-{status}",
            parser_version=PARSER_VERSION,
            run_mode=SyncRun.RunMode.REPLAY,
            replay_source_sync_run=source_run,
            replay_contract_version=EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
            replay_input_digest="b" * 64,
        )


@pytest.mark.django_db
def test_digest_is_order_independent_and_requires_the_complete_manifest() -> None:
    source, source_run, _records, observations = _observed_source("digest", count=2)
    first_observation, second_observation = observations

    digest_one = build_earnings_calendar_replay_input_digest(
        source_sync_run=source_run,
        parser_version=PARSER_VERSION,
        observations=[first_observation, second_observation],
    )
    digest_two = build_earnings_calendar_replay_input_digest(
        source_sync_run=source_run,
        parser_version=PARSER_VERSION,
        observations=[second_observation, first_observation],
    )
    digest_three = build_earnings_calendar_replay_input_digest(
        source_sync_run=source_run,
        parser_version=PARSER_VERSION,
    )

    assert digest_one == digest_two == digest_three

    with pytest.raises(InvalidEarningsCalendarReplay, match="every persisted"):
        build_earnings_calendar_replay_input_digest(
            source_sync_run=source_run,
            parser_version=PARSER_VERSION,
            observations=[first_observation],
        )
    with pytest.raises(InvalidEarningsCalendarReplay, match="unique"):
        build_earnings_calendar_replay_input_digest(
            source_sync_run=source_run,
            parser_version=PARSER_VERSION,
            observations=[first_observation, first_observation, second_observation],
        )


@pytest.mark.django_db
def test_digest_evidence_identity_excludes_raw_record_uuid() -> None:
    _source_value, source_run, _records, observations = _observed_source("digest-row-id")

    item = replay_foundation._build_evidence_item(source_run, observations[0])

    assert "raw_data_record_id" not in item
    assert item["request_fingerprint"]
    assert item["content_hash"]


@pytest.mark.django_db
def test_digest_changes_with_payload_and_parser_contract_context() -> None:
    source, source_run, records, _observations = _observed_source("digest-change")
    original = build_earnings_calendar_replay_input_digest(
        source_sync_run=source_run,
        parser_version=PARSER_VERSION,
    )
    changed_parser = build_earnings_calendar_replay_input_digest(
        source_sync_run=source_run,
        parser_version="fixture-parser-v2",
    )
    changed_contract = build_earnings_calendar_replay_input_digest(
        source_sync_run=source_run,
        parser_version=PARSER_VERSION,
        replay_contract_version="2",
    )
    assert original != changed_parser
    assert original != changed_contract

    record = records[0]
    changed_payload = b'{"suffix":"changed"}'
    RawDataRecord.objects.filter(pk=record.pk).update(
        payload=changed_payload,
        payload_size_bytes=len(changed_payload),
        content_hash=hashlib.sha256(changed_payload).hexdigest(),
    )
    changed_payload_digest = build_earnings_calendar_replay_input_digest(
        source_sync_run=source_run,
        parser_version=PARSER_VERSION,
    )
    assert changed_payload_digest != original

    RawDataRecord.objects.filter(pk=record.pk).update(content_hash="c" * 64)
    with pytest.raises(InvalidEarningsCalendarReplay, match="hash"):
        build_earnings_calendar_replay_input_digest(
            source_sync_run=source_run,
            parser_version=PARSER_VERSION,
        )


@pytest.mark.django_db
def test_replay_identity_is_deterministic_and_context_sensitive() -> None:
    source, source_run, _records, _observations = _observed_source("identity")
    digest = build_earnings_calendar_replay_input_digest(
        source_sync_run=source_run,
        parser_version=PARSER_VERSION,
    )
    first = build_earnings_calendar_replay_idempotency_key(
        source=source,
        source_sync_run=source_run,
        replay_input_digest=digest,
        parser_version=PARSER_VERSION,
    )
    second = build_earnings_calendar_replay_idempotency_key(
        source=source,
        source_sync_run=source_run,
        replay_input_digest=digest,
        parser_version=PARSER_VERSION,
    )

    assert first == second
    assert first.startswith("earnings-calendar-replay:v1:")

    other_source, other_run, _other_records, _other_observations = _observed_source("other-run")
    assert first != build_earnings_calendar_replay_idempotency_key(
        source=other_source,
        source_sync_run=other_run,
        replay_input_digest=digest,
        parser_version=PARSER_VERSION,
    )
    assert first != build_earnings_calendar_replay_idempotency_key(
        source=source,
        source_sync_run=source_run,
        replay_input_digest="c" * 64,
        parser_version=PARSER_VERSION,
    )
    assert first != build_earnings_calendar_replay_idempotency_key(
        source=source,
        source_sync_run=source_run,
        replay_input_digest=digest,
        parser_version="fixture-parser-v2",
    )
    assert first != build_earnings_calendar_replay_idempotency_key(
        source=source,
        source_sync_run=source_run,
        replay_input_digest=digest,
        parser_version=PARSER_VERSION,
        replay_contract_version="2",
    )


@pytest.mark.django_db
def test_replay_start_links_source_and_keeps_network_fetch_count_zero() -> None:
    source, source_run, _records, _observations = _observed_source("start")
    first = start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=PARSER_VERSION,
    )

    assert first.created is True
    replay = first.sync_run
    assert replay.run_mode == SyncRun.RunMode.REPLAY
    assert replay.scope["window_kind"] == "replay"
    assert replay.replay_source_sync_run_id == source_run.pk
    assert replay.fetched_count == 0
    assert replay.replayed_count == 0

    with pytest.raises(EarningsCalendarRunBusy, match="already running"):
        start_earnings_calendar_replay_sync_run(
            source=source,
            source_sync_run=source_run,
            parser_version=PARSER_VERSION,
        )

    mark_sync_run_succeeded(replay.pk)
    replay.refresh_from_db()
    second = start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=PARSER_VERSION,
    )

    assert second.created is False
    assert second.sync_run.pk == replay.pk
    assert SyncRun.objects.filter(replay_source_sync_run_id=source_run.pk).count() == 1


@pytest.mark.django_db
def test_replay_start_uses_new_identity_for_parser_or_contract_revision() -> None:
    source, source_run, _records, _observations = _observed_source("revision")
    first = _replay_run(source, source_run)
    mark_sync_run_succeeded(first.pk)

    parser_revision = _replay_run(source, source_run, parser_version="fixture-parser-v2")
    mark_sync_run_succeeded(parser_revision.pk)

    contract_revision = _replay_run(
        source,
        source_run,
        replay_contract_version="2",
    )

    assert len({first.pk, parser_revision.pk, contract_revision.pk}) == 3
    assert SyncRun.objects.filter(replay_source_sync_run_id=source_run.pk).count() == 3


@pytest.mark.django_db
def test_replay_identity_unique_constraint_rejects_idempotency_key_bypass() -> None:
    source, source_run, _records, _observations = _observed_source("db-identity")
    replay = _replay_run(source, source_run)
    mark_sync_run_succeeded(replay.pk)
    replay_scope = dict(source_run.scope)
    replay_scope["window_kind"] = "replay"
    now = timezone.now()

    with pytest.raises(IntegrityError):
        SyncRun.objects.create(
            job_type=JOB_TYPE,
            source=source,
            scope=replay_scope,
            idempotency_key="manual-bypass-attempt",
            run_mode=SyncRun.RunMode.REPLAY,
            replay_source_sync_run=source_run,
            replay_contract_version=replay.replay_contract_version,
            replay_input_digest=replay.replay_input_digest,
            parser_version=replay.parser_version,
            started_at=now,
            heartbeat_at=now,
        )


@pytest.mark.django_db
def test_existing_replay_identity_fails_closed_on_context_mismatch() -> None:
    source, source_run, _records, _observations = _observed_source("mismatch")
    created = _replay_run(source, source_run)
    mark_sync_run_succeeded(created.pk)
    created.parser_version = "other-parser"
    created.save(update_fields=("parser_version",))

    with pytest.raises(EarningsCalendarReplayContextMismatch):
        start_earnings_calendar_replay_sync_run(
            source=source,
            source_sync_run=source_run,
            parser_version=PARSER_VERSION,
        )


@pytest.mark.django_db
def test_replay_count_reconciles_from_persisted_observations_only() -> None:
    source, source_run, records, _observations = _observed_source("count")
    replay = _replay_run(source, source_run)
    RawDataObservation.objects.create(sync_run=replay, raw_data_record=records[0])

    reconciled = reconcile_earnings_calendar_replayed_count(replay.pk)
    assert reconciled.replayed_count == 1
    assert reconciled.fetched_count == 0

    SyncRun.objects.filter(pk=replay.pk).update(replayed_count=2)
    with pytest.raises(EarningsCalendarReplayCountMismatch):
        reconcile_earnings_calendar_replayed_count(replay.pk)

    with pytest.raises(InvalidSyncRunCount, match="provider fetches"):
        update_sync_run_counts(replay.pk, fetched_delta=1)


@pytest.mark.django_db
def test_option_a_pool_contract_validates_persisted_values_without_selector() -> None:
    source, source_run, _records, _observations = _observed_source("pool")
    before = SyncRun.objects.filter(pk=source_run.pk).values().get()

    validate_earnings_calendar_replay_pool_contract(
        source_sync_run=source_run,
        monitoring_pool_as_of=POOL_AS_OF,
        monitoring_pool_hash=POOL_HASH,
        selector_version=SELECTOR_VERSION,
    )
    with pytest.raises(InvalidEarningsCalendarReplay, match="monitoring-pool"):
        validate_earnings_calendar_replay_pool_contract(
            source_sync_run=source_run,
            monitoring_pool_as_of=POOL_AS_OF,
            monitoring_pool_hash="d" * 64,
            selector_version=SELECTOR_VERSION,
        )
    with pytest.raises(InvalidEarningsCalendarReplay, match="monitoring-pool"):
        validate_earnings_calendar_replay_pool_contract(
            source_sync_run=source_run,
            monitoring_pool_as_of=POOL_AS_OF,
            monitoring_pool_hash="bad-hash",
            selector_version=SELECTOR_VERSION,
        )

    assert SyncRun.objects.filter(pk=source_run.pk).values().get() == before
    assert SyncRun.objects.filter(run_mode=SyncRun.RunMode.REPLAY).count() == 0


@pytest.mark.django_db(transaction=True)
def test_replay_start_rejects_when_ingestion_owns_the_same_lock_domain() -> None:
    source, source_run, _records, _observations = _observed_source("lock-ingestion")
    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []

    def hold_ingestion_lock() -> None:
        with calendar_run_ownership(source_id=source.pk, job_type=JOB_TYPE):
            entered.set()
            assert release.wait(timeout=10)

    thread = threading.Thread(target=_in_thread, args=(hold_ingestion_lock, results))
    thread.start()
    try:
        assert entered.wait(timeout=10)
        with pytest.raises(EarningsCalendarRunBusy):
            start_earnings_calendar_replay_sync_run(
                source=source,
                source_sync_run=source_run,
                parser_version=PARSER_VERSION,
            )
        assert not SyncRun.objects.filter(run_mode=SyncRun.RunMode.REPLAY).exists()
    finally:
        release.set()
        thread.join(timeout=15)

    assert not thread.is_alive()
    assert results == [None]


@pytest.mark.django_db(transaction=True)
def test_scheduled_start_rejects_a_fresh_running_replay_run() -> None:
    source, source_run, _records, _observations = _observed_source("reverse-lock")
    replay = _replay_run(source, source_run)
    page_source = _NoFetchPageSource()
    assert isinstance(page_source, EarningsCalendarPageSource)

    with pytest.raises(EarningsCalendarRunBusy):
        execute_scheduled_earnings_calendar_window(
            source=source,
            page_source=page_source,
            parser=FixtureEarningsCalendarParser(),
            provider_key=PROVIDER_KEY,
            provider_version="fixture-provider-v1",
            window_start=date(2026, 9, 1),
            window_end=date(2026, 12, 20),
            monitoring_pool_as_of=POOL_AS_OF,
            monitoring_pool_hash=POOL_HASH,
            selector_version=SELECTOR_VERSION,
            schedule_bucket="fixture-bucket",
        )

    assert page_source.calls == []
    assert SyncRun.objects.filter(pk=replay.pk).exists()
    assert SyncRun.objects.filter(status=SyncRun.Status.SUCCEEDED).count() == 1


@pytest.mark.django_db(transaction=True)
def test_replay_observation_supports_append_only_parse_attempt_versions() -> None:
    source, source_run, records, _observations = _observed_source("parse")
    replay = _replay_run(source, source_run)
    replay_observation = RawDataObservation.objects.create(
        sync_run=replay,
        raw_data_record=records[0],
    )
    started = timezone.now()
    finished = started + timedelta(seconds=1)

    with transaction.atomic():
        first = record_raw_data_parse_attempt(
            observation=replay_observation,
            parser_version="parser-v1",
            status=RawDataParseAttempt.Status.SUCCEEDED,
            error_summary="",
            started_at=started,
            finished_at=finished,
        )
    with transaction.atomic():
        second = record_raw_data_parse_attempt(
            observation=replay_observation,
            parser_version="parser-v2",
            status=RawDataParseAttempt.Status.DATA_ERROR,
            error_summary="Fixture parser revision failure.",
            started_at=started,
            finished_at=finished,
        )

    assert first.created is True
    assert second.created is True
    assert RawDataParseAttempt.objects.filter(observation=replay_observation).count() == 2
    first.attempt.refresh_from_db()
    assert first.attempt.status == RawDataParseAttempt.Status.SUCCEEDED
    assert replay_observation.sync_run_id == replay.pk


@pytest.mark.django_db
def test_normalized_observation_supports_parser_revision_rows() -> None:
    source, source_run, records, _observations = _observed_source("normalized")
    first = make_calendar_observation(
        source=source,
        raw_data_record=records[0],
        parser_version="parser-v1",
        provider_event_id="evt-revision",
    )
    second = make_calendar_observation(
        source=source,
        raw_data_record=records[0],
        parser_version="parser-v2",
        provider_event_id="evt-revision",
    )

    assert first.pk != second.pk
    assert EarningsCalendarObservation.objects.filter(raw_data_record=records[0]).count() == 2


@pytest.mark.django_db
def test_stale_replay_recovery_preserves_source_and_rebuilds_count() -> None:
    source, source_run, records, _observations = _observed_source("stale")
    started_at = timezone.now() - timedelta(hours=2)
    replay = _replay_run(source, source_run, started_at=started_at)
    RawDataObservation.objects.create(sync_run=replay, raw_data_record=records[0])
    source_before = SyncRun.objects.filter(pk=source_run.pk).values().get()

    with calendar_run_ownership(source_id=source.pk, job_type=JOB_TYPE):
        recovered = retire_stale_earnings_calendar_replay_run(
            replay,
            cutoff=timezone.now() - timedelta(minutes=1),
        )

    assert recovered.status == SyncRun.Status.PARTIAL
    assert recovered.replayed_count == 1
    assert recovered.failed_count == 1
    assert recovered.fetched_count == 0
    assert SyncRun.objects.filter(pk=source_run.pk).values().get() == source_before


@pytest.mark.django_db
def test_stale_replay_recovery_fails_closed_on_impossible_count() -> None:
    source, source_run, records, _observations = _observed_source("stale-count")
    replay = _replay_run(
        source,
        source_run,
        started_at=timezone.now() - timedelta(hours=2),
    )
    RawDataObservation.objects.create(sync_run=replay, raw_data_record=records[0])
    SyncRun.objects.filter(pk=replay.pk).update(replayed_count=2)

    with calendar_run_ownership(source_id=source.pk, job_type=JOB_TYPE):
        with pytest.raises(EarningsCalendarReplayCountMismatch):
            retire_stale_earnings_calendar_replay_run(
                replay,
                cutoff=timezone.now() - timedelta(minutes=1),
            )


@pytest.mark.django_db
def test_stale_replay_recovery_requires_matching_ownership() -> None:
    source, source_run, _records, _observations = _observed_source("stale-no-lock")
    replay = _replay_run(
        source,
        source_run,
        started_at=timezone.now() - timedelta(hours=2),
    )

    with pytest.raises(EarningsCalendarRunOwnershipLost):
        retire_stale_earnings_calendar_replay_run(
            replay,
            cutoff=timezone.now() - timedelta(minutes=1),
        )

    replay.refresh_from_db()
    assert replay.status == SyncRun.Status.RUNNING
