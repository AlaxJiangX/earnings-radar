from __future__ import annotations

import threading
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.db import close_old_connections, connections

from audit.models import DataSource, SyncRun
from audit.services import (
    SyncRunStartResult,
    mark_sync_run_failed,
    mark_sync_run_partial,
    mark_sync_run_succeeded,
    start_sync_run,
)
from earnings.calendar_parsing import (
    FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
    FixtureEarningsCalendarParser,
)
from earnings.services import (
    EARNINGS_CALENDAR_REQUEST_IDEMPOTENCY_PREFIX,
    EARNINGS_CALENDAR_SCHEDULED_IDEMPOTENCY_PREFIX,
    EARNINGS_CALENDAR_SCOPE_FIELDS,
    EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    MAX_REQUEST_ID_LENGTH,
    MAX_SCHEDULE_BUCKET_LENGTH,
    EarningsCalendarPage,
    EarningsCalendarSyncRunAlreadyRunning,
    EarningsCalendarSyncRunContextMismatch,
    EarningsCalendarSyncRunRetryRequired,
    EarningsCalendarWindowKind,
    InvalidEarningsCalendarSyncIdentity,
    build_earnings_calendar_sync_scope,
    build_manual_earnings_calendar_idempotency_key,
    build_scheduled_earnings_calendar_idempotency_key,
    run_earnings_calendar_window,
    start_scheduled_earnings_calendar_sync_run,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "earnings_calendar"
FIXTURE_FETCHED_AT = datetime(2026, 7, 14, 12, 0, 1, tzinfo=UTC)
PROVIDER_KEY = "fixture-earnings-calendar"
PROVIDER_VERSION = FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION
SOURCE_KEY = "fixture-earnings-calendar-source"
WINDOW_START = date(2026, 9, 1)
WINDOW_END = date(2026, 12, 20)
POOL_AS_OF = date(2026, 9, 21)
POOL_HASH = "a" * 64
SELECTOR_VERSION = "earnings-monitoring-pool-v1"
SCHEDULE_BUCKET = "2026-09-21T13:00:00Z"


def _scope(
    *,
    provider_key: str = PROVIDER_KEY,
    window_kind: EarningsCalendarWindowKind | str = EarningsCalendarWindowKind.SCHEDULED,
    window_start: date = WINDOW_START,
    window_end: date = WINDOW_END,
    monitoring_pool_as_of: date = POOL_AS_OF,
    monitoring_pool_hash: str = POOL_HASH,
    selector_version: str = SELECTOR_VERSION,
) -> dict[str, object]:
    return build_earnings_calendar_sync_scope(
        provider_key=provider_key,
        window_kind=window_kind,
        window_start=window_start,
        window_end=window_end,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
    )


def _scheduled_key(
    *,
    source_key: str = SOURCE_KEY,
    provider_key: str = PROVIDER_KEY,
    window_start: date = WINDOW_START,
    window_end: date = WINDOW_END,
    monitoring_pool_as_of: date = POOL_AS_OF,
    monitoring_pool_hash: str = POOL_HASH,
    selector_version: str = SELECTOR_VERSION,
    schedule_bucket: str = SCHEDULE_BUCKET,
) -> str:
    return build_scheduled_earnings_calendar_idempotency_key(
        source_key=source_key,
        provider_key=provider_key,
        window_start=window_start,
        window_end=window_end,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
        schedule_bucket=schedule_bucket,
    )


def _manual_key(
    *,
    source_key: str = SOURCE_KEY,
    provider_key: str = PROVIDER_KEY,
    window_kind: EarningsCalendarWindowKind | str = EarningsCalendarWindowKind.MANUAL,
    window_start: date = WINDOW_START,
    window_end: date = WINDOW_END,
    monitoring_pool_as_of: date = POOL_AS_OF,
    monitoring_pool_hash: str = POOL_HASH,
    selector_version: str = SELECTOR_VERSION,
    request_id: str = "operator-request-1",
) -> str:
    return build_manual_earnings_calendar_idempotency_key(
        source_key=source_key,
        provider_key=provider_key,
        window_kind=window_kind,
        window_start=window_start,
        window_end=window_end,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
        request_id=request_id,
    )


def _source(
    suffix: str,
    *,
    source_type: str = DataSource.SourceType.EARNINGS_CALENDAR,
    provider_adapter: str = PROVIDER_KEY,
    is_enabled: bool = True,
) -> DataSource:
    return DataSource.objects.create(
        key=f"{SOURCE_KEY}-{suffix}",
        name=f"Fixture earnings calendar {suffix}",
        source_type=source_type,
        base_url="https://fixture-earnings-calendar.test/",
        provider_adapter=provider_adapter,
        is_enabled=is_enabled,
        license_notes="Synthetic test-only source.",
    )


def _start(
    source: DataSource,
    *,
    provider_key: str = PROVIDER_KEY,
    window_start: date = WINDOW_START,
    window_end: date = WINDOW_END,
    monitoring_pool_as_of: date = POOL_AS_OF,
    monitoring_pool_hash: str = POOL_HASH,
    selector_version: str = SELECTOR_VERSION,
    schedule_bucket: str = SCHEDULE_BUCKET,
    provider_version: str = PROVIDER_VERSION,
    code_version: str = "",
    parser_version: str = "",
    started_at: datetime | None = None,
) -> SyncRunStartResult:
    return start_scheduled_earnings_calendar_sync_run(
        source=source,
        provider_key=provider_key,
        window_start=window_start,
        window_end=window_end,
        monitoring_pool_as_of=monitoring_pool_as_of,
        monitoring_pool_hash=monitoring_pool_hash,
        selector_version=selector_version,
        schedule_bucket=schedule_bucket,
        provider_version=provider_version,
        code_version=code_version,
        parser_version=parser_version,
        started_at=started_at,
    )


def test_scope_is_deterministic_and_contains_only_frozen_identity_fields() -> None:
    first = _scope()
    second = _scope()

    assert first == second
    assert tuple(first) == EARNINGS_CALENDAR_SCOPE_FIELDS
    assert first == {
        "capability": "earnings_calendar",
        "provider_key": PROVIDER_KEY,
        "window_kind": "scheduled",
        "window_start": "2026-09-01",
        "window_end": "2026-12-20",
        "monitoring_pool_as_of": "2026-09-21",
        "monitoring_pool_hash": POOL_HASH,
        "selector_version": SELECTOR_VERSION,
    }
    assert "company_ids" not in first


@pytest.mark.parametrize(
    "window_kind",
    (
        EarningsCalendarWindowKind.SCHEDULED,
        EarningsCalendarWindowKind.MANUAL,
        EarningsCalendarWindowKind.BACKFILL,
        EarningsCalendarWindowKind.RETRY,
        EarningsCalendarWindowKind.REPLAY,
    ),
)
def test_scope_supports_all_window_kinds(
    window_kind: EarningsCalendarWindowKind,
) -> None:
    assert _scope(window_kind=window_kind)["window_kind"] == window_kind.value


def test_scope_normalizes_window_kind_string() -> None:
    assert _scope(window_kind="  Backfill  ")["window_kind"] == "backfill"


def test_scope_rejects_window_start_after_window_end() -> None:
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="window_start"):
        _scope(window_start=date(2026, 9, 22), window_end=date(2026, 9, 21))


