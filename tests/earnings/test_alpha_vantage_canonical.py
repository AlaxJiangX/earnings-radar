from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from audit.models import (
    DataSource,
    RawDataParseAttempt,
    RawDataRecord,
    SyncRun,
)
from companies.models import Company, SecurityListing
from earnings.alpha_vantage_canonical_parser import (
    ALPHA_VANTAGE_CANONICAL_PARSER_VERSION,
    AlphaVantageCanonicalParseStatus,
    parse_alpha_vantage_canonical_calendar,
)
from earnings.identity import derive_earnings_identity_key
from earnings.models import (
    EarningsCalendarObservation,
    EarningsEvent,
    EarningsReconciliationDecision,
    MonitoringPoolSnapshot,
)
from earnings.services import (
    ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2,
    ALPHA_VANTAGE_FORWARD_CAPABILITY,
    ALPHA_VANTAGE_PAST_CORRECTION_CAPABILITY,
    ALPHA_VANTAGE_WINDOW_CAPABILITY_VERSION,
    EARNINGS_SOURCE_EVENT_IDENTITY_VERSION_V2,
    AlphaVantageCanonicalReplayIntegrityError,
    AlphaVantageCanonicalSyncResult,
    derive_alpha_vantage_candidate_family_key,
    derive_alpha_vantage_candidate_uuid,
    derive_alpha_vantage_source_identity_v2,
    execute_alpha_vantage_canonical_sync,
    nominal_window_end,
    verify_alpha_vantage_canonical_replay,
)
from earnings.services.monitoring_pool import (
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
    select_monitoring_pool,
)
from indexes.models import IndexMembership, MarketIndex
from providers.alpha_vantage_canonical import (
    ALPHA_VANTAGE_CANONICAL_PROVIDER_VERSION,
    AlphaVantageCanonicalProvider,
)
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


def _source() -> DataSource:
    token = uuid.uuid4().hex[:8]
    return DataSource.objects.create(
        key=f"alpha-vantage-free-{token}",
        name="Alpha Vantage Free personal canonical",
        source_type=DataSource.SourceType.EARNINGS_CALENDAR,
        provider_adapter=AlphaVantageCanonicalProvider.provider_key,
        base_url="https://www.alphavantage.co",
        license_notes="Personal/private/single-user/non-commercial test source.",
    )


def _company_with_listing(
    *,
    ticker: str,
    exchange: str = "NASDAQ",
    effective_from: date = date(2026, 1, 1),
) -> tuple[Company, SecurityListing]:
    token = uuid.uuid4().hex[:6]
    company = Company.objects.create(
        legal_name=f"Legal {token}",
        display_name=f"Company {token}",
    )
    listing = SecurityListing.objects.create(
        company=company,
        ticker=ticker,
        exchange=exchange,
        security_name=f"{company.display_name} {ticker}",
        security_type="common_stock",
        effective_from=effective_from,
    )
    return company, listing


def _membership(
    listing: SecurityListing,
    *,
    effective_from: date = date(2026, 1, 1),
) -> None:
    index, _created = MarketIndex.objects.get_or_create(
        code="SP500",
        defaults={
            "name": "S&P 500",
            "index_group": MarketIndex.IndexGroup.LARGE,
            "is_enabled": True,
        },
    )
    IndexMembership.objects.create(
        index=index,
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=effective_from,
    )


def _pool(*, tickers: tuple[str, ...] = ("AAA",)) -> tuple[DataSource, dict[str, Company]]:
    source = _source()
    companies: dict[str, Company] = {}
    for ticker in tickers:
        company, listing = _company_with_listing(ticker=ticker)
        companies[ticker] = company
        _membership(listing)
    return source, companies


def _snapshot() -> MonitoringPoolSnapshot:
    return select_monitoring_pool(
        as_of=AS_OF,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        enabled_index_codes=("SP500",),
    ).snapshot


def _provider(body: bytes) -> AlphaVantageCanonicalProvider:
    return AlphaVantageCanonicalProvider(api_key="fixture-key", transport=CsvTransport(body))


