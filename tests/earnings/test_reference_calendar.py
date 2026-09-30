from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from accounts.models import User
from audit.models import (
    DataSource,
    RawDataObservation,
    RawDataParseAttempt,
    RawDataRecord,
    SourceEvidence,
    SyncRun,
)
from companies.models import Company, SecurityListing
from companies.services import transition_security_listing
from earnings.models import (
    EarningsCalendarObservation,
    EarningsEvent,
    EarningsReconciliationDecision,
)
from earnings.reference_calendar_parser import ReferenceParseStatus, parse_reference_calendar
from earnings.services.reference_calendar_projection import (
    latest_reference_calendar,
    project_reference_calendar,
)
from earnings.services.reference_calendar_sync import execute_reference_calendar_sync
from indexes.models import IndexMembership, MarketIndex
from indexes.services import end_membership
from providers.alpha_vantage_reference import AlphaVantageReferenceProvider
from providers.http import TransportRequest, TransportResponse

AS_OF = date(2026, 9, 30)
HEADER = b"symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\n"


class CsvTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls = 0

    def send(self, request: TransportRequest) -> TransportResponse:
        self.calls += 1
        assert "apikey" not in request.url
        return TransportResponse(
            status_code=200,
            headers={"Content-Type": "text/csv"},
            body=self.body,
            fetched_at=datetime.now(UTC),
        )


def test_reference_csv_parser_keeps_only_supported_reference_facts() -> None:
    payload = HEADER + (
        b"AAA,Alpha,2026-10-01,2026-09-30,1,USD,pre-market\n"
        b"BBB,Beta,2026-10-02,2026-09-30,1,USD,AmC\n"
        b"CCC,Gamma,not-a-date,2026-09-30,1,USD,bmo\n"
    )
    result = parse_reference_calendar(payload)
    assert result == parse_reference_calendar(payload)
    assert result.status == ReferenceParseStatus.PARTIAL
    assert [row.raw_position for row in result.rows] == [1, 2]
    assert [row.session for row in result.rows] == ["pre_market", "post_market"]
    assert result.invalid_row_count == 1
    assert not hasattr(result.rows[0], "period_type")
    assert not hasattr(result.rows[0], "provider_event_id")
    assert (
        parse_reference_calendar(HEADER + b" AAA ,Alpha,2026-10-01,,,,  BMO  \n").rows[0].session
        == "pre_market"
    )
    assert (
        parse_reference_calendar(HEADER + b"AAA,Alpha,2026-10-01,,,,unknown\n").rows[0].session
        == "unknown"
    )
    assert (
        parse_reference_calendar(HEADER + b",Alpha,2026-10-01,,,,amc\n").status
        == ReferenceParseStatus.PARTIAL
    )


def test_reference_csv_empty_and_structure_error() -> None:
    assert parse_reference_calendar(HEADER).status == ReferenceParseStatus.EMPTY
    assert parse_reference_calendar(b"symbol,reportDate\nAAA,2026-10-01\n").status == (
        ReferenceParseStatus.PAGE_ERROR
    )
    assert parse_reference_calendar(HEADER + b"AAA,too,few\n").rows == ()