def test_scope_rejects_non_date_and_datetime_values() -> None:
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="window_start"):
        _scope(window_start=datetime(2026, 9, 1, tzinfo=UTC))
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="monitoring_pool_as_of"):
        _scope(monitoring_pool_as_of="2026-09-21")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "pool_hash",
    (
        "",
        "A" * 64,
        "a" * 63,
        "g" * 64,
        "a" * 65,
        123,
    ),
)
def test_scope_rejects_malformed_pool_hash(pool_hash: object) -> None:
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="monitoring_pool_hash"):
        _scope(monitoring_pool_hash=pool_hash)  # type: ignore[arg-type]


@pytest.mark.parametrize("selector_version", ("", "   ", "v" * 101, 123))
def test_scope_rejects_invalid_selector_version(selector_version: object) -> None:
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="selector_version"):
        _scope(selector_version=selector_version)  # type: ignore[arg-type]


def test_scope_rejects_credential_like_values_without_echoing_them() -> None:
    secret = "fixture-scope-secret"

    with pytest.raises(InvalidEarningsCalendarSyncIdentity) as excinfo:
        _scope(selector_version=f"token={secret}")

    assert secret not in str(excinfo.value)


def test_scheduled_key_is_deterministic_and_versioned() -> None:
    first = _scheduled_key()
    second = _scheduled_key(
        schedule_bucket=SCHEDULE_BUCKET,
        selector_version=SELECTOR_VERSION,
        monitoring_pool_hash=POOL_HASH,
        monitoring_pool_as_of=POOL_AS_OF,
        window_end=WINDOW_END,
        window_start=WINDOW_START,
        provider_key=PROVIDER_KEY,
        source_key=SOURCE_KEY,
    )

    assert first == second
    assert first.startswith(EARNINGS_CALENDAR_SCHEDULED_IDEMPOTENCY_PREFIX)
    assert len(first) <= 255


