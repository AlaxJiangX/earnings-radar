from __future__ import annotations

import http.client
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest import mock

import pytest

from audit.models import (
    AuditRecord,
    DataChange,
    DataSource,
    RawDataObservation,
    RawDataParseAttempt,
    RawDataRecord,
    SourceEvidence,
    SyncRun,
)
from audit.services import mark_sync_run_succeeded, start_sync_run, update_sync_run_counts
from companies.models import Company, SecurityListing
from earnings.calendar_parsing import (
    FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION,
    FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
    FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
    EarningsCalendarParser,
    EarningsCalendarParserContextError,
    EarningsCalendarParseResult,
    FixtureEarningsCalendarParser,
)
from earnings.models import (
    EarningsCalendarObservation,
    EarningsDateChange,
    EarningsEvent,
    EarningsReconciliationDecision,
)
from earnings.services import (
    EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    MAX_EARNINGS_CALENDAR_PAGES,
    EarningsCalendarPage,
    EarningsCalendarPageSource,
    EarningsCalendarWindowFailure,
    EarningsCalendarWindowResult,
    InvalidEarningsCalendarWindow,
    run_earnings_calendar_window,
)
from earnings.services.calendar import (
    EarningsCalendarObservationWriteResult,
)
from earnings.services.calendar import (
    record_earnings_calendar_observation as real_record_earnings_calendar_observation,
)
from indexes.models import IndexMembership

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "earnings_calendar"
FETCHED_AT = datetime(2026, 7, 14, 12, 0, 1, tzinfo=UTC)
PROVIDER_KEY = FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY
PROVIDER_VERSION = FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def _calendar_source(suffix: str) -> DataSource:
    return DataSource.objects.create(
        key=f"fixture-calendar-pagination-{suffix}",
        name=f"Fixture calendar pagination {suffix}",
        source_type=DataSource.SourceType.EARNINGS_CALENDAR,
        base_url="https://fixture-earnings-calendar.test/",
        provider_adapter=PROVIDER_KEY,
        license_notes="Synthetic test-only source.",
    )


def _window_run(
    source: DataSource,
    suffix: str,
    *,
    job_type: str = EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
) -> SyncRun:
    return start_sync_run(
        job_type=job_type,
        source=source,
        scope={"fixture": suffix},
        idempotency_key=f"fixture.earnings-calendar-window:{suffix}",
    )


def _page(
    *,
    cursor: str | None,
    payload: bytes,
    next_cursor: str | None,
    is_terminal: bool,
    provider_key: str = PROVIDER_KEY,
    provider_version: str = PROVIDER_VERSION,
) -> EarningsCalendarPage:
    label = cursor if cursor is not None else "start"
    return EarningsCalendarPage(
        cursor=cursor,
        raw_content=payload,
        source_url=f"https://fixture-earnings-calendar.test/calendar?cursor={label}",
        fetched_at=FETCHED_AT,
        provider_key=provider_key,
        provider_version=provider_version,
        is_terminal=is_terminal,
        next_cursor=next_cursor,
        request_identity={"cursor": cursor},
    )


class FixturePageSource:
    def __init__(self, pages: dict[str | None, EarningsCalendarPage]) -> None:
        self.pages = dict(pages)
        self.calls: list[str | None] = []

    def fetch_page(self, cursor: str | None) -> EarningsCalendarPage:
        self.calls.append(cursor)
        try:
            return self.pages[cursor]
        except KeyError:
            raise RuntimeError("missing fixture page") from None


class FaultyPageSource(FixturePageSource):
    def __init__(
        self,
        pages: dict[str | None, EarningsCalendarPage],
        *,
        error_on_cursor: str | None,
        error: Exception,
    ) -> None:
        super().__init__(pages)
        self.error_on_cursor = error_on_cursor
        self.error = error

    def fetch_page(self, cursor: str | None) -> EarningsCalendarPage:
        self.calls.append(cursor)
        if cursor == self.error_on_cursor:
            raise self.error
        try:
            return self.pages[cursor]
        except KeyError:
            raise RuntimeError("missing fixture page") from None


class MalformedPageSource:
    def fetch_page(self, cursor: str | None) -> EarningsCalendarPage:
        del cursor
        return cast(EarningsCalendarPage, {"not": "a page"})


class InfinitePageSource:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def fetch_page(self, cursor: str | None) -> EarningsCalendarPage:
        self.calls.append(cursor)
        index = 0 if cursor is None else int(cursor)
        return _page(
            cursor=cursor,
            payload=_fixture_bytes("empty_payload.json"),
            next_cursor=str(index + 1),
            is_terminal=False,
        )


