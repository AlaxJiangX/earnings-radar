"""Stage 6.2W integration tests: sync completion enqueues notifications.

Each wired orchestration path is exercised end to end (real production
service functions, no direct dispatch calls), plus rerun dedup and the
outbox rollback requirement.
"""

from __future__ import annotations

import uuid
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import transaction

from accounts.models import User
from audit.models import DataSource
from companies.models import Company, SecurityListing
from earnings.models import EarningsEvent
from earnings.services import evaluate_investor_relations_observation
from earnings.services.ir_confirmation import InvestorRelationsEvaluationResult
from filings.models import Filing
from filings.sync import sync_sec_filings
from indexes.models import IndexMembership, MarketIndex
from notifications.models import Notification
from providers.sec_edgar import SecEdgarProvider
from tests.earnings.helpers import make_event
from tests.earnings.ir_helpers import (
    ingest_ir,
    ir_item,
    ir_payload,
    make_ir_company,
    make_ir_source,
    make_ir_sync_run,
)
from tests.filings.sec_sync_helpers import FilingSpec, SecFilingTransport
from watchlists.services import add_watchlist_item

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 10, 24)
EMAIL = "email"
IN_APP = "in_app"


def _user(suffix: str) -> User:
    return User.objects.create_user(
        email=f"wiring-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-only",
    )


def _membership(company: Company, group: str) -> None:
    index, _ = MarketIndex.objects.get_or_create(
        code="SP500" if group == "LARGE" else "RUSSELL2000",
        defaults={
            "name": "S&P 500" if group == "LARGE" else "Russell 2000",
            "index_group": group,
            "is_enabled": True,
        },
    )
    listing = company.security_listings.first()
    assert listing is not None
    IndexMembership.objects.create(
        index=index,
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )


def _listing(company: Company) -> SecurityListing:
    return SecurityListing.objects.create(
        company=company,
        ticker=f"WR{company.pk.hex[:4].upper()}",
        exchange="NYSE",
        security_name=f"{company.display_name} Common",
        security_type="common_stock",
        effective_from=date(2026, 1, 1),
    )


def _watch(user: User, company: Company, suffix: str) -> object:
    return add_watchlist_item(
        user=user,
        company_id=company.pk,
        as_of=AS_OF,
        reason="wiring test",
        request_id=f"wiring-add-{suffix}",
    )


def _statuses(**filters: object) -> set[tuple[str, str]]:
    return {(row.channel, row.status) for row in Notification.objects.filter(**filters)}


def _priorities(user: User) -> set[str]:
    return {row.priority for row in Notification.objects.filter(user=user)}