def test_scheduled_key_changes_for_every_identity_input() -> None:
    base = _scheduled_key()

    assert _scheduled_key(source_key="another-source") != base
    assert _scheduled_key(provider_key="another-provider") != base
    assert _scheduled_key(window_start=date(2026, 9, 2)) != base
    assert _scheduled_key(window_end=date(2026, 12, 21)) != base
    assert _scheduled_key(monitoring_pool_as_of=date(2026, 9, 22)) != base
    assert _scheduled_key(monitoring_pool_hash="b" * 64) != base
    assert _scheduled_key(selector_version="earnings-monitoring-pool-v2") != base
    assert _scheduled_key(schedule_bucket="2026-09-22T13:00:00Z") != base


def test_scheduled_key_does_not_contain_raw_identity_values() -> None:
    key = _scheduled_key(schedule_bucket="fixture-secret-bucket")

    assert "fixture-secret-bucket" not in key
    assert SOURCE_KEY not in key
    assert PROVIDER_KEY not in key


def test_scheduled_key_rejects_credential_like_bucket_without_leaking_it() -> None:
    secret = "fixture-bucket-secret"

    with pytest.raises(InvalidEarningsCalendarSyncIdentity) as excinfo:
        _scheduled_key(schedule_bucket=f"token={secret}")

    assert secret not in str(excinfo.value)


def test_manual_key_requires_explicit_request_id() -> None:
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="request_id"):
        _manual_key(request_id="   ")


def test_manual_key_is_deterministic_and_request_id_changes_identity() -> None:
    first = _manual_key(request_id="manual-request-1")
    replay = _manual_key(request_id="manual-request-1")
    retry = _manual_key(
        window_kind=EarningsCalendarWindowKind.RETRY,
        request_id="retry-request-1",
    )

    assert first == replay
    assert first.startswith(EARNINGS_CALENDAR_REQUEST_IDEMPOTENCY_PREFIX)
    assert retry != first
    assert len(retry) <= 255


def test_manual_key_rejects_scheduled_window_kind() -> None:
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="manual, backfill, or retry"):
        _manual_key(window_kind=EarningsCalendarWindowKind.SCHEDULED)


def test_manual_key_rejects_replay_window_kind() -> None:
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="manual, backfill, or retry"):
        _manual_key(window_kind=EarningsCalendarWindowKind.REPLAY)


@pytest.mark.django_db
def test_first_scheduled_start_creates_running_c3_compatible_run() -> None:
    source = _source("first")

    result = _start(source)

    assert result.created is True
    run = result.sync_run
    assert run.status == SyncRun.Status.RUNNING
    assert run.source_id == source.pk
    assert run.job_type == EARNINGS_CALENDAR_WINDOW_JOB_TYPE
    assert run.scope == _scope()
    assert run.idempotency_key == _scheduled_key(source_key=source.key)
    assert run.code_version == ""
    assert run.parser_version == ""
    assert run.provider_version == PROVIDER_VERSION
    assert (
        run.fetched_count,
        run.created_count,
        run.updated_count,
        run.skipped_count,
        run.failed_count,
    ) == (0, 0, 0, 0, 0)
    assert run.source.source_type == DataSource.SourceType.EARNINGS_CALENDAR
    assert run.source.provider_adapter == PROVIDER_KEY
    assert SyncRun.objects.count() == 1


