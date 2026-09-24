from __future__ import annotations

import http.client
import socket
import threading
import uuid
from collections.abc import Callable
from datetime import date, timedelta

import pytest
from django.db import close_old_connections, connection, connections, transaction
from django.utils import timezone

from audit.models import (
    DataSource,
    RawDataObservation,
    RawDataParseAttempt,
    SyncRun,
)
from audit.services import (
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    record_raw_data_parse_attempt,
    start_sync_run,
    start_sync_run_with_result,
    update_sync_run_counts,
)
from audit.services.raw_data import record_replay_raw_data_observation
from earnings.calendar_parsing import (
    EarningsCalendarParseResult,
    FixtureEarningsCalendarParser,
)
from earnings.models import EarningsCalendarObservation
from earnings.services import (
    EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
    EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    EarningsCalendarPayloadParseFailure,
    EarningsCalendarReplayContextMismatch,
    EarningsCalendarReplayCountMismatch,
    EarningsCalendarReplayPersistenceFailure,
    EarningsCalendarRunBusy,
    EarningsCalendarRunOwnershipLost,
    build_earnings_calendar_replay_idempotency_key,
    build_earnings_calendar_replay_input_digest,
    build_earnings_calendar_sync_scope,
    calendar_run_ownership,
    execute_earnings_calendar_offline_replay,
    execute_scheduled_earnings_calendar_window,
    ingest_earnings_calendar_payload,
    reconcile_earnings_calendar_replayed_count,
    start_earnings_calendar_replay_sync_run,
)
from earnings.services import calendar_replay_orchestration as replay_orchestration
from earnings.services.calendar import EarningsCalendarObservationIntegrityError
from earnings.services.calendar_ingestion import persist_earnings_calendar_parse_result
from tests.earnings.test_calendar_pagination import (
    FETCHED_AT,
    PROVIDER_KEY,
    PROVIDER_VERSION,
    FixturePageSource,
    _calendar_source,
    _fixture_bytes,
    _page,
)

WINDOW_START = date(2026, 9, 1)
WINDOW_END = date(2026, 12, 20)
POOL_AS_OF = date(2026, 9, 21)
POOL_HASH = "a" * 64
SELECTOR_VERSION = "earnings-monitoring-pool-v1"
SCHEDULE_BUCKET = "2026-09-21T13:00:00Z"


class RevisionFixtureParser(FixtureEarningsCalendarParser):
    parser_version = "fixture-earnings-calendar-parser-v2"


class ClosingConnectionParser(FixtureEarningsCalendarParser):
    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> EarningsCalendarParseResult:
        result = super().parse(
            raw_content,
            provider_key=provider_key,
            provider_version=provider_version,
        )
        connection.close()
        return result


def _scope() -> dict[str, object]:
    return build_earnings_calendar_sync_scope(
        provider_key=PROVIDER_KEY,
        window_kind="scheduled",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        monitoring_pool_as_of=POOL_AS_OF,
        monitoring_pool_hash=POOL_HASH,
        selector_version=SELECTOR_VERSION,
    )


def _source_run_with_payloads(
    payloads: tuple[bytes, ...],
    *,
    status: str = SyncRun.Status.SUCCEEDED,
) -> tuple[DataSource, SyncRun]:
    source = _calendar_source(f"orchestration-{uuid.uuid4().hex[:8]}")
    sync_run = start_sync_run(
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        source=source,
        scope=_scope(),
        idempotency_key=f"orchestration-source:{uuid.uuid4()}",
        provider_version=PROVIDER_VERSION,
        parser_version=FixtureEarningsCalendarParser.parser_version,
    )
    for index, payload in enumerate(payloads):
        ingest_earnings_calendar_payload(
            sync_run=sync_run,
            parser=FixtureEarningsCalendarParser(),
            raw_content=payload,
            provider_key=PROVIDER_KEY,
            provider_version=PROVIDER_VERSION,
            source_url=f"https://fixture-earnings-calendar.test/calendar/{index}",
            fetched_at=FETCHED_AT,
        )
        update_sync_run_counts(sync_run.pk, fetched_delta=1)
    if status == SyncRun.Status.SUCCEEDED:
        mark_sync_run_succeeded(sync_run.pk)
    elif status == SyncRun.Status.PARTIAL:
        mark_sync_run_partial(sync_run.pk, error_summary="Fixture source partial.")
    elif status == SyncRun.Status.FAILED:
        mark_sync_run_failed(sync_run.pk, error_summary="Fixture source failed.")
    sync_run.refresh_from_db()
    return source, sync_run