class TestFilingWiring:
    def test_sec_sync_enqueues_notifications_and_rerun_dedups(self) -> None:
        from earnings.services import (
            EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
            select_monitoring_pool,
        )

        watcher = _user("sec-watcher")
        plain = _user("sec-plain")
        company = Company.objects.create(
            legal_name="SEC Wiring", display_name="SEC Wiring", cik="0000001234"
        )
        listing = SecurityListing.objects.create(
            company=company,
            ticker="SECW",
            exchange="NYSE",
            security_name="SEC Wiring common",
            security_type="common_stock",
            effective_from=date(2026, 1, 1),
        )
        index, _ = MarketIndex.objects.get_or_create(
            code="SP500",
            defaults={"name": "S&P 500", "index_group": "LARGE", "is_enabled": True},
        )
        IndexMembership.objects.create(
            index=index,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
        _watch(watcher, company, "sec")
        snapshot = select_monitoring_pool(
            as_of=AS_OF,
            selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
            enabled_index_codes=("SP500",),
        ).snapshot
        from audit.models import DataSource

        source = DataSource.objects.create(
            key=f"sec-wiring-{uuid.uuid4().hex[:8]}",
            name="SEC official wiring",
            source_type=DataSource.SourceType.SEC,
            base_url="https://data.sec.gov",
            is_official=True,
            provider_adapter="sec-edgar",
        )
        company_ids = (company.pk,)
        transport = SecFilingTransport(
            FilingSpec(form="8-K", acceptance_datetime="2026-10-20T16:30:00")
        )
        provider = SecEdgarProvider(
            user_agent="Earnings Radar test@example.org", transport=transport
        )

        result = sync_sec_filings(
            source=source,
            provider=provider,
            company_ids=company_ids,
            pool_as_of=snapshot.as_of_date,
            pool_selector_version=snapshot.selector_version,
            pool_hash=snapshot.pool_hash,
            idempotency_key=f"sec-wiring-{uuid.uuid4().hex[:8]}",
        )

        assert result.sync_run.status == "succeeded"
        filing = Filing.objects.get()
        assert _statuses() == {(EMAIL, "pending"), (IN_APP, "pending")}
        assert {row.event_type for row in Notification.objects.all()} == {"filing"}
        assert _priorities(watcher) == {"P0"}  # A watchlist
        assert _priorities(plain) == {"P3"}  # B non-watchlist
        assert {row.related_object_id for row in Notification.objects.all()} == {filing.pk}

        # Rerun with a fresh sync run: filing_is_complete skips the accession.
        transport_two = SecFilingTransport(
            FilingSpec(form="8-K", acceptance_datetime="2026-10-20T16:30:00")
        )
        provider_two = SecEdgarProvider(
            user_agent="Earnings Radar test@example.org", transport=transport_two
        )
        sync_sec_filings(
            source=source,
            provider=provider_two,
            company_ids=company_ids,
            pool_as_of=snapshot.as_of_date,
            pool_selector_version=snapshot.selector_version,
            pool_hash=snapshot.pool_hash,
            idempotency_key=f"sec-wiring-{uuid.uuid4().hex[:8]}",
        )

        # First run: 2 users x 2 channels; rerun enqueued nothing.
        assert Notification.objects.count() == 4


class TestIRWiring:
    def _ir_context(
        self, suffix: str, **event_overrides: object
    ) -> tuple[Company, DataSource, EarningsEvent]:
        company = make_ir_company(suffix)
        _listing(company)
        _membership(company, "LARGE")
        source = make_ir_source(suffix)
        event = make_event(
            company=company,
            period_end_date=date(2026, 9, 30),
            period_type="Q3",
            **event_overrides,
        )
        return company, source, event

    def _evaluate(
        self, company: Company, source: DataSource, item: dict[str, object]
    ) -> InvestorRelationsEvaluationResult:
        run = make_ir_sync_run(
            company=company,
            source=source,
            request_id=f"ir-wiring-{uuid.uuid4().hex[:8]}",
        )
        ingestion = ingest_ir(sync_run=run, payload=ir_payload([item]), company=company)
        assert len(ingestion.observations) == 1
        return evaluate_investor_relations_observation(
            observation=ingestion.observations[0],
            sync_run=run,
        )

    def test_release_transition_enqueues_notifications(self) -> None:
        watcher = _user("ir-watcher")
        plain = _user("ir-plain")
        company, source, event = self._ir_context("release", status="scheduled_confirmed")
        _watch(watcher, company, "ir-release")

        evaluation = self._evaluate(
            company,
            source,
            ir_item(
                company,
                item_type="results_release",
                earnings_release={"value": "2026-10-20", "precision": "date_only"},
            ),
        )

        assert evaluation.decision.decision_type == "released"
        event.refresh_from_db()
        assert event.status == "released"
        assert {row.event_type for row in Notification.objects.all()} == {"earnings_released"}
        assert _priorities(watcher) == {"P1"}  # A release
        assert _priorities(plain) == {"P1"}  # B release
        assert _statuses() == {(EMAIL, "pending"), (IN_APP, "pending")}

    def test_confirmation_refinement_does_not_notify(self) -> None:
        watcher = _user("ir-confirm")
        company, source, event = self._ir_context("confirm")
        _watch(watcher, company, "ir-confirm")

        evaluation = self._evaluate(
            company,
            source,
            ir_item(company, confirmed_release="2026-10-20"),
        )

        assert evaluation.decision.decision_type == "confirmed_schedule"
        assert Notification.objects.count() == 0


class TestIndexWiring:
    def _pool_facts(self, suffix: str) -> tuple[User, User, Company, SecurityListing]:
        watcher = _user(f"idx-{suffix}-watcher")
        plain = _user(f"idx-{suffix}-plain")
        company = Company.objects.create(
            legal_name=f"Index Wiring {suffix}",
            display_name=f"Index Wiring {suffix}",
        )
        listing = SecurityListing.objects.create(
            company=company,
            ticker=f"IW{suffix[:3].upper()}",
            exchange="NYSE",
            security_name=f"{company.display_name} Common",
            security_type="common_stock",
            effective_from=date(2026, 1, 1),
        )
        return watcher, plain, company, listing

    def test_added_leg_enqueues_after_classification(self) -> None:
        from indexes.services import classify_index_change_event, record_index_change_leg

        watcher, plain, company, listing = self._pool_facts("add")
        _watch(watcher, company, "idx-add")
        sp500, _ = MarketIndex.objects.get_or_create(
            code="SP500",
            defaults={"name": "S&P 500", "index_group": "LARGE", "is_enabled": True},
        )

        write = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=AS_OF,
        )
        assert Notification.objects.count() == 0  # not yet classified

        classify_index_change_event(write.event)

        assert {row.event_type for row in Notification.objects.all()} == {"index_added"}
        assert _priorities(watcher) == {"P1"}
        assert _priorities(plain) == {"P1"}  # B via the ADDED leg's index group
        assert Notification.objects.count() == 4

        # Rerun the classification: the idempotency key dedups.
        classify_index_change_event(write.event)
        assert Notification.objects.count() == 4

    def test_full_exit_enqueues_removed_and_exit(self) -> None:
        from indexes.services import classify_index_change_event, record_index_change_leg

        watcher, plain, company, listing = self._pool_facts("exit")
        _watch(watcher, company, "idx-exit")
        russell, _ = MarketIndex.objects.get_or_create(
            code="RUSSELL2000",
            defaults={"name": "Russell 2000", "index_group": "SMALL", "is_enabled": True},
        )
        IndexMembership.objects.create(
            index=russell,
            security_listing=listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )

        write = record_index_change_leg(
            company=company,
            security_listing=listing,
            index=russell,
            action="removed",
            effective_date=AS_OF,
        )
        classify_index_change_event(write.event)

        write.event.refresh_from_db()
        assert write.event.monitoring_impact == "exits_base_pool"
        owner_types = {
            row.event_type: row.priority for row in Notification.objects.filter(user=watcher)
        }
        assert owner_types == {
            "index_removed": "P3",  # SMALL removal
            "full_exit": "P0",  # S watchlist
        }
        plain_types = {
            row.event_type: row.priority for row in Notification.objects.filter(user=plain)
        }
        assert plain_types == {"index_removed": "P3", "full_exit": "P3"}
        # 2 users x 2 channels x (removed + full_exit)
        assert Notification.objects.count() == 8

        classify_index_change_event(write.event)
        assert Notification.objects.count() == 8