@pytest.mark.django_db(transaction=True)
def test_reference_page_error_retains_raw_and_audit_failure() -> None:
    source, _company = _pool()
    provider = AlphaVantageReferenceProvider(
        api_key="fixture-key", transport=CsvTransport(b"not,a,calendar\n")
    )
    result = execute_reference_calendar_sync(
        source=source, as_of=AS_OF, enabled_index_codes=("SP500",), provider=provider
    )
    assert result.sync_run.status == SyncRun.Status.FAILED
    assert result.parse_status == "PAGE_ERROR"
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1
    assert RawDataParseAttempt.objects.get().status == RawDataParseAttempt.Status.DATA_ERROR
    assert EarningsCalendarObservation.objects.count() == 0
    assert EarningsEvent.objects.count() == 0
    assert SourceEvidence.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_horizon_above_90_rejected_before_provider_call() -> None:
    source, _company = _pool()
    transport = CsvTransport(HEADER)
    provider = AlphaVantageReferenceProvider(api_key="fixture-key", transport=transport)
    with pytest.raises(ValueError):
        execute_reference_calendar_sync(
            source=source,
            as_of=AS_OF,
            enabled_index_codes=("SP500",),
            provider=provider,
            forward_horizon_days=91,
        )
    assert transport.calls == 0
    assert SyncRun.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_empty_response_is_success_with_unknown_coverage() -> None:
    source, _company = _pool()
    provider = AlphaVantageReferenceProvider(api_key="fixture-key", transport=CsvTransport(HEADER))
    run = execute_reference_calendar_sync(
        source=source, as_of=AS_OF, enabled_index_codes=("SP500",), provider=provider
    )
    projection = project_reference_calendar(run.sync_run.pk)
    assert run.sync_run.status == SyncRun.Status.SUCCEEDED
    assert projection.status == "EMPTY"
    assert projection.rows == ()
    assert projection.observed_report_date_min is None
    assert projection.observed_report_date_max is None
    assert projection.provider_coverage_end is None


def _pool() -> tuple[DataSource, Company]:
    source = DataSource.objects.create(
        key="alpha-vantage-free",
        name="Alpha Vantage Free personal reference",
        source_type=DataSource.SourceType.EARNINGS_CALENDAR,
        provider_adapter=AlphaVantageReferenceProvider.provider_key,
        base_url="https://www.alphavantage.co",
    )
    company = Company.objects.create(legal_name="Alpha Inc", display_name="Alpha")
    listing = SecurityListing.objects.create(
        company=company,
        ticker="AAA",
        exchange="NYSE",
        security_type="common_stock",
        effective_from=date(2026, 1, 1),
    )
    index, _created = MarketIndex.objects.get_or_create(
        code="SP500",
        defaults={"name": "S&P 500", "index_group": "LARGE", "is_enabled": True},
    )
    IndexMembership.objects.create(
        index=index,
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )
    return source, company


