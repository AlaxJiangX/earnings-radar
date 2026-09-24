"""Provider-neutral pagination and logical-window completion for earnings calendars.

This service consumes a caller-owned running ``SyncRun`` and an offline
``EarningsCalendarPageSource``.  Each page is handed to the merged 4.2C-2
raw-first ingestion service; this module only owns cursor traversal, page-level
counts, and the logical-window terminal status.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn, Protocol, runtime_checkable

from audit.models import DataSource, SyncRun
from audit.security import ProviderRequestContextDescriptor
from audit.services import (
    build_request_fingerprint,
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    update_sync_run_counts,
)
from earnings.calendar_parsing import EarningsCalendarParser
from earnings.services.calendar_ingestion import (
    EarningsCalendarIngestionError,
    EarningsCalendarIngestionResult,
    ingest_earnings_calendar_payload,
)
from earnings.services.calendar_run_ownership import (
    EarningsCalendarRunOwnershipLost,
    assert_calendar_run_ownership,
    owned_calendar_run,
)

EARNINGS_CALENDAR_WINDOW_JOB_TYPE = "earnings.calendar_window"
MAX_EARNINGS_CALENDAR_PAGES = 100
MAX_EARNINGS_CALENDAR_CURSOR_LENGTH = 512


class EarningsCalendarWindowError(RuntimeError):
    """Base class for earnings calendar window orchestration failures."""


class InvalidEarningsCalendarWindow(EarningsCalendarWindowError):
    """Raised before pagination when the SyncRun or window context is invalid."""


class EarningsCalendarPaginationError(EarningsCalendarWindowError):
    """Raised for malformed, duplicate, or non-progressing pagination."""


class EarningsCalendarWindowFailure(EarningsCalendarWindowError):
    """Raised after a failed logical window has been finalized."""

    def __init__(
        self,
        *,
        message: str,
        sync_run: SyncRun,
        pages: tuple[EarningsCalendarWindowPageResult, ...],
        failed_page_index: int,
        cause: Exception,
    ) -> None:
        super().__init__(message)
        self.sync_run = sync_run
        self.pages = pages
        self.failed_page_index = failed_page_index
        self.cause = cause


@dataclass(frozen=True, slots=True)
class EarningsCalendarPage:
    """One provider-neutral page returned by a page source."""

    cursor: str | None
    raw_content: bytes
    source_url: str
    fetched_at: datetime
    provider_key: str
    provider_version: str
    is_terminal: bool
    next_cursor: str | None
    request_method: str = "GET"
    request_identity: Mapping[str, object] = field(default_factory=dict)
    http_status: int | None = 200
    content_type: str = "application/json"
    encoding: str = "utf-8"
    request_descriptor: ProviderRequestContextDescriptor | None = None


@runtime_checkable
class EarningsCalendarPageSource(Protocol):
    """Offline/provider-neutral source of one page for a requested cursor."""

    def fetch_page(self, cursor: str | None) -> EarningsCalendarPage: ...


@dataclass(frozen=True, slots=True)
class EarningsCalendarWindowPageResult:
    """Successful ingestion result for one page in a logical window."""

    page_index: int
    cursor: str | None
    next_cursor: str | None
    is_terminal: bool
    ingestion: EarningsCalendarIngestionResult


@dataclass(frozen=True, slots=True)
class EarningsCalendarWindowResult:
    """Completed logical window result."""

    sync_run: SyncRun
    pages: tuple[EarningsCalendarWindowPageResult, ...]
    completed: bool
    observation_count: int
    observations_created: int
    observations_reused: int


@dataclass(frozen=True, slots=True)
class _WindowContext:
    sync_run: SyncRun
    provider_key: str
    provider_version: str
    max_pages: int


@owned_calendar_run
def run_earnings_calendar_window(
    *,
    sync_run: SyncRun,
    page_source: EarningsCalendarPageSource,
    parser: EarningsCalendarParser,
    provider_key: str,
    provider_version: str,
    max_pages: int = MAX_EARNINGS_CALENDAR_PAGES,
) -> EarningsCalendarWindowResult:
    """Traverse all pages and finalize one logical earnings-calendar window."""

    context = _validate_window_context(
        sync_run=sync_run,
        page_source=page_source,
        parser=parser,
        provider_key=provider_key,
        provider_version=provider_version,
        max_pages=max_pages,
    )
    pages: list[EarningsCalendarWindowPageResult] = []
    seen_cursors: set[str] = set()
    seen_request_fingerprints: set[str] = set()
    request_cursor: str | None = None
    page_index = 1

    def count_persisted_page() -> None:
        assert_calendar_run_ownership()
        update_sync_run_counts(context.sync_run.pk, fetched_delta=1)

    while True:
        assert_calendar_run_ownership()
        if page_index > context.max_pages:
            cause = EarningsCalendarPaginationError(
                f"Maximum page count {context.max_pages} exceeded."
            )
            _raise_window_failure(
                context=context,
                pages=pages,
                page_index=page_index,
                cause=cause,
            )
        if request_cursor is not None:
            if request_cursor in seen_cursors:
                cause = EarningsCalendarPaginationError("Pagination cursor was already used.")
                _raise_window_failure(
                    context=context,
                    pages=pages,
                    page_index=page_index,
                    cause=cause,
                )
            seen_cursors.add(request_cursor)

        try:
            page = page_source.fetch_page(request_cursor)
            assert_calendar_run_ownership()
            next_cursor = _validate_page_envelope(
                page,
                request_cursor=request_cursor,
                context=context,
            )
            request_fingerprint = (
                page.request_descriptor.fingerprint
                if page.request_descriptor is not None
                else build_request_fingerprint(
                    method=page.request_method,
                    source_url=page.source_url,
                    request_identity=page.request_identity,
                )
            )
            if request_fingerprint in seen_request_fingerprints:
                raise EarningsCalendarPaginationError(
                    "Distinct pages must not share one persisted request identity."
                )
            seen_request_fingerprints.add(request_fingerprint)
        except EarningsCalendarPaginationError as error:
            _raise_window_failure(
                context=context,
                pages=pages,
                page_index=page_index,
                cause=error,
            )
        except EarningsCalendarRunOwnershipLost:
            raise
        except Exception as error:
            _raise_window_failure(
                context=context,
                pages=pages,
                page_index=page_index,
                cause=error,
            )

        try:
            ingestion_result = ingest_earnings_calendar_payload(
                sync_run=context.sync_run,
                parser=parser,
                raw_content=page.raw_content,
                provider_key=context.provider_key,
                provider_version=context.provider_version,
                source_url=page.source_url,
                fetched_at=page.fetched_at,
                request_method=page.request_method,
                request_identity=page.request_identity,
                http_status=page.http_status,
                content_type=page.content_type,
                encoding=page.encoding,
                request_descriptor=page.request_descriptor,
                on_raw_persisted=count_persisted_page,
                verify_run_ownership=assert_calendar_run_ownership,
            )
        except Exception as error:
            _raise_window_failure(
                context=context,
                pages=pages,
                page_index=page_index,
                cause=error,
            )

        page_result = EarningsCalendarWindowPageResult(
            page_index=page_index,
            cursor=request_cursor,
            next_cursor=next_cursor,
            is_terminal=page.is_terminal,
            ingestion=ingestion_result,
        )
        pages.append(page_result)

        if page.is_terminal:
            assert_calendar_run_ownership()
            finalized = mark_sync_run_succeeded(context.sync_run.pk)
            return EarningsCalendarWindowResult(
                sync_run=finalized,
                pages=tuple(pages),
                completed=True,
                observation_count=sum(
                    len(page_result.ingestion.observations) for page_result in pages
                ),
                observations_created=sum(
                    page_result.ingestion.observations_created for page_result in pages
                ),
                observations_reused=sum(
                    page_result.ingestion.observations_reused for page_result in pages
                ),
            )

        if next_cursor == request_cursor:
            cause = EarningsCalendarPaginationError("Pagination cursor did not progress.")
            _raise_window_failure(
                context=context,
                pages=pages,
                page_index=page_index,
                cause=cause,
            )
        if next_cursor in seen_cursors:
            cause = EarningsCalendarPaginationError("Pagination cursor cycle detected.")
            _raise_window_failure(
                context=context,
                pages=pages,
                page_index=page_index,
                cause=cause,
            )
        request_cursor = next_cursor
        page_index += 1


def _validate_window_context(
    *,
    sync_run: SyncRun,
    page_source: EarningsCalendarPageSource,
    parser: EarningsCalendarParser,
    provider_key: str,
    provider_version: str,
    max_pages: int,
) -> _WindowContext:
    if sync_run._state.adding or sync_run.pk is None:
        raise InvalidEarningsCalendarWindow("sync_run must be saved before use.")
    try:
        current_run = SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvalidEarningsCalendarWindow("sync_run no longer exists.") from error
    if current_run.status != SyncRun.Status.RUNNING:
        raise InvalidEarningsCalendarWindow("sync_run must be running.")
    if current_run.job_type != EARNINGS_CALENDAR_WINDOW_JOB_TYPE:
        raise InvalidEarningsCalendarWindow(
            f"sync_run job_type must be {EARNINGS_CALENDAR_WINDOW_JOB_TYPE!r}."
        )
    if current_run.source.source_type != DataSource.SourceType.EARNINGS_CALENDAR:
        raise InvalidEarningsCalendarWindow(
            "sync_run source must use the earnings_calendar source type."
        )
    normalized_provider_key = _require_text(
        provider_key,
        value_name="provider_key",
        maximum_length=64,
    )
    normalized_provider_version = _require_text(
        provider_version,
        value_name="provider_version",
        maximum_length=100,
    )
    if current_run.source.provider_adapter != normalized_provider_key:
        raise InvalidEarningsCalendarWindow(
            "provider_key must match the sync_run source provider_adapter."
        )
    if current_run.provider_version != normalized_provider_version:
        raise InvalidEarningsCalendarWindow(
            "provider_version must match the sync_run persisted provider context."
        )
    if not isinstance(page_source, EarningsCalendarPageSource):
        raise InvalidEarningsCalendarWindow(
            "page_source must implement EarningsCalendarPageSource."
        )
    if not isinstance(parser, EarningsCalendarParser):
        raise InvalidEarningsCalendarWindow("parser must implement EarningsCalendarParser.")
    if _has_prior_counts(current_run):
        raise InvalidEarningsCalendarWindow("sync_run must be a fresh window run with zero counts.")
    if (
        isinstance(max_pages, bool)
        or not isinstance(max_pages, int)
        or not 1 <= max_pages <= MAX_EARNINGS_CALENDAR_PAGES
    ):
        raise InvalidEarningsCalendarWindow(
            f"max_pages must be between 1 and {MAX_EARNINGS_CALENDAR_PAGES}."
        )
    return _WindowContext(
        sync_run=current_run,
        provider_key=normalized_provider_key,
        provider_version=normalized_provider_version,
        max_pages=max_pages,
    )


def _has_prior_counts(sync_run: SyncRun) -> bool:
    return any(
        count != 0
        for count in (
            sync_run.fetched_count,
            sync_run.created_count,
            sync_run.updated_count,
            sync_run.skipped_count,
            sync_run.failed_count,
        )
    )


def _validate_page_envelope(
    page: EarningsCalendarPage,
    *,
    request_cursor: str | None,
    context: _WindowContext,
) -> str | None:
    if not isinstance(page, EarningsCalendarPage):
        raise EarningsCalendarPaginationError("Page source must return an EarningsCalendarPage.")
    if page.cursor != request_cursor:
        raise EarningsCalendarPaginationError("Page cursor does not match the requested cursor.")
    if not isinstance(page.raw_content, bytes):
        raise EarningsCalendarPaginationError("Page raw_content must be bytes.")
    if not isinstance(page.is_terminal, bool):
        raise EarningsCalendarPaginationError("Page is_terminal must be a boolean.")
    if page.provider_key != context.provider_key:
        raise EarningsCalendarPaginationError(
            "Page provider_key does not match the window context."
        )
    if page.provider_version != context.provider_version:
        raise EarningsCalendarPaginationError(
            "Page provider_version does not match the window context."
        )
    if page.is_terminal:
        if page.next_cursor is not None:
            raise EarningsCalendarPaginationError("Terminal page must not define next_cursor.")
        return None
    if not isinstance(page.next_cursor, str) or not page.next_cursor.strip():
        raise EarningsCalendarPaginationError(
            "Non-terminal page must define a non-empty next_cursor."
        )
    normalized_next_cursor = page.next_cursor.strip()
    if len(normalized_next_cursor) > MAX_EARNINGS_CALENDAR_CURSOR_LENGTH:
        raise EarningsCalendarPaginationError("Pagination cursor exceeds the maximum length.")
    return normalized_next_cursor


def _raise_window_failure(
    *,
    context: _WindowContext,
    pages: list[EarningsCalendarWindowPageResult],
    page_index: int,
    cause: Exception,
) -> NoReturn:
    assert_calendar_run_ownership()
    summary = _failure_summary(page_index=page_index, cause=cause)
    update_sync_run_counts(context.sync_run.pk, failed_delta=1)
    if pages:
        finalized = mark_sync_run_partial(context.sync_run.pk, error_summary=summary)
    else:
        finalized = mark_sync_run_failed(context.sync_run.pk, error_summary=summary)
    raise EarningsCalendarWindowFailure(
        message=summary,
        sync_run=finalized,
        pages=tuple(pages),
        failed_page_index=page_index,
        cause=cause,
    ) from None


def _failure_summary(*, page_index: int, cause: Exception) -> str:
    if isinstance(cause, EarningsCalendarIngestionError):
        detail = str(cause)
    elif isinstance(cause, EarningsCalendarPaginationError):
        detail = str(cause)
    else:
        detail = type(cause).__name__
    return f"Earnings calendar window page {page_index} failed: {detail}"


def _require_text(value: object, *, value_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidEarningsCalendarWindow(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvalidEarningsCalendarWindow(f"{value_name} must not be empty.")
    if len(normalized) > maximum_length:
        raise InvalidEarningsCalendarWindow(
            f"{value_name} must contain at most {maximum_length} characters."
        )
    return normalized