def _source_run_with_parser_failure() -> tuple[DataSource, SyncRun]:
    source = _calendar_source(f"orchestration-failure-{uuid.uuid4().hex[:8]}")
    sync_run = start_sync_run(
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        source=source,
        scope=_scope(),
        idempotency_key=f"orchestration-source:{uuid.uuid4()}",
        provider_version=PROVIDER_VERSION,
        parser_version=FixtureEarningsCalendarParser.parser_version,
    )
    with pytest.raises(EarningsCalendarPayloadParseFailure):
        ingest_earnings_calendar_payload(
            sync_run=sync_run,
            parser=FixtureEarningsCalendarParser(),
            raw_content=b"{invalid-json",
            provider_key=PROVIDER_KEY,
            provider_version=PROVIDER_VERSION,
            source_url="https://fixture-earnings-calendar.test/calendar/malformed",
            fetched_at=FETCHED_AT,
        )
    update_sync_run_counts(sync_run.pk, fetched_delta=1)
    mark_sync_run_partial(sync_run.pk, error_summary="Fixture source parser failure.")
    sync_run.refresh_from_db()
    return source, sync_run


def _in_thread(operation: Callable[[], object], results: list[object]) -> None:
    close_old_connections()
    try:
        results.append(operation())
    except Exception as error:
        results.append(error)
    finally:
        for database_connection in connections.all():
            database_connection.close()


def _fail_network(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise AssertionError("Offline replay must not access the network.")


@pytest.mark.django_db(transaction=True)
def test_offline_replay_success_reuses_persisted_raw_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    source_before = SyncRun.objects.filter(pk=source_run.pk).values().get()
    source_observation = RawDataObservation.objects.get(sync_run=source_run)
    monkeypatch.setattr(socket, "create_connection", _fail_network)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _fail_network)

    result = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )

    replay = result.sync_run
    assert result.executed is True
    assert result.parse_failures == 0
    assert replay.pk != source_run.pk
    assert replay.status == SyncRun.Status.SUCCEEDED
    assert replay.run_mode == SyncRun.RunMode.REPLAY
    assert replay.provider_version == source_run.provider_version
    assert replay.fetched_count == 0
    assert replay.replayed_count == 1
    replay_observation = RawDataObservation.objects.get(sync_run=replay)
    assert replay_observation.raw_data_record_id == source_observation.raw_data_record_id
    assert RawDataParseAttempt.objects.filter(observation=replay_observation).count() == 1
    assert EarningsCalendarObservation.objects.count() == 2
    assert SyncRun.objects.filter(pk=source_run.pk).values().get() == source_before


@pytest.mark.django_db(transaction=True)
def test_offline_replay_same_identity_reuses_terminal_result() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))

    first = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )
    second = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )

    assert second.executed is False
    assert second.sync_run.pk == first.sync_run.pk
    assert RawDataObservation.objects.filter(sync_run=first.sync_run).count() == 1
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.parametrize("status", (SyncRun.Status.PARTIAL, SyncRun.Status.FAILED))
@pytest.mark.django_db(transaction=True)
def test_offline_replay_reuses_terminal_failed_or_partial_identity(status: str) -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    replay = start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=FixtureEarningsCalendarParser.parser_version,
    ).sync_run
    if status == SyncRun.Status.PARTIAL:
        mark_sync_run_partial(replay.pk, error_summary="Fixture replay partial.")
    else:
        mark_sync_run_failed(replay.pk, error_summary="Fixture replay failed.")

    result = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )

    assert result.executed is False
    assert result.sync_run.pk == replay.pk
    assert result.sync_run.status == status
    assert RawDataObservation.objects.filter(sync_run=replay).count() == 0
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_offline_replay_empty_calendar_succeeds_with_provider_context() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("empty_payload.json"),))

    result = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )

    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.provider_version == PROVIDER_VERSION
    assert result.sync_run.replayed_count == 1
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_offline_replay_parser_failure_is_partial_without_normalized_rows() -> None:
    source, source_run = _source_run_with_parser_failure()
    source_attempt = RawDataParseAttempt.objects.get()

    result = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )

    assert result.sync_run.status == SyncRun.Status.PARTIAL
    assert result.parse_failures == 1
    assert result.sync_run.replayed_count == 1
    assert result.sync_run.failed_count == 1
    assert EarningsCalendarObservation.objects.count() == 0
    source_attempt.refresh_from_db()
    assert source_attempt.status == RawDataParseAttempt.Status.DATA_ERROR
    replay_observation = RawDataObservation.objects.get(sync_run=result.sync_run)
    replay_attempt = RawDataParseAttempt.objects.get(observation=replay_observation)
    assert replay_attempt.status == RawDataParseAttempt.Status.DATA_ERROR