class RaisingParser:
    parser_version = FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION

    def __init__(self, error: Exception) -> None:
        self.error = error

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> EarningsCalendarParseResult:
        del raw_content, provider_key, provider_version
        raise self.error


def _run_window(
    *,
    sync_run: SyncRun,
    page_source: EarningsCalendarPageSource,
    parser: EarningsCalendarParser | None = None,
    max_pages: int = MAX_EARNINGS_CALENDAR_PAGES,
) -> EarningsCalendarWindowResult:
    selected_parser: EarningsCalendarParser
    if parser is None:
        selected_parser = FixtureEarningsCalendarParser()
    else:
        selected_parser = parser
    return run_earnings_calendar_window(
        sync_run=sync_run,
        page_source=page_source,
        parser=selected_parser,
        provider_key=PROVIDER_KEY,
        provider_version=PROVIDER_VERSION,
        max_pages=max_pages,
    )


def _forbidden_row_counts() -> tuple[int, ...]:
    return (
        EarningsEvent.objects.count(),
        EarningsReconciliationDecision.objects.count(),
        SourceEvidence.objects.count(),
        AuditRecord.objects.count(),
        DataChange.objects.count(),
        EarningsDateChange.objects.count(),
        Company.objects.count(),
        SecurityListing.objects.count(),
        IndexMembership.objects.count(),
    )


@pytest.mark.django_db
def test_two_page_window_succeeds_with_page_lineage_and_counts() -> None:
    source = _calendar_source("two-page")
    sync_run = _window_run(source, "two-page")
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor="page-2",
                is_terminal=False,
            ),
            "page-2": _page(
                cursor="page-2",
                payload=_fixture_bytes("optional_missing.json"),
                next_cursor=None,
                is_terminal=True,
            ),
        }
    )

    result = _run_window(sync_run=sync_run, page_source=page_source)

    assert page_source.calls == [None, "page-2"]
    assert [page.page_index for page in result.pages] == [1, 2]
    assert [page.cursor for page in result.pages] == [None, "page-2"]
    assert result.completed is True
    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.fetched_count == 2
    assert result.sync_run.failed_count == 0
    assert result.sync_run.created_count == 0
    assert result.sync_run.updated_count == 0
    assert result.sync_run.skipped_count == 0
    assert result.observation_count == 3
    assert result.observations_created == 3
    assert result.observations_reused == 0
    assert RawDataRecord.objects.count() == 2
    assert RawDataObservation.objects.count() == 2
    assert RawDataParseAttempt.objects.count() == 2
    assert EarningsCalendarObservation.objects.count() == 3

    first_page, second_page = result.pages
    assert first_page.ingestion.raw_data_record.pk != second_page.ingestion.raw_data_record.pk
    assert all(
        observation.raw_data_record_id == first_page.ingestion.raw_data_record.pk
        for observation in first_page.ingestion.observations
    )
    assert all(
        observation.raw_data_record_id == second_page.ingestion.raw_data_record.pk
        for observation in second_page.ingestion.observations
    )


@pytest.mark.django_db
def test_three_page_window_follows_cursor_order() -> None:
    source = _calendar_source("three-page")
    sync_run = _window_run(source, "three-page")
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor="B",
                is_terminal=False,
            ),
            "B": _page(
                cursor="B",
                payload=_fixture_bytes("optional_missing.json"),
                next_cursor="C",
                is_terminal=False,
            ),
            "C": _page(
                cursor="C",
                payload=_fixture_bytes("empty_payload.json"),
                next_cursor=None,
                is_terminal=True,
            ),
        }
    )

    result = _run_window(sync_run=sync_run, page_source=page_source)

    assert page_source.calls == [None, "B", "C"]
    assert [page.cursor for page in result.pages] == [None, "B", "C"]
    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.fetched_count == 3
    assert result.observation_count == 3
    assert EarningsCalendarObservation.objects.count() == 3