@pytest.mark.django_db(transaction=True)
def test_reference_sync_replay_is_read_only_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, company = _pool()
    transport = CsvTransport(
        HEADER
        + b"AAA,Alpha,2026-10-01,2026-09-30,1,USD,post-market\n"
        + b"ZZZ,Outside,2026-10-02,2026-09-30,1,USD,bmo\n"
        + b"AAA,Alpha,2027-01-01,2026-09-30,1,USD,bmo\n"
    )
    provider = AlphaVantageReferenceProvider(api_key="fixture-key", transport=transport)
    first = execute_reference_calendar_sync(
        source=source, as_of=AS_OF, enabled_index_codes=("SP500",), provider=provider
    )
    second = execute_reference_calendar_sync(
        source=source, as_of=AS_OF, enabled_index_codes=("SP500",), provider=provider
    )
    assert first.created is True and second.created is False
    assert transport.calls == 1
    assert first.sync_run.status == SyncRun.Status.SUCCEEDED
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1
    assert RawDataParseAttempt.objects.get().status == RawDataParseAttempt.Status.SUCCEEDED
    assert "fixture-key" not in str(first.sync_run.scope)
    assert "fixture-key" not in RawDataRecord.objects.get().source_url
    assert "fixture-key" not in str(first.sync_run.error_summary)
    before = (
        SyncRun.objects.count(),
        RawDataObservation.objects.count(),
        RawDataParseAttempt.objects.count(),
        SourceEvidence.objects.count(),
    )
    now = datetime.now(UTC)
    monkeypatch.setattr(
        "earnings.services.monitoring_pool.select_monitoring_pool",
        lambda **_kwargs: pytest.fail("Replay invoked the current monitoring-pool selector."),
    )
    projection = project_reference_calendar(first.sync_run.pk, now=now)
    repeated = project_reference_calendar(first.sync_run.pk, now=now + timedelta(days=3))
    assert projection.status == "COMPLETE"
    assert projection.requested_window_end == date(2026, 12, 28)
    assert projection.provider_coverage_end is None
    assert projection.observed_report_date_max == date(2027, 1, 1)
    assert len(projection.rows) == 1
    assert projection.rows[0].company_id == company.pk
    assert projection.rows[0].session == "post_market"
    assert projection.diagnostics.out_of_pool == 1
    assert projection.rows[0].row_key == repeated.rows[0].row_key
    assert projection.freshness == "fresh"
    assert repeated.freshness == "stale"
    assert before == (
        SyncRun.objects.count(),
        RawDataObservation.objects.count(),
        RawDataParseAttempt.objects.count(),
        SourceEvidence.objects.count(),
    )
    assert transport.calls == 1
    assert EarningsCalendarObservation.objects.count() == 0
    assert EarningsReconciliationDecision.objects.count() == 0
    assert EarningsEvent.objects.count() == 0
    assert SourceEvidence.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_partial_reference_run_falls_back_to_latest_complete() -> None:
    source, _company = _pool()
    provider = AlphaVantageReferenceProvider(
        api_key="fixture-key",
        transport=CsvTransport(HEADER + b"AAA,Alpha,2026-10-01,,,,pre-market\n"),
    )
    good = execute_reference_calendar_sync(
        source=source, as_of=AS_OF, enabled_index_codes=("SP500",), provider=provider
    )
    partial_provider = AlphaVantageReferenceProvider(
        api_key="fixture-key",
        transport=CsvTransport(
            HEADER + b"AAA,Alpha,2026-10-01,,,,pre-market\nBAD,Bad,invalid,,,,amc\n"
        ),
    )
    partial = execute_reference_calendar_sync(
        source=source,
        as_of=AS_OF,
        enabled_index_codes=("SP500",),
        provider=partial_provider,
        request_id="retry-1",
    )
    assert partial.sync_run.status == SyncRun.Status.PARTIAL
    assert project_reference_calendar(partial.sync_run.pk).rows == ()
    view = latest_reference_calendar(source_id=source.pk)
    assert view.update_state == "update_incomplete"
    assert view.projection is not None
    assert view.projection.sync_run_id == good.sync_run.pk
    assert len(view.projection.rows) == 1
    assert EarningsEvent.objects.count() == 0
    assert SourceEvidence.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_cross_company_ticker_ambiguity_is_excluded() -> None:
    source, _company = _pool()
    other = Company.objects.create(legal_name="Other Inc", display_name="Other")
    listing = SecurityListing.objects.create(
        company=other,
        ticker="AAA",
        exchange="NASDAQ",
        security_type="common_stock",
        effective_from=date(2026, 1, 1),
    )
    index = MarketIndex.objects.get(code="SP500")
    IndexMembership.objects.create(
        index=index,
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )
    provider = AlphaVantageReferenceProvider(
        api_key="fixture-key",
        transport=CsvTransport(HEADER + b"AAA,Alpha,2026-10-01,,,,bmo\n"),
    )
    run = execute_reference_calendar_sync(
        source=source, as_of=AS_OF, enabled_index_codes=("SP500",), provider=provider
    )
    projection = project_reference_calendar(run.sync_run.pk)
    assert projection.rows == ()
    assert projection.diagnostics.ambiguous == 1


@pytest.mark.django_db(transaction=True)
def test_reference_matching_dedupes_one_company_and_keeps_inclusive_window() -> None:
    source, company = _pool()
    second_listing = SecurityListing.objects.create(
        company=company,
        ticker="AAA",
        exchange="NASDAQ",
        security_type="common_stock",
        effective_from=date(2026, 1, 1),
    )
    IndexMembership.objects.create(
        index=MarketIndex.objects.get(code="SP500"),
        security_listing=second_listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )
    transport = CsvTransport(
        HEADER
        + b"AAA,Alpha,2026-09-30,,,,bmo\n"
        + b"AAA,Alpha,2026-09-30,,,,bmo\n"
        + b"AAA,Alpha,2026-12-28,,,,amc\n"
        + b"AAA.X,Alias,2026-10-01,,,,bmo\n"
        + b"AAA,Alpha,2026-12-29,,,,amc\n"
    )
    run = execute_reference_calendar_sync(
        source=source,
        as_of=AS_OF,
        enabled_index_codes=("SP500",),
        provider=AlphaVantageReferenceProvider(api_key="fixture-key", transport=transport),
    )
    projection = project_reference_calendar(run.sync_run.pk)
    assert [row.estimated_report_date for row in projection.rows] == [AS_OF, date(2026, 12, 28)]
    assert all(row.company_id == company.pk for row in projection.rows)
    assert projection.diagnostics.duplicate == 1
    assert projection.diagnostics.out_of_pool == 1
    assert projection.observed_report_date_max == date(2026, 12, 29)
    assert projection.provider_coverage_end is None