@pytest.mark.django_db(transaction=True)
def test_replay_of_partial_source_remains_partial_after_successful_evidence_replay() -> None:
    source, source_run = _source_run_with_payloads(
        (_fixture_bytes("complete_payload.json"),),
        status=SyncRun.Status.PARTIAL,
    )

    result = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )

    assert result.sync_run.status == SyncRun.Status.PARTIAL
    assert result.parse_failures == 0
    assert result.sync_run.replayed_count == 1
    assert result.sync_run.fetched_count == 0


@pytest.mark.django_db(transaction=True)
def test_offline_replay_parser_revision_appends_normalized_rows() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))

    first = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )
    second = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=RevisionFixtureParser(),
    )

    assert first.sync_run.pk != second.sync_run.pk
    assert second.sync_run.parser_version == RevisionFixtureParser.parser_version
    assert EarningsCalendarObservation.objects.count() == 4
    assert (
        EarningsCalendarObservation.objects.filter(
            parser_version=RevisionFixtureParser.parser_version
        ).count()
        == 2
    )


@pytest.mark.django_db(transaction=True)
def test_offline_replay_rejects_fresh_running_identity() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    started = start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=FixtureEarningsCalendarParser.parser_version,
    )

    with pytest.raises(EarningsCalendarRunBusy):
        execute_earnings_calendar_offline_replay(
            source=source,
            source_sync_run=source_run,
            parser=FixtureEarningsCalendarParser(),
        )

    assert RawDataObservation.objects.filter(sync_run=started.sync_run).count() == 0


@pytest.mark.django_db(transaction=True)
def test_offline_replay_context_mismatch_fails_before_observation_writes() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    parser_version = FixtureEarningsCalendarParser.parser_version
    digest = build_earnings_calendar_replay_input_digest(
        source_sync_run=source_run,
        parser_version=parser_version,
    )
    idempotency_key = build_earnings_calendar_replay_idempotency_key(
        source=source,
        source_sync_run=source_run,
        replay_input_digest=digest,
        parser_version=parser_version,
    )
    replay_scope = dict(source_run.scope)
    replay_scope["window_kind"] = "replay"
    start_sync_run_with_result(
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        source=source,
        scope=replay_scope,
        idempotency_key=idempotency_key,
        parser_version=parser_version,
        run_mode=SyncRun.RunMode.REPLAY,
        replay_source_sync_run=source_run,
        replay_contract_version=EARNINGS_CALENDAR_REPLAY_CONTRACT_VERSION,
        replay_input_digest="f" * 64,
        provider_version=source_run.provider_version,
    )

    with pytest.raises(EarningsCalendarReplayContextMismatch):
        execute_earnings_calendar_offline_replay(
            source=source,
            source_sync_run=source_run,
            parser=FixtureEarningsCalendarParser(),
        )

    assert RawDataObservation.objects.filter(sync_run__run_mode=SyncRun.RunMode.REPLAY).count() == 0


@pytest.mark.django_db(transaction=True)
def test_offline_replay_source_count_mismatch_fails_before_run_creation() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    SyncRun.objects.filter(pk=source_run.pk).update(fetched_count=0)

    with pytest.raises(EarningsCalendarReplayCountMismatch):
        execute_earnings_calendar_offline_replay(
            source=source,
            source_sync_run=source_run,
            parser=FixtureEarningsCalendarParser(),
        )

    assert not SyncRun.objects.filter(run_mode=SyncRun.RunMode.REPLAY).exists()


@pytest.mark.django_db(transaction=True)
def test_stale_replay_resumes_after_observation_before_parse() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    replay = start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=FixtureEarningsCalendarParser.parser_version,
        started_at=timezone.now() - timedelta(hours=2),
    ).sync_run
    source_observation = RawDataObservation.objects.get(sync_run=source_run)
    record_replay_raw_data_observation(
        sync_run=replay,
        raw_data_record=source_observation.raw_data_record,
    )

    result = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )

    assert result.sync_run.pk == replay.pk
    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert RawDataObservation.objects.filter(sync_run=replay).count() == 1
    assert RawDataParseAttempt.objects.filter(observation__sync_run=replay).count() == 1
    assert result.sync_run.replayed_count == 1