@pytest.mark.django_db
def test_scheduled_start_requires_persisted_provider_version() -> None:
    source = _source("missing-provider-version")

    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="provider_version"):
        _start(source, provider_version="")

    assert SyncRun.objects.count() == 0


@pytest.mark.django_db
def test_duplicate_running_start_fails_closed_without_second_owner() -> None:
    source = _source("running")
    first = _start(source)

    with pytest.raises(EarningsCalendarSyncRunAlreadyRunning) as excinfo:
        _start(source)

    assert excinfo.value.created is False
    assert excinfo.value.sync_run.pk == first.sync_run.pk
    assert excinfo.value.sync_run.status == SyncRun.Status.RUNNING
    assert SyncRun.objects.count() == 1


@pytest.mark.django_db
def test_duplicate_succeeded_start_reuses_terminal_run_without_replay() -> None:
    source = _source("succeeded")
    first = _start(source)
    mark_sync_run_succeeded(first.sync_run.pk)

    replay = _start(source)

    assert replay.created is False
    assert replay.sync_run.pk == first.sync_run.pk
    assert replay.sync_run.status == SyncRun.Status.SUCCEEDED
    assert SyncRun.objects.count() == 1


@pytest.mark.parametrize("terminal_status", (SyncRun.Status.FAILED, SyncRun.Status.PARTIAL))
@pytest.mark.django_db
def test_failed_or_partial_scheduled_run_requires_a_new_retry_identity(
    terminal_status: str,
) -> None:
    source = _source(f"terminal-{terminal_status}")
    first = _start(source)
    if terminal_status == SyncRun.Status.FAILED:
        mark_sync_run_failed(first.sync_run.pk, error_summary="Fixture failure.")
    else:
        mark_sync_run_partial(first.sync_run.pk, error_summary="Fixture partial.")

    with pytest.raises(EarningsCalendarSyncRunRetryRequired) as excinfo:
        _start(source)

    assert excinfo.value.created is False
    assert excinfo.value.sync_run.pk == first.sync_run.pk
    assert excinfo.value.sync_run.status == terminal_status
    assert SyncRun.objects.count() == 1


@pytest.mark.django_db
def test_manual_retry_identity_does_not_masquerade_as_failed_scheduled_run() -> None:
    source = _source("manual-retry")
    failed = _start(source)
    mark_sync_run_failed(failed.sync_run.pk, error_summary="Fixture failure.")

    scheduled_key = _scheduled_key(source_key=source.key)
    retry_key = _manual_key(
        source_key=source.key,
        window_kind=EarningsCalendarWindowKind.RETRY,
        request_id="retry-request-1",
    )

    assert retry_key != scheduled_key
    assert not SyncRun.objects.filter(
        idempotency_key=retry_key,
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        source=source,
    ).exists()


@pytest.mark.django_db
def test_existing_scope_mismatch_under_same_key_fails_closed() -> None:
    source = _source("scope-mismatch")
    start_sync_run(
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        source=source,
        scope={"wrong": "scope"},
        idempotency_key=_scheduled_key(source_key=source.key),
    )

    with pytest.raises(EarningsCalendarSyncRunContextMismatch):
        _start(source)

    assert SyncRun.objects.count() == 1


@pytest.mark.django_db
def test_existing_code_or_parser_context_mismatch_fails_closed() -> None:
    source = _source("version-mismatch")
    first = _start(source, code_version="code-v1", parser_version="parser-v1")
    mark_sync_run_succeeded(first.sync_run.pk)

    with pytest.raises(EarningsCalendarSyncRunContextMismatch):
        _start(source, code_version="code-v2", parser_version="parser-v1")

    assert SyncRun.objects.count() == 1


@pytest.mark.django_db
def test_wrong_source_type_is_rejected() -> None:
    source = _source("wrong-type", source_type=DataSource.SourceType.SEC)

    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="earnings_calendar"):
        _start(source)

    assert SyncRun.objects.count() == 0


@pytest.mark.django_db
def test_disabled_source_is_rejected() -> None:
    source = _source("disabled", is_enabled=False)

    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="enabled"):
        _start(source)

    assert SyncRun.objects.count() == 0