@pytest.mark.django_db(transaction=True)
def test_no_prior_complete_projection_is_unavailable() -> None:
    source, _company = _pool()
    provider = AlphaVantageReferenceProvider(
        api_key="fixture-key", transport=CsvTransport(b"not,a,calendar\n")
    )
    execute_reference_calendar_sync(
        source=source, as_of=AS_OF, enabled_index_codes=("SP500",), provider=provider
    )
    view = latest_reference_calendar(source_id=source.pk)
    assert view.projection is None
    assert view.update_state == "unavailable"


@pytest.mark.django_db(transaction=True)
def test_frozen_reference_replay_survives_backdated_listing_transition() -> None:
    source, _company = _pool()
    transport = CsvTransport(HEADER + b"AAA,Alpha,2026-10-01,,,,bmo\n")
    run = execute_reference_calendar_sync(
        source=source,
        as_of=AS_OF,
        enabled_index_codes=("SP500",),
        provider=AlphaVantageReferenceProvider(api_key="fixture-key", transport=transport),
    )
    now = datetime.now(UTC)
    before = project_reference_calendar(run.sync_run.pk, now=now)
    listing = SecurityListing.objects.get(ticker="AAA")
    actor = User.objects.create_user(
        email="listing-transition@example.com", password="fixture-password-only", is_staff=True
    )
    end_membership(
        membership=IndexMembership.objects.get(security_listing=listing),
        effective_to=date(2026, 9, 1),
        actor_user=actor,
        reason="Backdated membership correction",
        request_id="backdated-membership-fixture",
    )
    assert project_reference_calendar(run.sync_run.pk, now=now) == before
    transition_security_listing(
        listing=listing,
        transition_date=date(2026, 9, 1),
        ticker="BBB",
        exchange="NYSE",
        actor_user=actor,
        reason="Backdated listing identity correction",
        request_id="backdated-transition-fixture",
    )
    listing.refresh_from_db()
    assert listing.effective_to == date(2026, 9, 1)

    after = project_reference_calendar(run.sync_run.pk, now=now)
    assert after == before
    assert len(after.rows) == 1
    assert transport.calls == 1


@pytest.mark.django_db(transaction=True)
def test_failed_update_falls_back_and_system_error_keeps_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _company = _pool()
    provider = AlphaVantageReferenceProvider(
        api_key="fixture-key", transport=CsvTransport(HEADER + b"AAA,Alpha,2026-10-01,,,,bmo\n")
    )
    good = execute_reference_calendar_sync(
        source=source, as_of=AS_OF, enabled_index_codes=("SP500",), provider=provider
    )

    def broken_parser(_payload: bytes) -> None:
        raise RuntimeError("parser broke")

    monkeypatch.setattr(
        "earnings.services.reference_calendar_sync.parse_reference_calendar", broken_parser
    )
    with pytest.raises(RuntimeError, match="parser broke"):
        execute_reference_calendar_sync(
            source=source,
            as_of=AS_OF,
            enabled_index_codes=("SP500",),
            provider=provider,
            request_id="retry-system-error",
        )
    failed = SyncRun.objects.exclude(pk=good.sync_run.pk).get()
    assert failed.status == SyncRun.Status.FAILED
    assert RawDataObservation.objects.filter(sync_run=failed).count() == 1
    assert (
        RawDataParseAttempt.objects.filter(
            observation__sync_run=failed,
            status=RawDataParseAttempt.Status.SYSTEM_ERROR,
        ).count()
        == 1
    )
    view = latest_reference_calendar(source_id=source.pk)
    assert view.update_state == "latest_failed"
    assert view.projection is not None and view.projection.sync_run_id == good.sync_run.pk
