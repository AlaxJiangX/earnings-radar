"""Stage 7.3 data-health tests: liveness isolation, signals, five routes.

Covers the roadmap 7.3 acceptance criteria for the health surface:
liveness stays independent of data queries, the data-health page is
staff-only, every discoverability signal is computed from fixtures, and
each of the five freshness routes handles its boundary states honestly
(no fabricated zero delays, no green for blocked pipelines).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from django.db import DatabaseError, connection
from django.test import Client
from django.utils import timezone

from accounts.models import User
from audit.models import DataSource
from audit.selectors import FreshnessMeasurement, ingestion_task_snapshots
from companies.models import Company, SecurityListing
from config.data_health import (
    FRESHNESS_ROUTES,
    ROUTE_STATUS_BLOCKED,
    ROUTE_STATUS_FRESH,
    ROUTE_STATUS_NO_DATA,
    ROUTE_STATUS_NOT_CONFIGURED,
    ROUTE_STATUS_STALE,
    TASK_STATUS_NEVER_RUN,
    TASK_STATUS_OK,
    TASK_STATUS_OVERDUE,
    TASK_STATUS_UNMAPPED,
    build_data_health_report,
    route_status,
    task_overdue_status,
)
from earnings.models import EarningsCalendarObservation, InvestorRelationsObservation
from earnings.selectors import (
    measure_earnings_calendar_freshness,
    measure_investor_relations_freshness,
)
from filings.selectors import measure_sec_discovery_freshness
from indexes.selectors import measure_index_announcement_freshness
from notifications.models import Notification, ReminderRule
from notifications.selectors import (
    measure_p0p1_send_freshness,
    notification_backlog_snapshot,
)
from tests.earnings.filing_helpers import make_filing_with_evidence
from tests.earnings.helpers import make_company

pytestmark = pytest.mark.django_db

NOW = timezone.now()
RELATED = Notification.RelatedObjectType.FILING


# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------


def _health_source(source_type: str, suffix: str) -> DataSource:
    return DataSource.objects.create(
        key=f"health-{suffix}-{uuid.uuid4().hex[:8]}",
        name=f"Health {suffix} source",
        source_type=source_type,
        base_url="https://example.test/",
        license_notes="Synthetic test-only source.",
    )


def _calendar_observation(
    *,
    source: DataSource,
    source_observed_at: datetime | None,
) -> EarningsCalendarObservation:
    """Insert one calendar observation; created_at is the insert time.

    Append-only audit rows reject updates, so tests control latency
    through the source-provided timestamp only (created_at ~ test now).
    """

    from tests.earnings.helpers import make_calendar_raw_record

    raw_record = make_calendar_raw_record(source)
    return EarningsCalendarObservation.objects.create(
        source=source,
        raw_data_record=raw_record,
        provider_key=source.provider_adapter or "fixture-provider",
        provider_version="fixture-provider-v1",
        parser_version="fixture-parser-v1",
        provider_event_id=f"evt-{uuid.uuid4().hex[:8]}",
        raw_position=1,
        ticker="FAKE",
        period_end_date=date(2026, 3, 31),
        period_type="Q1",
        fiscal_calendar_type="month_based",
        estimated_release_date=date(2026, 4, 22),
        estimated_release_precision="date_only",
        release_session="after_market",
        source_observed_at=source_observed_at,
    )


def _ir_observation(
    *,
    source: DataSource,
    source_observed_at: datetime | None,
) -> InvestorRelationsObservation:
    from django.utils import timezone as tz

    from audit.models import RawDataObservation, RawDataRecord, SyncRun

    now = tz.now()
    sync_run = SyncRun.objects.create(
        job_type="fixture.ir-health",
        source=source,
        idempotency_key=f"fixture.ir-health:{uuid.uuid4()}",
        started_at=now,
        heartbeat_at=now,
    )
    payload = b'{"fixture":"ir-health"}'
    raw_record = RawDataRecord.objects.create(
        source=source,
        first_sync_run=sync_run,
        source_url=f"https://ir.example.test/{uuid.uuid4().hex[:8]}",
        request_fingerprint=hashlib.sha256(payload).hexdigest(),
        fetched_at=now,
        http_status=200,
        content_type="application/json",
        encoding="utf-8",
        content_hash=hashlib.sha256(uuid.uuid4().bytes + payload).hexdigest(),
        payload=payload,
        payload_size_bytes=len(payload),
    )
    RawDataObservation.objects.create(
        sync_run=sync_run,
        raw_data_record=raw_record,
        observed_at=now,
    )
    return InvestorRelationsObservation.objects.create(
        source=source,
        raw_data_record=raw_record,
        provider_key="fixture-ir",
        provider_version="fixture-ir-v1",
        parser_version="fixture-ir-v1",
        source_event_identity=f"ir-{uuid.uuid4().hex[:8]}",
        raw_position=1,
        company=_company("ir"),
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        item_type="release_confirmation",
        confirmed_release_date=date(2026, 10, 20),
        confirmed_release_precision="date_only",
        source_observed_at=source_observed_at,
    )


def _company(suffix: str) -> Company:
    return make_company(suffix)


def _notification(
    *,
    priority: str = "P1",
    status: str = "pending",
    created_hours_ago: float | None = None,
    sent_after_minutes: float | None = None,
) -> Notification:
    from accounts.models import User as UserModel

    user = UserModel.objects.create_user(
        email=f"health-{uuid.uuid4().hex[:8]}@example.test", password="fixture-only"
    )
    created_at = NOW - timedelta(hours=created_hours_ago) if created_hours_ago else NOW
    sent_at = (
        created_at + timedelta(minutes=sent_after_minutes)
        if sent_after_minutes is not None
        else None
    )
    notification = Notification.objects.create(
        user=user,
        event_type=ReminderRule.EventType.FILING,
        related_object_type=RELATED,
        related_object_id=uuid.uuid4(),
        event_version="v-health",
        priority=priority,
        channel=ReminderRule.Channel.EMAIL,
        status=status,
        sent_at=sent_at,
        idempotency_key=uuid.uuid4().hex,
    )
    Notification.objects.filter(pk=notification.pk).update(created_at=created_at)
    notification.refresh_from_db()
    return notification


# ---------------------------------------------------------------------------
# Liveness endpoint: separation from data queries
# ---------------------------------------------------------------------------


class TestLivenessEndpoint:
    def test_liveness_stays_healthy_when_data_health_report_breaks(
        self, client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*, now: object = None) -> dict[str, object]:
            raise RuntimeError("data-health query exploded")

        monkeypatch.setattr("config.data_health.build_data_health_report", _boom)

        response = client.get("/health/")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok"}

    def test_liveness_reports_503_when_database_is_down(
        self, client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken_cursor() -> object:
            raise DatabaseError("connection refused")

        monkeypatch.setattr(connection, "cursor", _broken_cursor)

        response = client.get("/health/")

        assert response.status_code == 503
        assert response.json() == {"status": "error", "database": "unavailable"}


# ---------------------------------------------------------------------------
# Data-health page: exposure and permissions
# ---------------------------------------------------------------------------


class TestDataHealthPageAccess:
    def test_anonymous_is_redirected_to_login(self, client: Client) -> None:
        response = client.get("/health/data/")

        assert response.status_code == 302
        assert "/admin/login" in response["Location"]

    def test_non_staff_user_is_redirected(self, client: Client) -> None:
        user = User.objects.create_user(email="plain@example.test", password="fixture-only")
        client.force_login(user)

        response = client.get("/health/data/")

        assert response.status_code == 302

    def test_staff_user_sees_all_sections(self, client: Client) -> None:
        staff = User.objects.create_superuser(
            email="health-admin@example.test", password="fixture-password-only"
        )
        client.force_login(staff)

        response = client.get("/health/data/")

        assert response.status_code == 200
        body = response.content.decode()
        for route in FRESHNESS_ROUTES:
            assert route.label in body
        assert "Sync tasks" in body
        assert "Notification backlog" in body
        assert "All times are UTC" in body


# ---------------------------------------------------------------------------
# Freshness routes
# ---------------------------------------------------------------------------


class TestSecDiscoveryFreshness:
    def test_measures_within_over_and_anomaly(self) -> None:
        company = _company("sec-health")
        # created_at is the insert time (~NOW); latency is controlled
        # through accepted_at alone.
        make_filing_with_evidence(
            company=company, suffix="sec-fast", accepted_at=NOW - timedelta(minutes=5)
        )
        make_filing_with_evidence(
            company=company, suffix="sec-slow", accepted_at=NOW - timedelta(minutes=45)
        )
        make_filing_with_evidence(
            company=company, suffix="sec-anomaly", accepted_at=NOW + timedelta(minutes=10)
        )

        measurement = measure_sec_discovery_freshness(now=NOW + timedelta(minutes=1))

        assert measurement.measured_count == 3
        assert measurement.within_target_count == 1
        assert measurement.over_target_count == 1
        assert measurement.anomaly_count == 1
        assert measurement.unmeasured_count == 0
        assert measurement.max_latency_seconds is not None
        assert measurement.max_latency_seconds > 40 * 60


class TestEarningsCalendarFreshness:
    def test_measured_unmeasured_and_over_target(self) -> None:
        source = _health_source(DataSource.SourceType.EARNINGS_CALENDAR, "cal")
        _calendar_observation(source=source, source_observed_at=timezone.now())
        # Observed 30h before insert: over the 24h target.
        _calendar_observation(
            source=source, source_observed_at=timezone.now() - timedelta(hours=30)
        )
        _calendar_observation(source=source, source_observed_at=None)

        measurement = measure_earnings_calendar_freshness(now=timezone.now())

        assert measurement.measured_count == 2
        assert measurement.within_target_count == 1
        assert measurement.over_target_count == 1
        assert measurement.unmeasured_count == 1
        assert measurement.latest_sample_at is not None


class TestInvestorRelationsFreshness:
    def test_measured_unmeasured_and_over_target(self) -> None:
        source = _health_source(DataSource.SourceType.INVESTOR_RELATIONS, "ir")
        _ir_observation(source=source, source_observed_at=timezone.now())
        # Observed 8h before insert: over the 6h target.
        _ir_observation(source=source, source_observed_at=timezone.now() - timedelta(hours=8))
        _ir_observation(source=source, source_observed_at=None)

        measurement = measure_investor_relations_freshness(now=timezone.now())

        assert measurement.measured_count == 2
        assert measurement.within_target_count == 1
        assert measurement.over_target_count == 1
        assert measurement.unmeasured_count == 1


class TestIndexAnnouncementFreshness:
    def test_measures_within_and_over_with_date_precision(self) -> None:
        from indexes.models import MarketIndex
        from indexes.services import record_index_change_leg

        company = _company("idx-health")
        listing = _listing_for(company)
        sp500, _created = MarketIndex.objects.get_or_create(
            code="SP500",
            defaults={"name": "S&P 500", "index_group": "LARGE", "is_enabled": True},
        )
        today = timezone.now().date()
        # Announced today: midnight-UTC start keeps latency under 24h.
        record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=today,
            announcement_date=today,
        )
        # Announced the day before creation: date-precision latency >= 24h.
        day_old_company = _company("idx-health-old")
        day_old_listing = _listing_for(day_old_company)
        record_index_change_leg(
            company=day_old_company,
            security_listing=day_old_listing,
            index=sp500,
            action="added",
            effective_date=today,
            announcement_date=today - timedelta(days=1),
        )

        measurement = measure_index_announcement_freshness(now=timezone.now())

        assert measurement.measured_count == 2
        assert measurement.within_target_count == 1
        assert measurement.over_target_count == 1
        assert measurement.max_latency_seconds is not None
        assert measurement.max_latency_seconds >= 24 * 60 * 60

    def test_event_without_leg_announcement_is_unmeasured(self) -> None:
        from indexes.models import MarketIndex
        from indexes.services import record_index_change_leg

        company = _company("idx-health-unannounced")
        listing = _listing_for(company)
        sp500, _created = MarketIndex.objects.get_or_create(
            code="SP500",
            defaults={"name": "S&P 500", "index_group": "LARGE", "is_enabled": True},
        )
        record_index_change_leg(
            company=company,
            security_listing=listing,
            index=sp500,
            action="added",
            effective_date=timezone.now().date(),
        )

        measurement = measure_index_announcement_freshness(now=timezone.now())

        assert measurement.measured_count == 0
        assert measurement.unmeasured_count == 1


def _listing_for(company: Company) -> SecurityListing:

    return SecurityListing.objects.create(
        company=company,
        ticker=f"HE{company.pk.hex[:4].upper()}",
        exchange="NYSE",
        security_name=f"{company.display_name} Common",
        security_type="common_stock",
        effective_from=date(2026, 1, 1),
    )


class TestP0P1SendFreshness:
    def test_sent_within_and_over_target_and_inflight_unmeasured(self) -> None:
        _notification(status="sent", sent_after_minutes=2.0)
        _notification(status="sent", sent_after_minutes=10.0)
        _notification(status="pending")
        _notification(status="processing")

        measurement = measure_p0p1_send_freshness(now=NOW + timedelta(minutes=15))

        assert measurement.measured_count == 2
        assert measurement.within_target_count == 1
        assert measurement.over_target_count == 1
        assert measurement.unmeasured_count == 2
        assert measurement.max_latency_seconds is not None
        assert measurement.max_latency_seconds >= 10 * 60


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------


class TestRouteStatus:
    def test_blocked_gate_wins_over_everything(self) -> None:
        ir_route = next(r for r in FRESHNESS_ROUTES if r.key == "investor_relations")
        _health_source(DataSource.SourceType.INVESTOR_RELATIONS, "blocked")

        assert route_status(ir_route, _measurement(measured=3, within=3)) == ROUTE_STATUS_BLOCKED

    def test_not_configured_when_no_enabled_source(self) -> None:
        calendar_route = next(r for r in FRESHNESS_ROUTES if r.key == "earnings_calendar")

        assert route_status(calendar_route, _measurement(measured=0)) == ROUTE_STATUS_NOT_CONFIGURED

    def test_no_data_with_configured_source_and_no_samples(self) -> None:
        calendar_route = next(r for r in FRESHNESS_ROUTES if r.key == "earnings_calendar")
        _health_source(DataSource.SourceType.EARNINGS_CALENDAR, "empty")

        assert route_status(calendar_route, _measurement()) == ROUTE_STATUS_NO_DATA

    def test_stale_when_any_sample_misses_target(self) -> None:
        calendar_route = next(r for r in FRESHNESS_ROUTES if r.key == "earnings_calendar")
        _health_source(DataSource.SourceType.EARNINGS_CALENDAR, "stale")

        assert (
            route_status(calendar_route, _measurement(measured=2, within=1, over=1))
            == ROUTE_STATUS_STALE
        )

    def test_fresh_when_all_samples_meet_target(self) -> None:
        calendar_route = next(r for r in FRESHNESS_ROUTES if r.key == "earnings_calendar")
        _health_source(DataSource.SourceType.EARNINGS_CALENDAR, "fresh")

        assert (
            route_status(calendar_route, _measurement(measured=2, within=2)) == ROUTE_STATUS_FRESH
        )


def _measurement(*, measured: int = 0, within: int = 0, over: int = 0) -> FreshnessMeasurement:
    return FreshnessMeasurement(
        measured_count=measured,
        within_target_count=within,
        over_target_count=over,
        unmeasured_count=0,
        anomaly_count=0,
        max_latency_seconds=None,
        latest_sample_at=None,
    )


class TestTaskOverdueStatus:
    NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

    def test_unmapped_job_type_is_never_judged(self) -> None:
        assert (
            task_overdue_status(job_type="fixture.unknown", last_run_started_at=None, now=self.NOW)
            == TASK_STATUS_UNMAPPED
        )

    def test_never_run(self) -> None:
        assert (
            task_overdue_status(
                job_type="filings.sec_edgar", last_run_started_at=None, now=self.NOW
            )
            == TASK_STATUS_NEVER_RUN
        )

    def test_ok_within_two_expected_intervals(self) -> None:
        assert (
            task_overdue_status(
                job_type="filings.sec_edgar",
                last_run_started_at=self.NOW - timedelta(minutes=19),
                now=self.NOW,
            )
            == TASK_STATUS_OK
        )

    def test_overdue_beyond_two_expected_intervals(self) -> None:
        assert (
            task_overdue_status(
                job_type="filings.sec_edgar",
                last_run_started_at=self.NOW - timedelta(minutes=21),
                now=self.NOW,
            )
            == TASK_STATUS_OVERDUE
        )


class TestIngestionTaskSnapshots:
    def test_reports_last_run_running_and_recent_failures(self) -> None:
        from audit.models import SyncRun

        source = _health_source(DataSource.SourceType.SEC, "tasks")
        past = NOW - timedelta(hours=2)
        base = SyncRun.objects.create(
            job_type="filings.sec_edgar",
            source=source,
            idempotency_key="health-sec-1",
            status=SyncRun.Status.FAILED,
            started_at=past,
            finished_at=past + timedelta(minutes=1),
            heartbeat_at=past,
            error_summary="boom",
        )
        SyncRun.objects.create(
            job_type="filings.sec_edgar",
            source=source,
            idempotency_key="health-sec-2",
            status=SyncRun.Status.RUNNING,
            started_at=NOW - timedelta(minutes=1),
            heartbeat_at=NOW,
        )
        SyncRun.objects.create(
            job_type="filings.sec_edgar",
            source=source,
            idempotency_key="health-sec-replay",
            status=SyncRun.Status.SUCCEEDED,
            run_mode=SyncRun.RunMode.REPLAY,
            scope={"window_kind": "replay"},
            replay_source_sync_run=base,
            replay_contract_version="fixture-replay-v1",
            replay_input_digest=hashlib.sha256(b"replay-input").hexdigest(),
            parser_version="fixture-parser-v1",
            started_at=past - timedelta(hours=1),
            finished_at=past,
            heartbeat_at=past,
        )

        snapshots = {s.job_type: s for s in ingestion_task_snapshots()}
        snapshot = snapshots["filings.sec_edgar"]

        assert snapshot.last_run_status == "running"
        assert snapshot.running_count == 1
        assert snapshot.failed_partial_count_24h == 1
        assert snapshot.last_run_started_at is not None
        assert snapshot.last_run_started_at > past


# ---------------------------------------------------------------------------
# Notification backlog
# ---------------------------------------------------------------------------


class TestNotificationBacklog:
    def test_counts_pending_processing_residue_and_failed(self) -> None:
        _notification(priority="P0", status="pending", created_hours_ago=2.0)
        _notification(priority="P1", status="pending", created_hours_ago=1.0)
        _notification(priority="P2", status="pending", created_hours_ago=0.5)
        _notification(priority="P1", status="processing", created_hours_ago=5.0)
        _notification(priority="P1", status="failed", created_hours_ago=3.0)
        _notification(priority="P3", status="failed", created_hours_ago=1.0)

        snapshot = notification_backlog_snapshot(now=NOW + timedelta(minutes=1))

        assert snapshot.pending_total == 3
        assert snapshot.pending_p0p1 == 2
        assert snapshot.oldest_pending_p0p1_seconds is not None
        assert snapshot.oldest_pending_p0p1_seconds > 59 * 60
        assert snapshot.processing_total == 1  # ADR-027 residue visibility
        assert snapshot.oldest_processing_seconds is not None
        assert snapshot.oldest_processing_seconds > 4 * 60 * 60
        assert snapshot.failed_total == 2
        assert snapshot.failed_p0p1 == 1


# ---------------------------------------------------------------------------
# Full report assembly
# ---------------------------------------------------------------------------


class TestBuildDataHealthReport:
    def test_report_has_five_routes_with_normalized_status(self) -> None:
        _health_source(DataSource.SourceType.SEC, "report-sec")

        report = build_data_health_report(now=NOW)

        assert {route["key"] for route in report["routes"]} == {
            "sec_filing_discovery",
            "earnings_calendar",
            "investor_relations",
            "index_announcements",
            "p0p1_notifications",
        }
        by_key = {route["key"]: route for route in report["routes"]}
        assert by_key["sec_filing_discovery"]["status"] == ROUTE_STATUS_NO_DATA
        assert by_key["earnings_calendar"]["status"] == ROUTE_STATUS_NOT_CONFIGURED
        assert by_key["investor_relations"]["status"] == ROUTE_STATUS_BLOCKED
        assert by_key["index_announcements"]["status"] == ROUTE_STATUS_BLOCKED
        assert by_key["p0p1_notifications"]["status"] == ROUTE_STATUS_NO_DATA