@pytest.mark.django_db(transaction=True)
def test_stale_replay_resumes_after_parse_before_normalized_persistence() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    replay = start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=FixtureEarningsCalendarParser.parser_version,
        started_at=timezone.now() - timedelta(hours=2),
    ).sync_run
    source_observation = RawDataObservation.objects.get(sync_run=source_run)
    replay_observation = record_replay_raw_data_observation(
        sync_run=replay,
        raw_data_record=source_observation.raw_data_record,
    ).observation
    started_at = timezone.now()
    with transaction.atomic():
        record_raw_data_parse_attempt(
            observation=replay_observation,
            parser_version=FixtureEarningsCalendarParser.parser_version,
            status=RawDataParseAttempt.Status.SUCCEEDED,
            error_summary="",
            started_at=started_at,
            finished_at=started_at,
        )

    result = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )

    assert result.sync_run.pk == replay.pk
    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert RawDataObservation.objects.filter(sync_run=replay).count() == 1
    assert RawDataParseAttempt.objects.filter(observation=replay_observation).count() == 1
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_stale_replay_resumes_after_normalized_persistence_before_count_reconcile() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    replay = start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=FixtureEarningsCalendarParser.parser_version,
        started_at=timezone.now() - timedelta(hours=2),
    ).sync_run
    source_observation = RawDataObservation.objects.get(sync_run=source_run)
    raw_record = source_observation.raw_data_record
    replay_observation = record_replay_raw_data_observation(
        sync_run=replay,
        raw_data_record=raw_record,
    ).observation
    parse_result = FixtureEarningsCalendarParser().parse(
        bytes(raw_record.payload),
        provider_key=PROVIDER_KEY,
        provider_version=PROVIDER_VERSION,
    )
    started_at = timezone.now()
    with transaction.atomic():
        record_raw_data_parse_attempt(
            observation=replay_observation,
            parser_version=FixtureEarningsCalendarParser.parser_version,
            status=RawDataParseAttempt.Status.SUCCEEDED,
            error_summary="",
            started_at=started_at,
            finished_at=started_at,
        )
    persist_earnings_calendar_parse_result(
        source=source,
        provider_key=PROVIDER_KEY,
        provider_version=PROVIDER_VERSION,
        parser_version=FixtureEarningsCalendarParser.parser_version,
        raw_record=raw_record,
        parse_result=parse_result,
    )

    result = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )

    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.replayed_count == 1
    assert RawDataObservation.objects.filter(sync_run=replay).count() == 1
    assert RawDataParseAttempt.objects.filter(observation=replay_observation).count() == 1
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_stale_replay_resumes_after_count_reconcile_before_finalize() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    replay = start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=FixtureEarningsCalendarParser.parser_version,
        started_at=timezone.now() - timedelta(hours=2),
    ).sync_run
    source_observation = RawDataObservation.objects.get(sync_run=source_run)
    raw_record = source_observation.raw_data_record
    replay_observation = record_replay_raw_data_observation(
        sync_run=replay,
        raw_data_record=raw_record,
    ).observation
    parse_result = FixtureEarningsCalendarParser().parse(
        bytes(raw_record.payload),
        provider_key=PROVIDER_KEY,
        provider_version=PROVIDER_VERSION,
    )
    started_at = timezone.now()
    with transaction.atomic():
        record_raw_data_parse_attempt(
            observation=replay_observation,
            parser_version=FixtureEarningsCalendarParser.parser_version,
            status=RawDataParseAttempt.Status.SUCCEEDED,
            error_summary="",
            started_at=started_at,
            finished_at=started_at,
        )
    persist_earnings_calendar_parse_result(
        source=source,
        provider_key=PROVIDER_KEY,
        provider_version=PROVIDER_VERSION,
        parser_version=FixtureEarningsCalendarParser.parser_version,
        raw_record=raw_record,
        parse_result=parse_result,
    )
    reconcile_earnings_calendar_replayed_count(replay.pk)

    result = execute_earnings_calendar_offline_replay(
        source=source,
        source_sync_run=source_run,
        parser=FixtureEarningsCalendarParser(),
    )

    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.replayed_count == 1
    assert RawDataObservation.objects.filter(sync_run=replay).count() == 1
    assert RawDataParseAttempt.objects.filter(observation=replay_observation).count() == 1
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_offline_replay_ownership_loss_stops_before_parse_persistence() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))

    with pytest.raises(EarningsCalendarRunOwnershipLost):
        execute_earnings_calendar_offline_replay(
            source=source,
            source_sync_run=source_run,
            parser=ClosingConnectionParser(),
        )

    replay = SyncRun.objects.get(run_mode=SyncRun.RunMode.REPLAY)
    assert replay.status == SyncRun.Status.RUNNING
    replay_observation = RawDataObservation.objects.get(sync_run=replay)
    assert RawDataParseAttempt.objects.filter(observation=replay_observation).count() == 0
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_offline_replay_persistence_failure_is_partial_and_preserves_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))

    def fail_persistence(**kwargs: object) -> None:
        del kwargs
        raise EarningsCalendarObservationIntegrityError("Fixture replay persistence failure.")

    monkeypatch.setattr(
        replay_orchestration,
        "persist_earnings_calendar_parse_result",
        fail_persistence,
    )

    with pytest.raises(EarningsCalendarReplayPersistenceFailure) as excinfo:
        execute_earnings_calendar_offline_replay(
            source=source,
            source_sync_run=source_run,
            parser=FixtureEarningsCalendarParser(),
        )

    replay = excinfo.value.sync_run
    assert replay.status == SyncRun.Status.PARTIAL
    assert replay.replayed_count == 1
    replay_observation = RawDataObservation.objects.get(sync_run=replay)
    assert RawDataParseAttempt.objects.filter(observation=replay_observation).count() == 1
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_offline_replay_rejects_when_ingestion_owns_same_lock_domain() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []

    def hold_lock() -> None:
        with calendar_run_ownership(
            source_id=source.pk,
            job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        ):
            entered.set()
            assert release.wait(timeout=10)

    thread = threading.Thread(target=_in_thread, args=(hold_lock, results))
    thread.start()
    try:
        assert entered.wait(timeout=10)
        with pytest.raises(EarningsCalendarRunBusy):
            execute_earnings_calendar_offline_replay(
                source=source,
                source_sync_run=source_run,
                parser=FixtureEarningsCalendarParser(),
            )
        assert not SyncRun.objects.filter(run_mode=SyncRun.RunMode.REPLAY).exists()
    finally:
        release.set()
        thread.join(timeout=15)

    assert not thread.is_alive()
    assert results == [None]