class TestWiringRollback:
    def test_ir_evaluation_rollback_leaves_no_notifications(self) -> None:
        watcher = _user("rollback-watcher")
        company = make_ir_company("rollback")
        _listing(company)
        source = make_ir_source("rollback")
        make_event(
            company=company,
            period_end_date=date(2026, 9, 30),
            period_type="Q3",
            status="scheduled_confirmed",
        )
        _watch(watcher, company, "rollback")
        run = make_ir_sync_run(company=company, source=source, request_id="ir-rollback")
        ingestion = ingest_ir(
            sync_run=run,
            payload=ir_payload(
                [
                    ir_item(
                        company,
                        item_type="results_release",
                        earnings_release={"value": "2026-10-20", "precision": "date_only"},
                    )
                ]
            ),
            company=company,
        )

        with pytest.raises(RuntimeError, match="boom"), transaction.atomic():
            evaluate_investor_relations_observation(
                observation=ingestion.observations[0],
                sync_run=run,
            )
            raise RuntimeError("boom")

        assert Notification.objects.count() == 0


class TestAdvanceReminderCommand:
    def test_dry_run_reports_without_writing(self) -> None:
        watcher = _user("cmd-dry")
        company = make_ir_company("cmd-dry")
        _listing(company)
        _watch(watcher, company, "cmd-dry")
        make_event(
            company=company,
            status="scheduled_confirmed",
            confirmed_release_date=date(2026, 11, 3),
            confirmed_release_precision="date_only",
        )

        out = StringIO()
        call_command("enqueue_advance_reminders", "--as-of=2026-11-02", "--dry-run", stdout=out)

        assert "dry_run=true" in out.getvalue()
        assert Notification.objects.count() == 0

    def test_real_run_enqueues_and_rerun_dedups(self) -> None:
        watcher = _user("cmd-real")
        plain = _user("cmd-plain")
        company = make_ir_company("cmd-real")
        _listing(company)
        _membership(company, "LARGE")
        _watch(watcher, company, "cmd-real")
        make_event(
            company=company,
            status="scheduled_confirmed",
            confirmed_release_date=date(2026, 11, 3),
            confirmed_release_precision="date_only",
        )

        call_command("enqueue_advance_reminders", "--as-of=2026-11-02")
        assert Notification.objects.count() == 4
        assert _priorities(watcher) == {"P1"}  # A, 24h band
        assert _priorities(plain) == {"P2"}  # B, 24h band

        call_command("enqueue_advance_reminders", "--as-of=2026-11-02")
        assert Notification.objects.count() == 4