@pytest.mark.django_db
def test_provider_mismatch_is_rejected() -> None:
    source = _source("provider-mismatch", provider_adapter="another-provider")

    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="provider_adapter"):
        _start(source)

    with pytest.raises(InvalidEarningsCalendarSyncIdentity):
        _start(source, provider_key="UPPERCASE")

    assert SyncRun.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_concurrent_exact_scheduled_identity_creates_at_most_one_run() -> None:
    source = _source("concurrent")
    source_id = source.pk
    barrier = threading.Barrier(2, timeout=10)
    results: list[SyncRunStartResult] = []
    errors: list[BaseException] = []

    def worker() -> None:
        close_old_connections()
        try:
            barrier.wait()
            current_source = DataSource.objects.get(pk=source_id)
            results.append(_start(current_source))
        except Exception as error:
            errors.append(error)
        finally:
            for current_connection in connections.all():
                current_connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    for thread in threads:
        assert not thread.is_alive(), "Concurrent scheduled start thread hung"
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], EarningsCalendarSyncRunAlreadyRunning)
    assert errors[0].created is False
    assert SyncRun.objects.count() == 1


def test_source_identity_rejects_unsupported_values() -> None:
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="source_key"):
        _scheduled_key(source_key="   ")
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="source_key"):
        _scheduled_key(source_key="s" * 65)


def test_failed_scheduled_retry_uses_new_request_identity() -> None:
    first_retry = _manual_key(
        window_kind=EarningsCalendarWindowKind.RETRY,
        request_id="retry-request-1",
    )
    second_retry = _manual_key(
        window_kind=EarningsCalendarWindowKind.RETRY,
        request_id="retry-request-2",
    )

    assert first_retry != second_retry


def test_uuid_type_is_not_silently_accepted_as_source_identity() -> None:
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="source_key"):
        _scheduled_key(source_key=uuid.uuid4())  # type: ignore[arg-type]


@pytest.mark.django_db
def test_distinct_sources_never_share_one_scheduled_identity() -> None:
    first_source = _source("identity-a")
    second_source = _source("identity-b")

    first = _start(first_source)
    second = _start(second_source)

    assert first.created is True
    assert second.created is True
    assert first.sync_run.pk != second.sync_run.pk
    assert first.sync_run.idempotency_key != second.sync_run.idempotency_key
    assert SyncRun.objects.count() == 2

    with pytest.raises(EarningsCalendarSyncRunAlreadyRunning) as excinfo:
        _start(first_source)

    assert excinfo.value.sync_run.pk == first.sync_run.pk
    assert SyncRun.objects.count() == 2


def test_scheduled_and_request_namespaces_do_not_collide_on_equal_labels() -> None:
    shared_label = "shared-identity-label"

    keys = {
        _scheduled_key(schedule_bucket=shared_label),
        _manual_key(window_kind=EarningsCalendarWindowKind.MANUAL, request_id=shared_label),
        _manual_key(window_kind=EarningsCalendarWindowKind.BACKFILL, request_id=shared_label),
        _manual_key(window_kind=EarningsCalendarWindowKind.RETRY, request_id=shared_label),
    }

    assert len(keys) == 4


@pytest.mark.parametrize(
    ("label", "field", "replacement"),
    (
        ("capability", "capability", "investor_relations"),
        ("provider", "provider_key", "another-provider"),
        ("kind", "window_kind", EarningsCalendarWindowKind.MANUAL.value),
        ("start", "window_start", "2026-09-02"),
        ("end", "window_end", "2026-12-21"),
        ("poolasof", "monitoring_pool_as_of", "2026-09-22"),
        ("poolhash", "monitoring_pool_hash", "b" * 64),
        ("selector", "selector_version", "earnings-monitoring-pool-v2"),
    ),
)
@pytest.mark.django_db
def test_existing_run_scope_field_mismatch_fails_closed(
    label: str,
    field: str,
    replacement: str,
) -> None:
    source = _source(f"drift-{label}")
    drifted_scope = _scope()
    drifted_scope[field] = replacement
    start_sync_run(
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        source=source,
        scope=drifted_scope,
        idempotency_key=_scheduled_key(source_key=source.key),
    )

    with pytest.raises(EarningsCalendarSyncRunContextMismatch):
        _start(source)

    assert SyncRun.objects.count() == 1