def _execute(
    *,
    source: DataSource,
    snapshot: MonitoringPoolSnapshot,
    provider: AlphaVantageCanonicalProvider,
    request_id: str | None = None,
    run_date: date = AS_OF,
) -> AlphaVantageCanonicalSyncResult:
    return execute_alpha_vantage_canonical_sync(
        source=source,
        run_date=run_date,
        monitoring_pool_as_of=snapshot.as_of_date,
        monitoring_pool_hash=snapshot.pool_hash,
        selector_version=snapshot.selector_version,
        provider=provider,
        request_id=request_id,
    )


def test_alpha_vantage_canonical_parser_keeps_frozen_facts() -> None:
    payload = HEADER + (
        b"AAA,Alpha,2026-10-01,2026-09-30,1.23,USD,post-market\n"
        b"BAD,Bad,not-a-date,2026-09-30,1,USD,pre-market\n"
        b"CCC,Gamma,2026-10-02,2026-06-30,2,USD,AmC\n"
    )
    result = parse_alpha_vantage_canonical_calendar(payload)
    assert result == parse_alpha_vantage_canonical_calendar(payload)
    assert result.status == AlphaVantageCanonicalParseStatus.PARTIAL
    assert result.invalid_row_count == 1
    assert [row.raw_position for row in result.rows] == [1, 3]
    assert result.rows[0].period_end_date == date(2026, 9, 30)
    assert result.rows[0].release_session == "after_market"
    assert result.rows[1].release_session == "after_market"
    assert result.rows[0].parser_version == ALPHA_VANTAGE_CANONICAL_PARSER_VERSION
    assert parse_alpha_vantage_canonical_calendar(HEADER).status == (
        AlphaVantageCanonicalParseStatus.EMPTY
    )
    invalid_header = parse_alpha_vantage_canonical_calendar(b"symbol,reportDate\nAAA,2026-10-01\n")
    assert invalid_header.status == AlphaVantageCanonicalParseStatus.PAGE_ERROR


def test_v2_identity_excludes_mutable_scheduling_facts() -> None:
    company_id = uuid.uuid4()
    identity = derive_alpha_vantage_source_identity_v2(
        source_key="alpha-vantage-free",
        provider_key="alpha-vantage-free",
        company_id=company_id,
        period_end_date=date(2026, 9, 30),
    )
    assert identity.startswith("internal:v2:")
    assert identity == derive_alpha_vantage_source_identity_v2(
        source_key="alpha-vantage-free",
        provider_key="alpha-vantage-free",
        company_id=company_id,
        period_end_date=date(2026, 9, 30),
    )
    assert identity != derive_alpha_vantage_source_identity_v2(
        source_key="alpha-vantage-free",
        provider_key="alpha-vantage-free",
        company_id=uuid.uuid4(),
        period_end_date=date(2026, 9, 30),
    )
    assert identity != derive_alpha_vantage_source_identity_v2(
        source_key="alpha-vantage-free",
        provider_key="alpha-vantage-free",
        company_id=company_id,
        period_end_date=date(2026, 6, 30),
    )
    family_key = derive_alpha_vantage_candidate_family_key(
        source_identity=identity,
        source_key="alpha-vantage-free",
        provider_key="alpha-vantage-free",
        company_id=company_id,
        period_end_date=date(2026, 9, 30),
    )
    assert derive_alpha_vantage_candidate_uuid(family_key) == (
        derive_alpha_vantage_candidate_uuid(family_key)
    )
    assert ALPHA_VANTAGE_FORWARD_CAPABILITY == "nominal_3month"
    assert ALPHA_VANTAGE_PAST_CORRECTION_CAPABILITY == "unsupported"
    assert ALPHA_VANTAGE_WINDOW_CAPABILITY_VERSION == "alpha-vantage-window-capability-v1"
    assert EARNINGS_SOURCE_EVENT_IDENTITY_VERSION_V2 == "earnings-source-event-identity-v2"
    assert nominal_window_end(date(2026, 9, 30)) == date(2026, 12, 29)
    assert nominal_window_end(date(2026, 1, 31)) == date(2026, 4, 29)