@pytest.mark.django_db
def test_first_page_parser_failure_finalizes_failed_and_preserves_raw() -> None:
    source = _calendar_source("first-page-failure")
    sync_run = _window_run(source, "first-page-failure")
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("missing_provider_event_id.json"),
                next_cursor="B",
                is_terminal=False,
            ),
            "B": _page(
                cursor="B",
                payload=_fixture_bytes("empty_payload.json"),
                next_cursor=None,
                is_terminal=True,
            ),
        }
    )

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(sync_run=sync_run, page_source=page_source)

    failure = excinfo.value
    assert page_source.calls == [None]
    assert failure.failed_page_index == 1
    assert failure.pages == ()
    assert failure.sync_run.status == SyncRun.Status.FAILED
    assert failure.sync_run.fetched_count == 0
    assert failure.sync_run.failed_count == 1
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1
    assert RawDataParseAttempt.objects.get().status == RawDataParseAttempt.Status.UNSUPPORTED
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_later_page_parser_failure_finalizes_partial_and_keeps_both_lineages() -> None:
    source = _calendar_source("later-page-failure")
    sync_run = _window_run(source, "later-page-failure")
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor="B",
                is_terminal=False,
            ),
            "B": _page(
                cursor="B",
                payload=_fixture_bytes("missing_provider_event_id.json"),
                next_cursor="C",
                is_terminal=False,
            ),
            "C": _page(
                cursor="C",
                payload=_fixture_bytes("empty_payload.json"),
                next_cursor=None,
                is_terminal=True,
            ),
        }
    )

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(sync_run=sync_run, page_source=page_source)

    failure = excinfo.value
    assert page_source.calls == [None, "B"]
    assert failure.failed_page_index == 2
    assert len(failure.pages) == 1
    assert failure.sync_run.status == SyncRun.Status.PARTIAL
    assert failure.sync_run.fetched_count == 1
    assert failure.sync_run.failed_count == 1
    assert RawDataRecord.objects.count() == 2
    assert RawDataObservation.objects.count() == 2
    assert (
        RawDataParseAttempt.objects.filter(status=RawDataParseAttempt.Status.SUCCEEDED).count() == 1
    )
    assert (
        RawDataParseAttempt.objects.filter(status=RawDataParseAttempt.Status.UNSUPPORTED).count()
        == 1
    )
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db
def test_page_source_exception_on_first_page_finalizes_failed_without_raw() -> None:
    source = _calendar_source("source-error-first")
    sync_run = _window_run(source, "source-error-first")
    page_source = FaultyPageSource(
        {},
        error_on_cursor=None,
        error=RuntimeError("password=fixture-secret-token"),
    )

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(sync_run=sync_run, page_source=page_source)

    failure = excinfo.value
    assert failure.sync_run.status == SyncRun.Status.FAILED
    assert failure.sync_run.fetched_count == 0
    assert failure.sync_run.failed_count == 1
    assert "fixture-secret-token" not in failure.sync_run.error_summary
    assert "RuntimeError" in failure.sync_run.error_summary
    assert RawDataRecord.objects.count() == 0


@pytest.mark.django_db
def test_page_source_exception_on_later_page_finalizes_partial() -> None:
    source = _calendar_source("source-error-later")
    sync_run = _window_run(source, "source-error-later")
    page_source = FaultyPageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor="B",
                is_terminal=False,
            )
        },
        error_on_cursor="B",
        error=RuntimeError("temporary fixture failure"),
    )

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(sync_run=sync_run, page_source=page_source)

    failure = excinfo.value
    assert page_source.calls == [None, "B"]
    assert failure.sync_run.status == SyncRun.Status.PARTIAL
    assert failure.sync_run.fetched_count == 1
    assert failure.sync_run.failed_count == 1
    assert len(failure.pages) == 1
    assert RawDataRecord.objects.count() == 1


@pytest.mark.django_db
def test_empty_terminal_window_succeeds_with_zero_observations() -> None:
    source = _calendar_source("empty-window")
    sync_run = _window_run(source, "empty-window")
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

    result = _run_window(sync_run=sync_run, page_source=page_source)

    assert page_source.calls == [None]
    assert result.completed is True
    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.sync_run.fetched_count == 1
    assert result.sync_run.failed_count == 0
    assert result.observation_count == 0
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_cursor_cycle_fails_closed_without_infinite_loop() -> None:
    source = _calendar_source("cursor-cycle")
    sync_run = _window_run(source, "cursor-cycle")
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor="A",
                is_terminal=False,
            ),
            "A": _page(
                cursor="A",
                payload=_fixture_bytes("optional_missing.json"),
                next_cursor="B",
                is_terminal=False,
            ),
            "B": _page(
                cursor="B",
                payload=_fixture_bytes("empty_payload.json"),
                next_cursor="A",
                is_terminal=False,
            ),
        }
    )

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(sync_run=sync_run, page_source=page_source)

    failure = excinfo.value
    assert page_source.calls == [None, "A", "B"]
    assert failure.failed_page_index == 3
    assert failure.sync_run.status == SyncRun.Status.PARTIAL
    assert failure.sync_run.fetched_count == 3
    assert failure.sync_run.failed_count == 1
    assert "cycle" in failure.sync_run.error_summary


