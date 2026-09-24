"""PostgreSQL ownership tests for one earnings-calendar logical window."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from django.db import close_old_connections, connection, connections, transaction
from django.test import override_settings
from django.utils import timezone

from audit.models import (
    DataSource,
    RawDataObservation,
    RawDataParseAttempt,
    RawDataRecord,
    SyncRun,
)
from audit.services import (
    InvalidSyncRunTransition,
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    record_raw_data_observation,
    update_sync_run_counts,
)
from earnings.calendar_parsing import (
    EarningsCalendarParseResult,
    EarningsCalendarPayloadError,
    FixtureEarningsCalendarParser,
)
from earnings.models import (
    EarningsCalendarObservation,
    EarningsEvent,
    EarningsReconciliationDecision,
)
from earnings.services import (
    EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    EarningsCalendarExecutionResult,
    EarningsCalendarPage,
    EarningsCalendarRetryContextMismatch,
    EarningsCalendarRunBusy,
    EarningsCalendarRunCountMismatch,
    EarningsCalendarRunOwnershipLost,
    EarningsCalendarSyncRunRetryRequired,
    EarningsCalendarWindowFailure,
    EarningsCalendarWindowResult,
    InvalidEarningsCalendarSyncIdentity,
    calendar_run_ownership,
    execute_retry_earnings_calendar_window,
    execute_scheduled_earnings_calendar_window,
    run_earnings_calendar_window,
    start_scheduled_earnings_calendar_sync_run,
)
from tests.earnings.test_calendar_pagination import (
    PROVIDER_KEY,
    PROVIDER_VERSION,
    FixturePageSource,
    _calendar_source,
    _fixture_bytes,
    _page,
    _window_run,
)

POOL_HASH = "a" * 64
WINDOW_START = date(2026, 1, 1)
WINDOW_END = date(2026, 1, 10)
POOL_AS_OF = date(2025, 12, 31)
SELECTOR_VERSION = "fixture-monitoring-pool-v1"


def _terminal_page() -> EarningsCalendarPage:
    return _page(
        cursor=None,
        payload=_fixture_bytes("empty_payload.json"),
        next_cursor=None,
        is_terminal=True,
    )


def _execute_scheduled(
    source: DataSource,
    page_source: FixturePageSource,
    *,
    bucket: str = "fixture-bucket",
    window_start: date = WINDOW_START,
    window_end: date = WINDOW_END,
) -> EarningsCalendarExecutionResult:
    return execute_scheduled_earnings_calendar_window(
        source=source,
        page_source=page_source,
        parser=FixtureEarningsCalendarParser(),
        provider_key=PROVIDER_KEY,
        provider_version=PROVIDER_VERSION,
        window_start=window_start,
        window_end=window_end,
        monitoring_pool_as_of=POOL_AS_OF,
        monitoring_pool_hash=POOL_HASH,
        selector_version=SELECTOR_VERSION,
        schedule_bucket=bucket,
    )


def _start_scheduled(source: DataSource, *, started_at: datetime | None = None) -> SyncRun:
    return start_scheduled_earnings_calendar_sync_run(
        source=source,
        provider_key=PROVIDER_KEY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        monitoring_pool_as_of=POOL_AS_OF,
        monitoring_pool_hash=POOL_HASH,
        selector_version=SELECTOR_VERSION,
        schedule_bucket="fixture-bucket",
        provider_version=PROVIDER_VERSION,
        parser_version=FixtureEarningsCalendarParser.parser_version,
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


@pytest.mark.django_db(transaction=True)
def test_same_sync_run_cannot_execute_twice_concurrently() -> None:
    source = _calendar_source("double-execution")
    sync_run = _window_run(source, "double-execution")
    entered_fetch = threading.Event()
    release_fetch = threading.Event()
    page = _page(
        cursor=None,
        payload=_fixture_bytes("empty_payload.json"),
        next_cursor=None,
        is_terminal=True,
    )

    class BlockingPageSource(FixturePageSource):
        def fetch_page(self, cursor: str | None) -> EarningsCalendarPage:
            entered_fetch.set()
            assert release_fetch.wait(timeout=10)
            return super().fetch_page(cursor)

    def execute(page_source: FixturePageSource) -> EarningsCalendarWindowResult:
        return run_earnings_calendar_window(
            sync_run=sync_run,
            page_source=page_source,
            parser=FixtureEarningsCalendarParser(),
            provider_key=PROVIDER_KEY,
            provider_version=PROVIDER_VERSION,
        )

    first_results: list[object] = []
    second_results: list[object] = []
    first = threading.Thread(
        target=_in_thread,
        args=(lambda: execute(BlockingPageSource({None: page})), first_results),
    )
    second = threading.Thread(
        target=_in_thread,
        args=(lambda: execute(FixturePageSource({None: page})), second_results),
    )
    first.start()
    try:
        assert entered_fetch.wait(timeout=10)
        second.start()
        second.join(timeout=10)
        assert not second.is_alive()
    finally:
        release_fetch.set()
        first.join(timeout=15)

    assert not first.is_alive()
    assert len(first_results) == len(second_results) == 1
    assert isinstance(first_results[0], EarningsCalendarWindowResult)
    assert type(second_results[0]).__name__ == "EarningsCalendarRunBusy"
    final = SyncRun.objects.get(pk=sync_run.pk)
    assert final.status == SyncRun.Status.SUCCEEDED
    assert final.fetched_count == 1
    assert RawDataObservation.objects.filter(sync_run=sync_run).count() == 1
    assert first_results[0].observation_count == 0
    assert first_results[0].pages[0].ingestion.raw_observation_created is True


@pytest.mark.parametrize(
    ("second_start", "second_end"),
    (
        (WINDOW_START, WINDOW_END),
        (date(2026, 1, 5), date(2026, 1, 15)),
        (date(2026, 1, 20), date(2026, 1, 25)),
    ),
)
@pytest.mark.django_db(transaction=True)
def test_scheduled_same_source_is_serial_for_same_overlapping_and_disjoint_windows(
    second_start: date, second_end: date
) -> None:
    source = _calendar_source(f"serial-{second_start}-{second_end}")
    entered_fetch = threading.Event()
    release_fetch = threading.Event()
    page = _terminal_page()

    class BlockingPageSource(FixturePageSource):
        def fetch_page(self, cursor: str | None) -> EarningsCalendarPage:
            entered_fetch.set()
            assert release_fetch.wait(timeout=10)
            return super().fetch_page(cursor)

    first_results: list[object] = []
    first = threading.Thread(
        target=_in_thread,
        args=(lambda: _execute_scheduled(source, BlockingPageSource({None: page})), first_results),
    )
    first.start()
    try:
        assert entered_fetch.wait(timeout=10)
        with pytest.raises(EarningsCalendarRunBusy):
            _execute_scheduled(
                source,
                FixturePageSource({None: page}),
                bucket="fixture-bucket" if second_start == WINDOW_START else "second-bucket",
                window_start=second_start,
                window_end=second_end,
            )
        assert SyncRun.objects.count() == 1
    finally:
        release_fetch.set()
        first.join(timeout=15)

    assert not first.is_alive()
    assert len(first_results) == 1
    assert not isinstance(first_results[0], Exception)
    assert SyncRun.objects.get().status == SyncRun.Status.SUCCEEDED


@pytest.mark.parametrize("different_job", (False, True))
@pytest.mark.django_db(transaction=True)
def test_different_source_or_job_type_can_acquire_in_parallel(different_job: bool) -> None:
    first_source = _calendar_source(f"domain-a-{different_job}")
    second_source = _calendar_source("domain-b")
    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []

    def hold_first() -> None:
        with calendar_run_ownership(
            source_id=first_source.pk, job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE
        ):
            entered.set()
            assert release.wait(timeout=10)

    thread = threading.Thread(target=_in_thread, args=(hold_first, results))
    thread.start()
    try:
        assert entered.wait(timeout=10)
        with calendar_run_ownership(
            source_id=first_source.pk if different_job else second_source.pk,
            job_type="fixture.other_job" if different_job else EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        ):
            pass
    finally:
        release.set()
        thread.join(timeout=15)

    assert not thread.is_alive()
    assert results == [None]


@pytest.mark.django_db(transaction=True)
def test_session_lock_survives_rollback_and_releases_after_exception() -> None:
    source = _calendar_source("rollback-lock")
    results: list[object] = []

    with calendar_run_ownership(source_id=source.pk, job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE):
        with pytest.raises(RuntimeError, match="fixture rollback"):
            with transaction.atomic():
                raise RuntimeError("fixture rollback")
        contender = threading.Thread(
            target=_in_thread,
            args=(
                lambda: _try_own_calendar_run(source),
                results,
            ),
        )
        contender.start()
        contender.join(timeout=10)
        assert not contender.is_alive()
        assert len(results) == 1
        assert isinstance(results[0], EarningsCalendarRunBusy)

    with calendar_run_ownership(source_id=source.pk, job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE):
        pass


def _try_own_calendar_run(source: DataSource) -> None:
    with calendar_run_ownership(source_id=source.pk, job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE):
        pass


@pytest.mark.django_db(transaction=True)
def test_succeeded_scheduled_key_reuses_run_without_fetch() -> None:
    source = _calendar_source("completed-key")
    first = _execute_scheduled(source, FixturePageSource({None: _terminal_page()}))
    unused_source = FixturePageSource({None: _terminal_page()})

    second = _execute_scheduled(source, unused_source)

    assert first.created is True
    assert second.created is False
    assert second.sync_run.pk == first.sync_run.pk
    assert second.window_result is None
    assert unused_source.calls == []
    assert SyncRun.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_provider_failure_releases_run_ownership() -> None:
    source = _calendar_source("provider-error-release")

    with pytest.raises(EarningsCalendarWindowFailure):
        _execute_scheduled(source, FixturePageSource({}))

    assert SyncRun.objects.get().status == SyncRun.Status.FAILED
    with calendar_run_ownership(source_id=source.pk, job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE):
        pass


@pytest.mark.django_db(transaction=True)
def test_fresh_unowned_running_run_is_not_taken_over() -> None:
    source = _calendar_source("fresh-running")
    original = _start_scheduled(source)

    with pytest.raises(EarningsCalendarRunBusy):
        _execute_scheduled(source, FixturePageSource({None: _terminal_page()}))

    original.refresh_from_db()
    assert original.status == SyncRun.Status.RUNNING
    assert SyncRun.objects.count() == 1


@pytest.mark.parametrize("has_raw", (False, True))
@pytest.mark.django_db(transaction=True)
@override_settings(EARNINGS_CALENDAR_STALE_AFTER_SECONDS=60)
def test_stale_run_is_preserved_and_retry_starts_a_new_run(has_raw: bool) -> None:
    source = _calendar_source(f"stale-{has_raw}")
    old = _start_scheduled(source, started_at=timezone.now() - timedelta(hours=2))
    page = _terminal_page()
    if has_raw:
        record_raw_data_observation(
            sync_run=old,
            source_url=page.source_url,
            payload=page.raw_content,
            request_identity=page.request_identity,
            fetched_at=page.fetched_at,
        )

    with pytest.raises(EarningsCalendarSyncRunRetryRequired):
        _execute_scheduled(source, FixturePageSource({None: page}))

    old.refresh_from_db()
    assert old.status == (SyncRun.Status.PARTIAL if has_raw else SyncRun.Status.FAILED)
    assert old.failed_count == 1
    assert old.fetched_count == (1 if has_raw else 0)
    retry_source = FixturePageSource({None: page})
    retried = execute_retry_earnings_calendar_window(
        previous_run=old,
        request_id="retry-after-stale-1",
        expected_pool_hash=POOL_HASH,
        page_source=retry_source,
        parser=FixtureEarningsCalendarParser(),
        provider_version=PROVIDER_VERSION,
    )

    assert retried.created is True
    assert retried.sync_run.pk != old.pk
    assert retried.sync_run.status == SyncRun.Status.SUCCEEDED
    assert retried.sync_run.provider_version == PROVIDER_VERSION
    assert retried.sync_run.scope["window_kind"] == "retry"
    assert retry_source.calls == [None]
    assert SyncRun.objects.count() == 2
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == (2 if has_raw else 1)
    assert retried.sync_run.fetched_count == 1
    assert EarningsEvent.objects.count() == 0
    assert EarningsReconciliationDecision.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_retry_does_not_call_monitoring_pool_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _calendar_source("retry-selector-isolation")
    previous = _start_scheduled(source)
    mark_sync_run_failed(previous.pk, error_summary="Fixture retry source failed.")
    previous.refresh_from_db()
    page_source = FixturePageSource({None: _terminal_page()})

    def fail_selector(**kwargs: object) -> None:
        del kwargs
        raise AssertionError("Retry must not rerun the monitoring-pool selector.")

    monkeypatch.setattr(
        "earnings.services.monitoring_pool.select_monitoring_pool",
        fail_selector,
    )
    monkeypatch.setattr("earnings.services.select_monitoring_pool", fail_selector)

    retried = execute_retry_earnings_calendar_window(
        previous_run=previous,
        request_id="retry-selector-isolation",
        expected_pool_hash=POOL_HASH,
        page_source=page_source,
        parser=FixtureEarningsCalendarParser(),
        provider_version=PROVIDER_VERSION,
    )

    assert retried.created is True
    assert retried.sync_run.status == SyncRun.Status.SUCCEEDED


@pytest.mark.django_db(transaction=True)
@override_settings(EARNINGS_CALENDAR_STALE_AFTER_SECONDS=60)
def test_stale_run_with_count_exceeding_raw_facts_requires_manual_review() -> None:
    source = _calendar_source("stale-count-mismatch")
    old = _start_scheduled(source, started_at=timezone.now() - timedelta(hours=2))
    update_sync_run_counts(old.pk, fetched_delta=1)
    SyncRun.objects.filter(pk=old.pk).update(heartbeat_at=old.started_at)

    with pytest.raises(EarningsCalendarRunCountMismatch):
        _execute_scheduled(source, FixturePageSource({None: _terminal_page()}))

    old.refresh_from_db()
    assert old.status == SyncRun.Status.RUNNING
    assert old.fetched_count == 1
    assert SyncRun.objects.count() == 1
    with calendar_run_ownership(source_id=source.pk, job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE):
        pass


@pytest.mark.django_db(transaction=True)
def test_partial_retry_requires_new_key_and_preserves_old_lineage() -> None:
    source = _calendar_source("partial-retry")
    old = _start_scheduled(source)
    page = _terminal_page()
    record_raw_data_observation(
        sync_run=old,
        source_url=page.source_url,
        payload=page.raw_content,
        request_identity=page.request_identity,
        fetched_at=page.fetched_at,
    )
    mark_sync_run_partial(old.pk, error_summary="Fixture partial run.")

    first = execute_retry_earnings_calendar_window(
        previous_run=old,
        request_id="partial-retry-1",
        expected_pool_hash=POOL_HASH,
        page_source=FixturePageSource({None: page}),
        parser=FixtureEarningsCalendarParser(),
        provider_version=PROVIDER_VERSION,
    )
    second_source = FixturePageSource({None: page})
    second = execute_retry_earnings_calendar_window(
        previous_run=old,
        request_id="partial-retry-1",
        expected_pool_hash=POOL_HASH,
        page_source=second_source,
        parser=FixtureEarningsCalendarParser(),
        provider_version=PROVIDER_VERSION,
    )

    old.refresh_from_db()
    assert old.status == SyncRun.Status.PARTIAL
    assert first.sync_run.pk != old.pk
    assert second.sync_run.pk == first.sync_run.pk
    assert second.created is False
    assert second_source.calls == []
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_retry_pool_hash_mismatch_fails_before_new_run() -> None:
    source = _calendar_source("pool-mismatch")
    old = _start_scheduled(source)
    mark_sync_run_failed(old.pk, error_summary="Fixture failure.")

    with pytest.raises(EarningsCalendarRetryContextMismatch):
        execute_retry_earnings_calendar_window(
            previous_run=old,
            request_id="pool-mismatch-retry",
            expected_pool_hash="b" * 64,
            page_source=FixturePageSource({None: _terminal_page()}),
            parser=FixtureEarningsCalendarParser(),
            provider_version=PROVIDER_VERSION,
        )

    assert SyncRun.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_retry_missing_provider_version_fails_before_new_run() -> None:
    source = _calendar_source("retry-missing-provider-version")
    old = _start_scheduled(source)
    mark_sync_run_failed(old.pk, error_summary="Fixture failure.")
    page_source = FixturePageSource({None: _terminal_page()})

    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="provider_version"):
        execute_retry_earnings_calendar_window(
            previous_run=old,
            request_id="missing-provider-version-retry",
            expected_pool_hash=POOL_HASH,
            page_source=page_source,
            parser=FixtureEarningsCalendarParser(),
            provider_version="",
        )

    assert page_source.calls == []
    assert RawDataRecord.objects.count() == 0
    assert SyncRun.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_duplicate_finalize_cannot_change_terminal_run() -> None:
    source = _calendar_source("duplicate-finalize")
    old = _start_scheduled(source)
    final = mark_sync_run_succeeded(old.pk)

    with pytest.raises(InvalidSyncRunTransition):
        mark_sync_run_succeeded(old.pk)

    unchanged = SyncRun.objects.get(pk=old.pk)
    assert unchanged.status == final.status
    assert unchanged.finished_at == final.finished_at
    assert unchanged.fetched_count == 0


@pytest.mark.django_db(transaction=True)
def test_page_fetch_holds_ownership_without_a_database_transaction() -> None:
    source = _calendar_source("fetch-outside-transaction")
    page = _terminal_page()

    class CheckingPageSource(FixturePageSource):
        def fetch_page(self, cursor: str | None) -> EarningsCalendarPage:
            assert connection.in_atomic_block is False
            return super().fetch_page(cursor)

    result = _execute_scheduled(source, CheckingPageSource({None: page}))

    assert result.sync_run.status == SyncRun.Status.SUCCEEDED


@pytest.mark.django_db(transaction=True)
def test_lost_database_session_does_not_finalize_run() -> None:
    source = _calendar_source("lost-session")
    sync_run = _window_run(source, "lost-session")
    page = _terminal_page()

    class ClosingPageSource(FixturePageSource):
        def fetch_page(self, cursor: str | None) -> EarningsCalendarPage:
            connection.close()
            return super().fetch_page(cursor)

    with pytest.raises(EarningsCalendarRunOwnershipLost):
        run_earnings_calendar_window(
            sync_run=sync_run,
            page_source=ClosingPageSource({None: page}),
            parser=FixtureEarningsCalendarParser(),
            provider_key=PROVIDER_KEY,
            provider_version=PROVIDER_VERSION,
        )

    unchanged = SyncRun.objects.get(pk=sync_run.pk)
    assert unchanged.status == SyncRun.Status.RUNNING
    assert unchanged.fetched_count == 0
    assert RawDataObservation.objects.filter(sync_run=sync_run).count() == 0
    with calendar_run_ownership(source_id=source.pk, job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE):
        pass


@pytest.mark.django_db(transaction=True)
def test_parser_connection_loss_stops_normalized_persistence() -> None:
    source = _calendar_source("parser-lost-session")
    sync_run = _window_run(source, "parser-lost-session")
    page = _page(
        cursor=None,
        payload=_fixture_bytes("complete_payload.json"),
        next_cursor=None,
        is_terminal=True,
    )

    class ClosingParser(FixtureEarningsCalendarParser):
        def parse(
            self,
            raw_content: bytes,
            *,
            provider_key: str,
            provider_version: str,
        ) -> EarningsCalendarParseResult:
            result = super().parse(
                raw_content, provider_key=provider_key, provider_version=provider_version
            )
            connection.close()
            return result

    with pytest.raises(EarningsCalendarRunOwnershipLost):
        run_earnings_calendar_window(
            sync_run=sync_run,
            page_source=FixturePageSource({None: page}),
            parser=ClosingParser(),
            provider_key=PROVIDER_KEY,
            provider_version=PROVIDER_VERSION,
        )

    assert EarningsCalendarObservation.objects.count() == 0
    assert SyncRun.objects.get(pk=sync_run.pk).status == SyncRun.Status.RUNNING


@pytest.mark.django_db(transaction=True)
def test_parser_failure_after_connection_loss_does_not_write_parse_attempt() -> None:
    source = _calendar_source("parser-error-lost-session")
    sync_run = _window_run(source, "parser-error-lost-session")
    page = _terminal_page()

    class FailingClosingParser(FixtureEarningsCalendarParser):
        def parse(
            self,
            raw_content: bytes,
            *,
            provider_key: str,
            provider_version: str,
        ) -> EarningsCalendarParseResult:
            connection.close()
            raise EarningsCalendarPayloadError("fixture parse failure")

    with pytest.raises(EarningsCalendarRunOwnershipLost):
        run_earnings_calendar_window(
            sync_run=sync_run,
            page_source=FixturePageSource({None: page}),
            parser=FailingClosingParser(),
            provider_key=PROVIDER_KEY,
            provider_version=PROVIDER_VERSION,
        )

    assert RawDataObservation.objects.filter(sync_run=sync_run).count() == 1
    assert RawDataParseAttempt.objects.count() == 0
    assert SyncRun.objects.get(pk=sync_run.pk).status == SyncRun.Status.RUNNING


@pytest.mark.django_db(transaction=True)
def test_two_pages_cannot_share_one_persisted_request_identity() -> None:
    source = _calendar_source("duplicate-page-request")
    sync_run = _window_run(source, "duplicate-page-request")
    first = replace(
        _page(
            cursor=None,
            payload=_fixture_bytes("empty_payload.json"),
            next_cursor="next",
            is_terminal=False,
        ),
        source_url="https://fixture-earnings-calendar.test/calendar",
        request_identity={},
    )
    second = replace(
        _page(
            cursor="next",
            payload=_fixture_bytes("empty_payload.json"),
            next_cursor=None,
            is_terminal=True,
        ),
        source_url=first.source_url,
        request_identity={},
    )

    with pytest.raises(EarningsCalendarWindowFailure):
        run_earnings_calendar_window(
            sync_run=sync_run,
            page_source=FixturePageSource({None: first, "next": second}),
            parser=FixtureEarningsCalendarParser(),
            provider_key=PROVIDER_KEY,
            provider_version=PROVIDER_VERSION,
        )

    final = SyncRun.objects.get(pk=sync_run.pk)
    assert final.status == SyncRun.Status.PARTIAL
    assert final.fetched_count == 1
    assert RawDataObservation.objects.filter(sync_run=sync_run).count() == 1