@pytest.mark.django_db(transaction=True)
def test_canonical_sync_creates_incomplete_candidate_without_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, companies = _pool()
    snapshot = _snapshot()
    transport = CsvTransport(HEADER + b"AAA,Alpha,2026-10-01,2026-09-30,1.23,USD,post-market\n")
    provider = AlphaVantageCanonicalProvider(api_key="fixture-key", transport=transport)

    def _fail_promotion(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Alpha Vantage canonical sync called promotion.")

    monkeypatch.setattr("earnings.services.promotion.promote_earnings_event", _fail_promotion)
    monkeypatch.setattr(
        "earnings.services.reconciliation_workflow.promote_earnings_event",
        _fail_promotion,
    )

    result = _execute(source=source, snapshot=snapshot, provider=provider)

    run = SyncRun.objects.get(pk=result.sync_run.pk)
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.provider_version == ALPHA_VANTAGE_CANONICAL_PROVIDER_VERSION
    assert run.parser_version == ALPHA_VANTAGE_CANONICAL_PARSER_VERSION
    assert run.scope["window_start"] == "2026-09-30"
    assert run.scope["window_end"] == "2026-12-29"
    assert set(run.scope) == {
        "capability",
        "provider_key",
        "window_kind",
        "window_start",
        "window_end",
        "monitoring_pool_as_of",
        "monitoring_pool_hash",
        "selector_version",
    }
    assert "fixture-key" not in str(run.scope)
    assert "fixture-key" not in RawDataRecord.objects.get().source_url

    observation = EarningsCalendarObservation.objects.get()
    assert observation.period_type is None
    assert observation.provider_event_id.startswith("internal:v2:")
    assert observation.provider_symbol == "AAA"
    assert observation.period_end_date == date(2026, 9, 30)
    assert observation.estimated_release_date == date(2026, 10, 1)
    assert observation.release_session == "after_market"

    candidate = EarningsEvent.objects.get()
    assert candidate.company_id == companies["AAA"].pk
    assert candidate.identity_status == "candidate"
    assert candidate.identity_key is None
    assert candidate.identity_rule_version is None
    assert candidate.period_type is None
    assert candidate.period_end_date == date(2026, 9, 30)
    assert candidate.estimated_release_date == date(2026, 10, 1)
    assert candidate.release_session == "after_market"
    assert candidate.status == "scheduled_estimated"
    assert candidate.confirmed_release_date is None
    assert candidate.earnings_release_date is None
    assert candidate.conference_call_date is None
    assert EarningsEvent.objects.filter(identity_status="canonical").count() == 0

    decision = EarningsReconciliationDecision.objects.get(decision_type="created_candidate")
    assert decision.target_event_id == candidate.pk
    namespace = decision.match_factors["av_v2"]
    assert namespace["matcher_version"] == ALPHA_VANTAGE_COMPANY_MATCHER_VERSION_V2
    assert namespace["forward_capability"] == "nominal_3month"
    assert namespace["past_correction_capability"] == "unsupported"
    assert namespace["coverage_exactness"] == "nominal"
    assert namespace["matched_company_id"] == str(companies["AAA"].pk)

    repeated = _execute(source=source, snapshot=snapshot, provider=provider)
    assert repeated.created is False
    assert transport.calls == 1
    assert SyncRun.objects.count() == 1
    assert EarningsCalendarObservation.objects.count() == 1
    assert EarningsEvent.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_repeated_observation_reuses_candidate_family_and_updates_schedule() -> None:
    source, _companies = _pool()
    snapshot = _snapshot()
    first = _execute(
        source=source,
        snapshot=snapshot,
        provider=_provider(HEADER + b"AAA,Alpha,2026-10-01,2026-09-30,1.23,USD,post-market\n"),
    )
    candidate_id = EarningsEvent.objects.get().pk
    assert first.candidate_created_count == 1

    second = _execute(
        source=source,
        snapshot=snapshot,
        provider=_provider(HEADER + b"AAA,Alpha,2026-10-02,2026-09-30,1.23,USD,pre-market\n"),
        request_id="changed-schedule",
    )

    assert second.created is True
    assert EarningsEvent.objects.count() == 1
    candidate = EarningsEvent.objects.get(pk=candidate_id)
    assert candidate.estimated_release_date == date(2026, 10, 2)
    assert candidate.release_session == "pre_market"
    assert EarningsCalendarObservation.objects.count() == 2
    assert (
        EarningsReconciliationDecision.objects.filter(decision_type="matched_candidate").count()
        == 1
    )
    assert EarningsEvent.objects.filter(identity_status="canonical").count() == 0


@pytest.mark.django_db(transaction=True)
def test_cross_company_symbol_ambiguity_fails_closed() -> None:
    source = _source()
    _company_a, listing_a = _company_with_listing(ticker="AAA", exchange="NASDAQ")
    _company_b, listing_b = _company_with_listing(ticker="AAA", exchange="NYSE")
    _membership(listing_a)
    _membership(listing_b)
    snapshot = _snapshot()
    result = _execute(
        source=source,
        snapshot=snapshot,
        provider=_provider(HEADER + b"AAA,Alpha,2026-10-01,2026-09-30,1.23,USD,post-market\n"),
    )

    assert result.sync_run.status == SyncRun.Status.PARTIAL
    assert result.failed_count == 1
    assert EarningsCalendarObservation.objects.count() == 0
    assert EarningsEvent.objects.count() == 0
    assert EarningsReconciliationDecision.objects.count() == 0
    assert RawDataParseAttempt.objects.get().status == RawDataParseAttempt.Status.SUCCEEDED


@pytest.mark.django_db(transaction=True)
def test_replay_verifier_is_read_only_and_fails_closed_on_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _companies = _pool()
    snapshot = _snapshot()
    transport = CsvTransport(HEADER + b"AAA,Alpha,2026-10-01,2026-09-30,1.23,USD,post-market\n")
    result = _execute(
        source=source,
        snapshot=snapshot,
        provider=AlphaVantageCanonicalProvider(api_key="fixture-key", transport=transport),
    )
    before = (
        SyncRun.objects.count(),
        EarningsCalendarObservation.objects.count(),
        EarningsEvent.objects.count(),
        EarningsReconciliationDecision.objects.count(),
    )
    monkeypatch.setattr(
        "earnings.services.monitoring_pool.select_monitoring_pool",
        lambda **_kwargs: pytest.fail("Replay invoked the current monitoring-pool selector."),
    )
    replay = verify_alpha_vantage_canonical_replay(sync_run=result.sync_run)
    assert replay.resolved_row_count == 1
    assert replay.unresolved_row_count == 0
    assert transport.calls == 1
    assert before == (
        SyncRun.objects.count(),
        EarningsCalendarObservation.objects.count(),
        EarningsEvent.objects.count(),
        EarningsReconciliationDecision.objects.count(),
    )

    raw_record = RawDataRecord.objects.get()
    RawDataRecord.objects.filter(pk=raw_record.pk).update(
        payload=HEADER + b"AAA,Alpha,2026-10-02,2026-06-30,1.23,USD,post-market\n"
    )
    with pytest.raises(AlphaVantageCanonicalReplayIntegrityError):
        verify_alpha_vantage_canonical_replay(sync_run=result.sync_run)


@pytest.mark.django_db(transaction=True)
def test_out_of_pool_symbol_is_excluded_without_domain_writes() -> None:
    source, _companies = _pool()
    snapshot = _snapshot()
    result = _execute(
        source=source,
        snapshot=snapshot,
        provider=_provider(HEADER + b"ZZZ,Outside,2026-10-01,2026-09-30,1.23,USD,post-market\n"),
    )

    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.skipped_count == 1
    assert EarningsCalendarObservation.objects.count() == 0
    assert EarningsEvent.objects.count() == 0
    assert RawDataParseAttempt.objects.get().status == RawDataParseAttempt.Status.SUCCEEDED


@pytest.mark.django_db(transaction=True)
def test_same_company_multiple_basis_listings_resolve_once() -> None:
    source = _source()
    company, listing_a = _company_with_listing(ticker="AAA", exchange="NASDAQ")
    _membership(listing_a)
    listing_b = SecurityListing.objects.create(
        company=company,
        ticker="AAA",
        exchange="NYSE",
        security_name=f"{company.display_name} AAA",
        security_type="common_stock",
        effective_from=date(2026, 1, 1),
    )
    _membership(listing_b)
    snapshot = _snapshot()

    result = _execute(
        source=source,
        snapshot=snapshot,
        provider=_provider(HEADER + b"AAA,Alpha,2026-10-01,2026-09-30,1.23,USD,post-market\n"),
    )

    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert EarningsCalendarObservation.objects.count() == 1
    assert EarningsEvent.objects.count() == 1
    decision = EarningsReconciliationDecision.objects.get(decision_type="created_candidate")
    assert decision.match_factors["av_v2"]["matched_security_listing_ids"] == sorted(
        [str(listing_a.pk), str(listing_b.pk)]
    )


@pytest.mark.django_db(transaction=True)
def test_symbol_reuse_across_snapshots_fails_closed() -> None:
    source = _source()
    company_a, listing_a = _company_with_listing(ticker="AAA", exchange="NASDAQ")
    _membership(listing_a)
    snapshot_a = _snapshot()
    first = _execute(
        source=source,
        snapshot=snapshot_a,
        provider=_provider(HEADER + b"AAA,Alpha,2026-10-01,2026-09-30,1.23,USD,post-market\n"),
    )
    assert first.sync_run.status == SyncRun.Status.SUCCEEDED
    assert EarningsEvent.objects.count() == 1

    IndexMembership.objects.filter(security_listing=listing_a).update(
        status=IndexMembership.Status.ENDED,
        effective_to=AS_OF,
    )
    SecurityListing.objects.filter(pk=listing_a.pk).update(effective_to=AS_OF)
    company_b, listing_b = _company_with_listing(
        ticker="AAA",
        exchange="NASDAQ",
        effective_from=AS_OF,
    )
    _membership(listing_b, effective_from=AS_OF)
    next_date = AS_OF + timedelta(days=1)
    snapshot_b = select_monitoring_pool(
        as_of=next_date,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        enabled_index_codes=("SP500",),
    ).snapshot

    second = _execute(
        source=source,
        snapshot=snapshot_b,
        provider=_provider(HEADER + b"AAA,Alpha,2026-10-01,2026-09-30,1.23,USD,post-market\n"),
        request_id="symbol-reuse",
        run_date=next_date,
    )

    assert second.sync_run.status == SyncRun.Status.PARTIAL
    assert EarningsEvent.objects.count() == 1
    assert EarningsEvent.objects.get().company_id == company_a.pk
    collision = EarningsReconciliationDecision.objects.get(decision_type="collision")
    assert collision.status == "open"
    assert collision.target_event_id is None
    assert collision.match_factors["av_v2"]["matched_company_id"] == str(company_b.pk)


@pytest.mark.django_db(transaction=True)
def test_canonical_sync_does_not_mutate_unrelated_canonical_event() -> None:
    source, _companies = _pool()
    snapshot = _snapshot()
    other_company, _other_listing = _company_with_listing(ticker="BBB", exchange="NYSE")
    canonical = EarningsEvent.objects.create(
        company=other_company,
        identity_status="canonical",
        identity_key=derive_earnings_identity_key(
            company_id=other_company.pk,
            period_end_date=date(2026, 6, 30),
            period_type="Q2",
        ),
        identity_rule_version="v1",
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        includes_q4=False,
        fiscal_calendar_type="unknown",
        status="scheduled_estimated",
    )
    before = (
        canonical.identity_status,
        canonical.identity_key,
        canonical.period_type,
        canonical.status,
        canonical.estimated_release_date,
    )

    result = _execute(
        source=source,
        snapshot=snapshot,
        provider=_provider(HEADER + b"AAA,Alpha,2026-10-01,2026-09-30,1.23,USD,post-market\n"),
    )

    canonical.refresh_from_db()
    assert result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert (
        canonical.identity_status,
        canonical.identity_key,
        canonical.period_type,
        canonical.status,
        canonical.estimated_release_date,
    ) == before