@pytest.mark.django_db
def test_non_progressing_cursor_fails_closed() -> None:
    source = _calendar_source("non-progressing")
    sync_run = _window_run(source, "non-progressing")
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor="A",
                is_terminal=False,
            ),
            "A": _page(
                cursor="A",
                payload=_fixture_bytes("empty_payload.json"),
                next_cursor="A",
                is_terminal=False,
            ),
        }
    )

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(sync_run=sync_run, page_source=page_source)

    failure = excinfo.value
    assert page_source.calls == [None, "A"]
    assert failure.failed_page_index == 2
    assert failure.sync_run.status == SyncRun.Status.PARTIAL
    assert failure.sync_run.fetched_count == 2
    assert failure.sync_run.failed_count == 1
    assert "did not progress" in failure.sync_run.error_summary


@pytest.mark.django_db
def test_terminal_page_with_next_cursor_fails_closed_before_raw_persistence() -> None:
    source = _calendar_source("terminal-next")
    sync_run = _window_run(source, "terminal-next")
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor="A",
                is_terminal=True,
            )
        }
    )

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(sync_run=sync_run, page_source=page_source)

    failure = excinfo.value
    assert page_source.calls == [None]
    assert failure.sync_run.status == SyncRun.Status.FAILED
    assert failure.sync_run.fetched_count == 0
    assert failure.sync_run.failed_count == 1
    assert RawDataRecord.objects.count() == 0


@pytest.mark.django_db
def test_page_provider_mismatch_fails_closed_before_raw_persistence() -> None:
    source = _calendar_source("page-provider-mismatch")
    sync_run = _window_run(source, "page-provider-mismatch")
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor=None,
                is_terminal=True,
                provider_key="other-fixture-provider",
            )
        }
    )

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(sync_run=sync_run, page_source=page_source)

    assert excinfo.value.sync_run.status == SyncRun.Status.FAILED
    assert excinfo.value.sync_run.failed_count == 1
    assert RawDataRecord.objects.count() == 0


@pytest.mark.django_db
def test_malformed_page_envelope_fails_closed() -> None:
    source = _calendar_source("malformed-page")
    sync_run = _window_run(source, "malformed-page")

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(sync_run=sync_run, page_source=MalformedPageSource())

    assert excinfo.value.sync_run.status == SyncRun.Status.FAILED
    assert excinfo.value.sync_run.failed_count == 1
    assert RawDataRecord.objects.count() == 0


@pytest.mark.django_db
def test_max_pages_cap_fails_closed() -> None:
    source = _calendar_source("max-pages")
    sync_run = _window_run(source, "max-pages")
    page_source = InfinitePageSource()

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(sync_run=sync_run, page_source=page_source, max_pages=3)

    failure = excinfo.value
    assert page_source.calls == [None, "1", "2"]
    assert failure.sync_run.status == SyncRun.Status.PARTIAL
    assert failure.sync_run.fetched_count == 3
    assert failure.sync_run.failed_count == 1
    assert "Maximum page count" in failure.sync_run.error_summary


@pytest.mark.django_db
def test_wrong_job_type_is_rejected_before_pagination() -> None:
    source = _calendar_source("wrong-job-type")
    sync_run = _window_run(source, "wrong-job-type", job_type="other.job")
    page_source = FixturePageSource({})

    with pytest.raises(InvalidEarningsCalendarWindow, match="job_type"):
        _run_window(sync_run=sync_run, page_source=page_source)

    assert page_source.calls == []
    sync_run.refresh_from_db()
    assert sync_run.status == SyncRun.Status.RUNNING


@pytest.mark.django_db
def test_non_running_run_is_rejected_before_pagination() -> None:
    source = _calendar_source("non-running")
    sync_run = _window_run(source, "non-running")
    mark_sync_run_succeeded(sync_run.pk)
    page_source = FixturePageSource({})

    with pytest.raises(InvalidEarningsCalendarWindow, match="running"):
        _run_window(sync_run=sync_run, page_source=page_source)

    assert page_source.calls == []


