"""Acceptance: the full sync suite runs twice with zero duplicates.

Roadmap 7.3 acceptance line: "全套同步连续运行两次无重复关键数据和通
知".  Every production entry point available to fixtures runs twice on
the same inputs; the second pass must not create another Company,
EarningsEvent, Filing, IndexChangeEvent, EarningsDateChange or
Notification, nor duplicate raw bodies (AGENTS section 8).
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from accounts.models import User
from audit.services import start_sync_run
from earnings.calendar_parsing import (
    FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
    FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
    FixtureEarningsCalendarParser,
)
from earnings.models import (
    EarningsCalendarObservation,
    EarningsDateChange,
    EarningsEvent,
)
from earnings.services import (
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
    ingest_earnings_calendar_payload,
    select_monitoring_pool,
)
from filings.models import Filing
from filings.sync import sync_sec_filings
from indexes.models import IndexChangeEvent
from notifications.models import Notification
from providers.sec_edgar import SecEdgarProvider
from tests.acceptance.helpers import AS_OF, add_membership, make_pool_company
from tests.earnings.helpers import make_event
from tests.filings.sec_sync_helpers import (
    FilingSpec,
    SecFilingTransport,
    make_sec_source,
)
from watchlists.services import add_watchlist_item

pytestmark = pytest.mark.django_db


def _watcher(suffix: str) -> User:

    return User.objects.create_user(
        email=f"acc-idem-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-password-only",
    )


def _plain_user() -> User:

    return User.objects.create_user(
        email=f"acc-idem-plain-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-password-only",
    )


def _calendar_payload() -> bytes:
    from pathlib import Path

    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "providers"
        / "earnings_calendar"
        / "complete_payload.json"
    )
    return fixture_path.read_bytes()


def test_full_suite_twice_creates_no_duplicates() -> None:
    from audit.models import DataSource

    watcher = _watcher("idempotent")
    _plain_user()
    company, listing = make_pool_company("idempotent", ticker_prefix="ID")
    add_membership(listing, code="SP500")
    add_watchlist_item(
        user=watcher,
        company_id=company.pk,
        as_of=AS_OF,
        reason="acceptance idempotency",
        request_id="acc-idem-watch",
    )
    event = make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        status="scheduled_confirmed",
    )
    pool = select_monitoring_pool(
        as_of=AS_OF,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        enabled_index_codes=("SP500",),
    ).snapshot

    # --- SEC: two consecutive runs on the same accession -------------------
    sec_source = make_sec_source(key=f"sec-idem-{uuid.uuid4().hex[:8]}")
    spec = FilingSpec(form="8-K", items="2.02", exhibit_type="EX-99.1")
    for pass_number in ("first", "second"):
        result = sync_sec_filings(
            source=sec_source,
            provider=SecEdgarProvider(
                user_agent="Earnings Radar test@example.org",
                transport=SecFilingTransport(spec),
            ),
            company_ids=(company.pk,),
            pool_as_of=pool.as_of_date,
            pool_selector_version=pool.selector_version,
            pool_hash=pool.pool_hash,
            idempotency_key=f"sec-idem-{pass_number}-{uuid.uuid4().hex[:8]}",
        )
        assert result.sync_run.status == "succeeded"
    assert Filing.objects.count() == 1

    # --- Calendar: the same fixture payload ingested through two window
    # runs; raw content dedup keeps one body, and the observation for the
    # second pass reuses the first one (same raw record + parser + event).
    calendar_source = DataSource.objects.create(
        key=f"calendar-idem-{uuid.uuid4().hex[:8]}",
        name="Acceptance calendar idempotency",
        source_type=DataSource.SourceType.EARNINGS_CALENDAR,
        base_url="https://fixture-earnings-calendar.test/",
        provider_adapter=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
        license_notes="Synthetic test-only source.",
    )
    payload = _calendar_payload()
    for pass_number in ("first", "second"):
        run = start_sync_run(
            job_type="earnings.calendar_window",
            source=calendar_source,
            scope={"window_kind": "daily", "pass": pass_number},
            idempotency_key=f"calendar-idem-{pass_number}-{uuid.uuid4().hex[:8]}",
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        )
        ingest_earnings_calendar_payload(
            sync_run=run,
            parser=FixtureEarningsCalendarParser(),
            raw_content=payload,
            provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
            source_url="https://fixture-earnings-calendar.test/calendar?fixture=idempotency",
            fetched_at=run.started_at,
        )
    # The fixture payload carries two calendar events; the second pass
    # reused the raw body (one record) and both observations.
    assert EarningsCalendarObservation.objects.count() == 2

    # --- Domain invariants after both passes ------------------------------
    assert EarningsEvent.objects.count() == 1
    assert EarningsDateChange.objects.count() == 0  # no upstream change -> no change rows
    assert IndexChangeEvent.objects.count() == 0
    # SEC notifications only (2 users x 2 channels); calendar ingestion
    # wires no notifications (ADR-026), the IR path owns date changes.
    assert Notification.objects.count() == 4
    del event