@pytest.mark.django_db
def test_succeeded_duplicate_with_drifted_scope_is_not_silently_reused() -> None:
    source = _source("succeeded-drift")
    drifted_scope = _scope()
    drifted_scope["monitoring_pool_hash"] = "c" * 64
    drifted = start_sync_run(
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        source=source,
        scope=drifted_scope,
        idempotency_key=_scheduled_key(source_key=source.key),
    )
    mark_sync_run_succeeded(drifted.pk)

    with pytest.raises(EarningsCalendarSyncRunContextMismatch):
        _start(source)

    assert SyncRun.objects.count() == 1


@pytest.mark.parametrize(
    ("label", "credential_value"),
    (
        ("token", "token=super-secret"),
        ("password", "password=super-secret"),
        ("authorization", "Authorization: Bearer super-secret"),
        ("userinfo-url", "https://user:password@example.com/calendar"),
        ("token-url", "https://example.com/calendar?token=super-secret"),
    ),
)
@pytest.mark.django_db
def test_credential_like_schedule_bucket_is_rejected_without_write_or_leak(
    label: str,
    credential_value: str,
) -> None:
    source = _source(f"credential-{label}")

    with pytest.raises(InvalidEarningsCalendarSyncIdentity) as excinfo:
        _start(source, schedule_bucket=credential_value)

    assert "super-secret" not in str(excinfo.value)
    assert "password" not in str(excinfo.value)
    assert SyncRun.objects.count() == 0


def test_identity_text_normalization_is_deterministic_and_bounded() -> None:
    assert _scheduled_key(schedule_bucket="  bucket-1  ") == _scheduled_key(
        schedule_bucket="bucket-1"
    )
    assert _scheduled_key(selector_version="  v1  ") == _scheduled_key(selector_version="v1")
    newline_bucket = _scheduled_key(schedule_bucket="bucket\n2")
    assert newline_bucket == _scheduled_key(schedule_bucket="bucket\n2")
    assert newline_bucket != _scheduled_key(schedule_bucket="bucket 2")
    assert _manual_key(request_id="  request-1  ") == _manual_key(request_id="request-1")

    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="schedule_bucket"):
        _scheduled_key(schedule_bucket="b" * (MAX_SCHEDULE_BUCKET_LENGTH + 1))
    with pytest.raises(InvalidEarningsCalendarSyncIdentity, match="request_id"):
        _manual_key(request_id="r" * (MAX_REQUEST_ID_LENGTH + 1))


class _SingleTerminalPageSource:
    """Minimal offline page source used to prove C-4 to C-3 handoff."""

    def __init__(self, page: EarningsCalendarPage) -> None:
        self.page = page
        self.calls: list[str | None] = []

    def fetch_page(self, cursor: str | None) -> EarningsCalendarPage:
        self.calls.append(cursor)
        return self.page


@pytest.mark.django_db
def test_created_scheduled_run_satisfies_c3_window_preconditions() -> None:
    source = _source("c3-handoff")
    created = _start(source)
    page = EarningsCalendarPage(
        cursor=None,
        raw_content=(FIXTURE_DIR / "empty_payload.json").read_bytes(),
        source_url="https://fixture-earnings-calendar.test/calendar",
        fetched_at=FIXTURE_FETCHED_AT,
        provider_key=PROVIDER_KEY,
        provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        is_terminal=True,
        next_cursor=None,
        request_identity={"cursor": None},
    )

    result = run_earnings_calendar_window(
        sync_run=created.sync_run,
        page_source=_SingleTerminalPageSource(page),
        parser=FixtureEarningsCalendarParser(),
        provider_key=PROVIDER_KEY,
        provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
    )

    assert result.completed is True
    assert result.observation_count == 0
    finalized = SyncRun.objects.get(pk=created.sync_run.pk)
    assert finalized.status == SyncRun.Status.SUCCEEDED
    assert finalized.scope == _scope()