@pytest.mark.django_db(transaction=True)
def test_scheduled_ingestion_rejects_fresh_running_replay_run() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    replay = start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=FixtureEarningsCalendarParser.parser_version,
    ).sync_run
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("empty_payload.json"),
                next_cursor=None,
                is_terminal=True,
            )
        }
    )

    with pytest.raises(EarningsCalendarRunBusy):
        execute_scheduled_earnings_calendar_window(
            source=source,
            page_source=page_source,
            parser=FixtureEarningsCalendarParser(),
            provider_key=PROVIDER_KEY,
            provider_version=PROVIDER_VERSION,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            monitoring_pool_as_of=POOL_AS_OF,
            monitoring_pool_hash=POOL_HASH,
            selector_version=SELECTOR_VERSION,
            schedule_bucket=SCHEDULE_BUCKET,
        )

    assert page_source.calls == []
    assert SyncRun.objects.get(pk=replay.pk).status == SyncRun.Status.RUNNING


@pytest.mark.django_db(transaction=True)
def test_scheduled_stale_gate_terminalizes_replay_with_replay_semantics() -> None:
    source, source_run = _source_run_with_payloads((_fixture_bytes("complete_payload.json"),))
    replay = start_earnings_calendar_replay_sync_run(
        source=source,
        source_sync_run=source_run,
        parser_version=FixtureEarningsCalendarParser.parser_version,
        started_at=timezone.now() - timedelta(hours=2),
    ).sync_run
    source_observation = RawDataObservation.objects.get(sync_run=source_run)
    record_replay_raw_data_observation(
        sync_run=replay,
        raw_data_record=source_observation.raw_data_record,
    )
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("empty_payload.json"),
                next_cursor=None,
                is_terminal=True,
            )
        }
    )

    scheduled = execute_scheduled_earnings_calendar_window(
        source=source,
        page_source=page_source,
        parser=FixtureEarningsCalendarParser(),
        provider_key=PROVIDER_KEY,
        provider_version=PROVIDER_VERSION,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        monitoring_pool_as_of=POOL_AS_OF,
        monitoring_pool_hash=POOL_HASH,
        selector_version=SELECTOR_VERSION,
        schedule_bucket=SCHEDULE_BUCKET,
    )

    replay.refresh_from_db()
    assert replay.status == SyncRun.Status.PARTIAL
    assert replay.fetched_count == 0
    assert replay.replayed_count == 1
    assert replay.failed_count == 1
    assert scheduled.sync_run.status == SyncRun.Status.SUCCEEDED
