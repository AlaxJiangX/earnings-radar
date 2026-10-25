"""Acceptance: the SEC pipeline, end to end on fixtures.

Fixture submissions -> sync_sec_filings -> Filing persisted with raw
lineage -> filing notifications enqueued (ADR-026 wiring point 1) ->
FilingEarningsLink classified YES against a scheduled event (ADR-021
metadata-only v1) -> rerun with the same accession dedups everything.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from accounts.models import User
from audit.models import DataSource
from companies.models import Company
from earnings.models import EarningsEvent, FilingEarningsLink, MonitoringPoolSnapshot
from earnings.services import (
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
    select_monitoring_pool,
)
from earnings.services.filing_links import evaluate_filing_earnings_link
from filings.models import Filing
from filings.sync import sync_sec_filings
from notifications.models import Notification
from providers.sec_edgar import SecEdgarProvider
from tests.acceptance.helpers import AS_OF, add_membership, make_pool_company
from tests.earnings.filing_helpers import (
    add_raw_observation_for_run,
    make_sec_sync_run,
)
from tests.earnings.helpers import make_event
from tests.filings.sec_sync_helpers import FilingSpec, SecFilingTransport
from watchlists.services import add_watchlist_item

pytestmark = pytest.mark.django_db


def _watched_pool(
    suffix: str,
) -> tuple[User, Company, DataSource, MonitoringPoolSnapshot]:
    from tests.filings.sec_sync_helpers import make_sec_source

    watcher_user = User.objects.create_user(
        email=f"acc-sec-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-password-only",
    )
    # One non-watchlist pool user keeps the fan-out counts deterministic
    # (2 users x 2 channels per occurrence).
    User.objects.create_user(
        email=f"acc-sec-plain-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-password-only",
    )
    company, listing = make_pool_company(suffix, ticker_prefix="SC")
    add_membership(listing, code="SP500")
    add_watchlist_item(
        user=watcher_user,
        company_id=company.pk,
        as_of=AS_OF,
        reason="acceptance sec",
        request_id=f"acc-sec-{suffix}",
    )
    source = make_sec_source(key=f"sec-acceptance-{suffix}-{uuid.uuid4().hex[:8]}")
    snapshot = select_monitoring_pool(
        as_of=AS_OF,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        enabled_index_codes=("SP500",),
    ).snapshot
    return watcher_user, company, source, snapshot


def test_sec_submission_to_link_classification_and_notifications() -> None:
    _watcher, company, source, snapshot = _watched_pool("e2e")
    make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        status="scheduled_confirmed",
        estimated_release_date=date(2026, 10, 20),
        estimated_release_precision="date_only",
    )
    spec = FilingSpec(
        form="8-K",
        items="2.02",
        exhibit_type="EX-99.1",
        acceptance_datetime="2026-10-20T16:30:00",
    )

    result = sync_sec_filings(
        source=source,
        provider=SecEdgarProvider(
            user_agent="Earnings Radar test@example.org", transport=SecFilingTransport(spec)
        ),
        company_ids=(company.pk,),
        pool_as_of=snapshot.as_of_date,
        pool_selector_version=snapshot.selector_version,
        pool_hash=snapshot.pool_hash,
        idempotency_key=f"sec-acceptance-e2e-{uuid.uuid4().hex[:8]}",
    )

    assert result.sync_run.status == "succeeded"
    filing = Filing.objects.get()
    assert filing.form_type == "8-K"
    # SEC acceptance datetimes are Eastern time; the column stores UTC.
    eastern = ZoneInfo("America/New_York")
    assert filing.accepted_at == datetime(2026, 10, 20, 16, 30, tzinfo=eastern)

    # ADR-026 wiring point 1: the sync completion enqueued the filing
    # notification (watcher P0 watchlist; other pool users P3).
    priorities = {(row.related_object_id, row.priority) for row in Notification.objects.all()}
    assert filing.pk in {pair[0] for pair in priorities}
    assert "P0" in {pair[1] for pair in priorities}

    # ADR-021 metadata-only classification against the scheduled event.
    evaluation = evaluate_filing_earnings_link(filing=filing, sync_run=result.sync_run)
    assert evaluation.outcome == "matched_release_filing"
    assert evaluation.link is not None
    link: FilingEarningsLink = evaluation.link
    assert link.earnings_event is not None
    assert link.release_filing_classification == "YES"
    assert link.classification_reason == "ITEM_202_WITH_EARNINGS_EXHIBIT"
    assert link.review_status == "auto"
    event = EarningsEvent.objects.get(company=company, period_type="Q3")
    assert link.earnings_event_id == event.pk

    # Rerun with the same accession: nothing duplicates (AGENTS section 8).
    rerun = sync_sec_filings(
        source=source,
        provider=SecEdgarProvider(
            user_agent="Earnings Radar test@example.org", transport=SecFilingTransport(spec)
        ),
        company_ids=(company.pk,),
        pool_as_of=snapshot.as_of_date,
        pool_selector_version=snapshot.selector_version,
        pool_hash=snapshot.pool_hash,
        idempotency_key=f"sec-acceptance-e2e-rerun-{uuid.uuid4().hex[:8]}",
    )
    assert rerun.sync_run.status == "succeeded"
    assert Filing.objects.count() == 1
    assert (
        Notification.objects.count()
        == Notification.objects.filter(related_object_id=filing.pk).count()
    )


@pytest.mark.django_db
def test_link_evaluation_rerun_keeps_single_link() -> None:
    _watcher, company, source, snapshot = _watched_pool("rerun")
    make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        status="scheduled_confirmed",
        estimated_release_date=date(2026, 10, 20),
        estimated_release_precision="date_only",
    )
    spec = FilingSpec(
        form="8-K", items="2.02", exhibit_type="EX-99.1", acceptance_datetime="2026-10-20T16:30:00"
    )
    result = sync_sec_filings(
        source=source,
        provider=SecEdgarProvider(
            user_agent="Earnings Radar test@example.org", transport=SecFilingTransport(spec)
        ),
        company_ids=(company.pk,),
        pool_as_of=snapshot.as_of_date,
        pool_selector_version=snapshot.selector_version,
        pool_hash=snapshot.pool_hash,
        idempotency_key=f"sec-acceptance-rerun-{uuid.uuid4().hex[:8]}",
    )
    filing = Filing.objects.get()

    first = evaluate_filing_earnings_link(filing=filing, sync_run=result.sync_run)
    # A rerun may arrive on a new SyncRun, but it must have observed the
    # filing's raw evidence (AGENTS section 9 source-reload rule).
    rerun_sync_run = make_sec_sync_run(source=source, suffix="link-rerun")
    add_raw_observation_for_run(filing=filing, sync_run=rerun_sync_run)
    second = evaluate_filing_earnings_link(
        filing=filing,
        sync_run=rerun_sync_run,
    )

    assert first.link is not None and second.link is not None
    assert first.link.pk == second.link.pk
    assert FilingEarningsLink.objects.count() == 1
    assert Notification.objects.count() == 4  # 1 filing event x 2 users x 2 channels