@pytest.mark.django_db
def test_run_with_prior_counts_is_rejected_before_pagination() -> None:
    source = _calendar_source("prior-counts")
    sync_run = _window_run(source, "prior-counts")
    update_sync_run_counts(sync_run.pk, fetched_delta=1)
    page_source = FixturePageSource({})

    with pytest.raises(InvalidEarningsCalendarWindow, match="zero counts"):
        _run_window(sync_run=sync_run, page_source=page_source)

    assert page_source.calls == []


@pytest.mark.django_db
def test_provider_context_mismatch_is_rejected_before_pagination() -> None:
    source = _calendar_source("provider-context")
    sync_run = _window_run(source, "provider-context")
    page_source = FixturePageSource({})

    with pytest.raises(InvalidEarningsCalendarWindow, match="provider_adapter"):
        run_earnings_calendar_window(
            sync_run=sync_run,
            page_source=page_source,
            parser=FixtureEarningsCalendarParser(),
            provider_key="other-fixture-provider",
            provider_version=PROVIDER_VERSION,
        )

    assert page_source.calls == []


@pytest.mark.django_db
def test_parser_context_failure_finalizes_window() -> None:
    source = _calendar_source("parser-context-failure")
    sync_run = _window_run(source, "parser-context-failure")
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor=None,
                is_terminal=True,
            )
        }
    )

    with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
        _run_window(
            sync_run=sync_run,
            page_source=page_source,
            parser=RaisingParser(EarningsCalendarParserContextError("bad parser context")),
        )

    assert excinfo.value.sync_run.status == SyncRun.Status.FAILED
    assert excinfo.value.sync_run.failed_count == 1
    assert RawDataParseAttempt.objects.get().status == RawDataParseAttempt.Status.SYSTEM_ERROR
    assert EarningsCalendarObservation.objects.count() == 0


@pytest.mark.django_db
def test_normalized_persistence_failure_on_later_page_finalizes_partial() -> None:
    source = _calendar_source("persistence-failure")
    sync_run = _window_run(source, "persistence-failure")
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("empty_payload.json"),
                next_cursor="B",
                is_terminal=False,
            ),
            "B": _page(
                cursor="B",
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor=None,
                is_terminal=True,
            ),
        }
    )
    real_record = cast(
        Callable[..., EarningsCalendarObservationWriteResult],
        real_record_earnings_calendar_observation,
    )
    call_count = 0

    def fail_on_second_record(**kwargs: object) -> EarningsCalendarObservationWriteResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return real_record(**kwargs)
        raise RuntimeError("simulated normalized persistence failure")

    with mock.patch(
        "earnings.services.calendar_ingestion.record_earnings_calendar_observation",
        side_effect=fail_on_second_record,
    ):
        with pytest.raises(EarningsCalendarWindowFailure) as excinfo:
            _run_window(sync_run=sync_run, page_source=page_source)

    failure = excinfo.value
    assert page_source.calls == [None, "B"]
    assert failure.sync_run.status == SyncRun.Status.PARTIAL
    assert failure.sync_run.fetched_count == 1
    assert failure.sync_run.failed_count == 1
    assert len(failure.pages) == 1
    assert RawDataRecord.objects.count() == 2
    assert RawDataObservation.objects.count() == 2
    assert (
        RawDataParseAttempt.objects.filter(status=RawDataParseAttempt.Status.SUCCEEDED).count() == 2
    )
    assert EarningsCalendarObservation.objects.count() == 1


@pytest.mark.django_db
def test_window_writes_no_downstream_domain_rows() -> None:
    source = _calendar_source("side-effects")
    sync_run = _window_run(source, "side-effects")
    forbidden_before = _forbidden_row_counts()
    page_source = FixturePageSource(
        {
            None: _page(
                cursor=None,
                payload=_fixture_bytes("complete_payload.json"),
                next_cursor="B",
                is_terminal=False,
            ),
            "B": _page(
                cursor="B",
                payload=_fixture_bytes("empty_payload.json"),
                next_cursor=None,
                is_terminal=True,
            ),
        }
    )

    _run_window(sync_run=sync_run, page_source=page_source)

    assert _forbidden_row_counts() == forbidden_before
    assert EarningsCalendarObservation.objects.count() == 2


@pytest.mark.django_db
def test_window_does_not_open_network_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("window orchestration must not open network connections")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", fail_connect)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", fail_connect)
    source = _calendar_source("no-network")
    sync_run = _window_run(source, "no-network")
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

    result = _run_window(sync_run=sync_run, page_source=page_source)

    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
