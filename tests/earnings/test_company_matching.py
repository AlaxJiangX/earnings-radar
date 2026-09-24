from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from django.db import close_old_connections, connection, connections
from django.utils import timezone

from audit.models import (
    AuditRecord,
    DataChange,
    DataSource,
    RawDataObservation,
    RawDataRecord,
    SourceEvidence,
    SyncRun,
)
from companies.models import Company, SecurityListing
from earnings.models import (
    EarningsCalendarObservation,
    EarningsDateChange,
    EarningsEvent,
    EarningsReconciliationDecision,
    FiscalCalendarType,
    MonitoringPoolMember,
    MonitoringPoolSnapshot,
    PeriodType,
)
from earnings.services import (
    EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    EARNINGS_COMPANY_MATCHER_VERSION,
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
    CandidateCreationResult,
    CandidateMatchingIntegrityError,
    CompanyMatchOutcome,
    InvalidCandidateMatchingInput,
    MonitoringPoolIntegrityError,
    UnknownCompanyMatcherVersion,
    build_earnings_calendar_sync_scope,
    create_earnings_candidate_for_observation,
    record_earnings_calendar_observation,
    resolve_monitoring_pool_snapshot,
    select_monitoring_pool,
)
from earnings.services import candidate_matching as candidate_matching_module
from earnings.services.calendar_run_ownership import EarningsCalendarRunBusy
from earnings.services.calendar_sync_identity import EarningsCalendarWindowKind
from indexes.models import IndexMembership, MarketIndex

AS_OF = date(2026, 9, 30)
LISTING_FROM = date(2025, 1, 1)


@dataclass(frozen=True, slots=True)
class _Scenario:
    source: DataSource
    company: Company
    listing: SecurityListing
    snapshot: MonitoringPoolSnapshot
    run: SyncRun
    raw_record: RawDataRecord
    observation: EarningsCalendarObservation


def _source(suffix: str) -> DataSource:
    token = uuid.uuid4().hex[:8]
    provider_key = f"fixture-{suffix[:20]}-{token}"
    return DataSource.objects.create(
        key=f"fixture-{suffix[:30]}-{token}",
        name=f"Fixture {suffix}",
        source_type=DataSource.SourceType.EARNINGS_CALENDAR,
        base_url="https://calendar.example.test/",
        provider_adapter=provider_key,
        license_notes="Synthetic test-only source.",
    )


def _company(
    suffix: str,
    *,
    cik: str | None = None,
    company_id: uuid.UUID | None = None,
) -> Company:
    return Company.objects.create(
        id=company_id or uuid.uuid4(),
        cik=cik,
        legal_name=f"Legal {suffix}",
        display_name=f"Company {suffix}",
    )


def _listing(
    company: Company,
    *,
    ticker: str,
    exchange: str = "NASDAQ",
    effective_from: date = LISTING_FROM,
    effective_to: date | None = None,
    is_primary: bool = False,
) -> SecurityListing:
    return SecurityListing.objects.create(
        company=company,
        ticker=ticker,
        exchange=exchange,
        security_name=f"{company.display_name} {ticker}",
        security_type="common_stock",
        is_primary=is_primary,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _market_index() -> MarketIndex:
    index, _ = MarketIndex.objects.get_or_create(
        code="SP500",
        defaults={
            "name": "S&P 500",
            "index_group": MarketIndex.IndexGroup.LARGE,
            "is_enabled": True,
        },
    )
    return index


def _membership(
    *,
    listing: SecurityListing,
    effective_from: date = LISTING_FROM,
    effective_to: date | None = None,
) -> IndexMembership:
    return IndexMembership.objects.create(
        index=_market_index(),
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _snapshot(*, as_of: date = AS_OF) -> MonitoringPoolSnapshot:
    return select_monitoring_pool(
        as_of=as_of,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        enabled_index_codes=("SP500",),
    ).snapshot


def _run(
    *,
    source: DataSource,
    snapshot: MonitoringPoolSnapshot,
    suffix: str,
    status: str = SyncRun.Status.RUNNING,
) -> SyncRun:
    scope = build_earnings_calendar_sync_scope(
        provider_key=source.provider_adapter,
        window_kind=EarningsCalendarWindowKind.SCHEDULED,
        window_start=snapshot.as_of_date,
        window_end=snapshot.as_of_date,
        monitoring_pool_as_of=snapshot.as_of_date,
        monitoring_pool_hash=snapshot.pool_hash,
        selector_version=snapshot.selector_version,
    )
    now = timezone.now()
    values: dict[str, Any] = {
        "job_type": EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
        "source": source,
        "scope": scope,
        "idempotency_key": f"fixture-{suffix}-{uuid.uuid4()}",
        "status": status,
        "started_at": now,
        "heartbeat_at": now,
        "provider_version": "fixture-provider-v1",
    }
    if status != SyncRun.Status.RUNNING:
        values["finished_at"] = now
    return SyncRun.objects.create(**values)


def _raw_record(
    *,
    source: DataSource,
    run: SyncRun,
    suffix: str,
) -> RawDataRecord:
    payload = f'{{"fixture":"{suffix}-{uuid.uuid4().hex}"}}'.encode()
    return RawDataRecord.objects.create(
        source=source,
        first_sync_run=run,
        source_url=f"https://calendar.example.test/{suffix}",
        request_fingerprint=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        fetched_at=timezone.now(),
        http_status=200,
        content_type="application/json",
        encoding="utf-8",
        content_hash=hashlib.sha256(payload).hexdigest(),
        payload=payload,
        payload_size_bytes=len(payload),
    )


def _observation(
    *,
    source: DataSource,
    run: SyncRun,
    raw_record: RawDataRecord | None = None,
    parser_version: str = "fixture-calendar-parser-v1",
    provider_event_id: str | None = None,
    **overrides: object,
) -> EarningsCalendarObservation:
    raw_record = raw_record or _raw_record(
        source=source,
        run=run,
        suffix=f"observation-{uuid.uuid4().hex[:8]}",
    )
    RawDataObservation.objects.get_or_create(
        sync_run=run,
        raw_data_record=raw_record,
        defaults={"observed_at": timezone.now()},
    )
    values: dict[str, Any] = {
        "provider_key": source.provider_adapter,
        "provider_version": "fixture-provider-v1",
        "parser_version": parser_version,
        "provider_event_id": provider_event_id or f"event-{uuid.uuid4().hex[:8]}",
        "raw_position": 1,
        "cik": "",
        "ticker": "",
        "exchange": "",
        "provider_symbol": "",
        "company_name": "",
        "fiscal_label_raw": "Q1",
        "fiscal_year": 2026,
        "period_end_date": date(2026, 3, 31),
        "period_type": PeriodType.Q1,
        "fiscal_calendar_type": FiscalCalendarType.MONTH_BASED,
        "period_length_weeks": None,
        "estimated_release": None,
        "estimated_release_precision": None,
        "release_session": "unknown",
        "source_observed_at": None,
        "confidence": Decimal("0.9000"),
        **overrides,
    }
    return record_earnings_calendar_observation(
        source=source,
        raw_data_record=raw_record,
        **values,
    ).observation


def _scenario(
    suffix: str,
    *,
    cik: str | None = "0000000123",
    ticker: str = "ACME",
    exchange: str = "NASDAQ",
    as_of: date = AS_OF,
    listing_effective_from: date = LISTING_FROM,
    listing_effective_to: date | None = None,
    observation_overrides: dict[str, Any] | None = None,
) -> _Scenario:
    source = _source(suffix)
    company = _company(suffix, cik=cik)
    listing = _listing(
        company,
        ticker=ticker,
        exchange=exchange,
        effective_from=listing_effective_from,
        effective_to=listing_effective_to,
        is_primary=True,
    )
    _membership(
        listing=listing,
        effective_from=listing_effective_from,
        effective_to=listing_effective_to,
    )
    snapshot = _snapshot(as_of=as_of)
    run = _run(source=source, snapshot=snapshot, suffix=suffix)
    raw_record = _raw_record(source=source, run=run, suffix=suffix)
    observation_values: dict[str, Any] = {
        "cik": cik or "",
        "ticker": ticker,
        "exchange": exchange,
    }
    observation_values.update(observation_overrides or {})
    observation = _observation(
        source=source,
        run=run,
        raw_record=raw_record,
        **observation_values,
    )
    return _Scenario(source, company, listing, snapshot, run, raw_record, observation)


def _match(
    scenario: _Scenario,
    *,
    matcher_version: str = EARNINGS_COMPANY_MATCHER_VERSION,
) -> CandidateCreationResult:
    return create_earnings_candidate_for_observation(
        sync_run=scenario.run,
        observation=scenario.observation,
        matcher_version=matcher_version,
    )


def _factor(result: CandidateCreationResult) -> dict[str, object]:
    value = result.decision.match_factors["company_match"]
    return cast(dict[str, object], value)


def _run_concurrently[T](
    operation: Callable[[], T],
) -> tuple[list[T], list[Exception]]:
    barrier = threading.Barrier(2, timeout=10)
    results: list[T] = []
    errors: list[Exception] = []

    def worker() -> None:
        close_old_connections()
        try:
            barrier.wait()
            results.append(operation())
        except Exception as error:  # pragma: no cover - asserted by each caller
            errors.append(error)
        finally:
            for database_connection in connections.all():
                database_connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert all(not thread.is_alive() for thread in threads)
    return results, errors


def _unwrapped_matcher() -> Callable[..., CandidateCreationResult]:
    wrapped = cast(Any, candidate_matching_module.create_earnings_candidate_for_observation)
    return cast(
        Callable[..., CandidateCreationResult],
        wrapped.__wrapped__,
    )


@pytest.mark.django_db
def test_exact_cik_matches_in_pool_and_persists_candidate_lineage() -> None:
    scenario = _scenario(
        "cik",
        observation_overrides={"ticker": "", "exchange": ""},
    )

    result = _match(scenario)

    assert result.outcome is CompanyMatchOutcome.MATCHED
    assert result.candidate is not None
    assert result.source_evidence is not None
    assert result.candidate.company_id == scenario.company.pk
    assert result.candidate.identity_status == "candidate"
    assert result.candidate.identity_key is None
    assert result.candidate.identity_rule_version is None
    assert result.candidate.status == "scheduled_estimated"
    assert result.candidate.period_type == PeriodType.Q1
    assert result.candidate.period_end_date == date(2026, 3, 31)
    assert result.candidate.includes_q4 is False
    assert result.candidate.source_evidence_id == result.source_evidence.pk
    assert result.decision.decision_type == "created_candidate"
    assert result.decision.status == "resolved"
    assert result.decision.target_event_id == result.candidate.pk
    assert _factor(result)["match_status"] == "MATCHED"
    assert _factor(result)["match_strategy"] == "cik"
    assert _factor(result)["matched_company_id"] == str(scenario.company.pk)
    assert _factor(result)["matched_security_listing_ids"] == []
    assert result.source_evidence.normalized_value == result.decision.match_factors
    assert result.source_evidence.confidence == Decimal("0.9000")
    assert (
        AuditRecord.objects.filter(
            sync_run=scenario.run,
            target_type=AuditRecord.TargetType.EARNINGS_EVENT,
            target_id=result.candidate.pk,
            action=AuditRecord.Action.CREATE,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_exact_ticker_and_exchange_match_in_pool() -> None:
    scenario = _scenario(
        "ticker",
        cik=None,
        observation_overrides={"ticker": "  acme ", "exchange": " nasdaq "},
    )

    result = _match(scenario)

    assert result.outcome is CompanyMatchOutcome.MATCHED
    assert result.candidate is not None
    assert result.candidate.company_id == scenario.company.pk
    assert _factor(result)["match_strategy"] == "exchange_ticker"
    assert _factor(result)["normalized_ticker"] == "ACME"
    assert _factor(result)["normalized_exchange"] == "NASDAQ"
    assert _factor(result)["matched_security_listing_ids"] == [str(scenario.listing.pk)]


@pytest.mark.django_db
def test_both_tiers_matching_same_company_preserve_listing_evidence() -> None:
    scenario = _scenario("both-tiers")

    result = _match(scenario)

    assert result.outcome is CompanyMatchOutcome.MATCHED
    assert _factor(result)["match_strategy"] == "cik+exchange_ticker"
    assert _factor(result)["matched_security_listing_ids"] == [str(scenario.listing.pk)]


@pytest.mark.django_db
def test_conflicting_tiers_are_ambiguous_and_create_no_candidate() -> None:
    source = _source("tier-conflict")
    first = _company("tier-conflict-a", cik="0000000111")
    second = _company("tier-conflict-b", cik="0000000222")
    first_listing = _listing(first, ticker="AAAA")
    second_listing = _listing(second, ticker="BBBB", exchange="NYSE")
    _membership(listing=first_listing)
    _membership(listing=second_listing)
    snapshot = _snapshot()
    run = _run(source=source, snapshot=snapshot, suffix="tier-conflict")
    observation = _observation(
        source=source,
        run=run,
        cik="0000000111",
        ticker="BBBB",
        exchange="NYSE",
    )

    result = create_earnings_candidate_for_observation(
        sync_run=run,
        observation=observation,
    )

    assert result.outcome is CompanyMatchOutcome.AMBIGUOUS
    assert result.candidate is None
    assert result.source_evidence is None
    assert result.decision.decision_type == "review_required"
    assert result.decision.status == "open"
    assert result.decision.target_event_id is None
    assert _factor(result)["match_strategy"] == "multiple"
    exact_company_ids = _factor(result)["exact_company_ids"]
    assert isinstance(exact_company_ids, list)
    assert len(exact_company_ids) == 2


@pytest.mark.parametrize(
    ("suffix", "overrides"),
    (
        ("ticker-only", {"cik": "", "ticker": "ACME", "exchange": ""}),
        ("name-only", {"cik": "", "ticker": "", "exchange": "", "company_name": "Company name"}),
        (
            "symbol-only",
            {"cik": "", "ticker": "", "exchange": "", "provider_symbol": "ACME"},
        ),
        ("missing-hints", {"cik": "", "ticker": "", "exchange": ""}),
        (
            "unknown-exchange",
            {"cik": "", "ticker": "ACME", "exchange": "OTC-UNKNOWN"},
        ),
        ("partial-ticker", {"cik": "", "ticker": "ACM", "exchange": "NASDAQ"}),
    ),
)
@pytest.mark.django_db
def test_non_exact_inputs_are_unmatched(
    suffix: str,
    overrides: dict[str, Any],
) -> None:
    scenario = _scenario(suffix, cik=None, observation_overrides=overrides)

    result = _match(scenario)

    assert result.outcome is CompanyMatchOutcome.UNMATCHED
    assert result.candidate is None
    assert result.source_evidence is None
    assert result.decision.decision_type == "no_match"
    assert result.decision.status == "rejected"
    assert result.decision.target_event_id is None
    assert _factor(result)["reason_code"] == "no_exact_match"


@pytest.mark.django_db
def test_same_ticker_different_exchange_does_not_cross_match() -> None:
    source = _source("exchange-isolation")
    first = _company("exchange-isolation-a", cik=None)
    second = _company("exchange-isolation-b", cik=None)
    first_listing = _listing(first, ticker="SAME", exchange="NYSE")
    second_listing = _listing(second, ticker="SAME", exchange="NASDAQ")
    _membership(listing=first_listing)
    _membership(listing=second_listing)
    snapshot = _snapshot()
    run = _run(source=source, snapshot=snapshot, suffix="exchange-isolation")
    observation = _observation(
        source=source,
        run=run,
        ticker="SAME",
        exchange="NASDAQ",
    )

    result = create_earnings_candidate_for_observation(
        sync_run=run,
        observation=observation,
    )

    assert result.outcome is CompanyMatchOutcome.MATCHED
    assert result.candidate is not None
    assert result.candidate.company_id == second.pk
    assert _factor(result)["matched_security_listing_ids"] == [str(second_listing.pk)]


@pytest.mark.django_db
def test_one_company_with_multiple_listings_still_matches_one_company() -> None:
    scenario = _scenario("multiple-listings", cik="0000000999")
    secondary = _listing(
        scenario.company,
        ticker="ACME-B",
        exchange="NYSE",
        effective_from=AS_OF - timedelta(days=1),
    )
    _membership(listing=secondary, effective_from=AS_OF - timedelta(days=1))

    result = _match(scenario)

    assert result.outcome is CompanyMatchOutcome.MATCHED
    assert result.candidate is not None
    assert result.candidate.company_id == scenario.company.pk
    assert _factor(result)["matched_company_id"] == str(scenario.company.pk)


@pytest.mark.django_db
def test_exact_company_outside_frozen_pool_is_out_of_pool() -> None:
    source = _source("out-of-pool")
    inside = _company("out-of-pool-inside", cik="0000000001")
    inside_listing = _listing(inside, ticker="INPOOL")
    _membership(listing=inside_listing)
    outside = _company("out-of-pool-outside", cik="0000000002")
    outside_listing = _listing(outside, ticker="OUTSIDE")
    snapshot = _snapshot()
    run = _run(source=source, snapshot=snapshot, suffix="out-of-pool")
    observation = _observation(
        source=source,
        run=run,
        cik="0000000002",
        ticker="OUTSIDE",
        exchange="NASDAQ",
    )

    result = create_earnings_candidate_for_observation(
        sync_run=run,
        observation=observation,
    )

    assert result.outcome is CompanyMatchOutcome.OUT_OF_POOL
    assert result.candidate is None
    assert result.source_evidence is None
    assert result.decision.decision_type == "ignored"
    assert result.decision.status == "rejected"
    assert result.decision.target_event_id is None
    assert _factor(result)["matched_company_id"] is None
    assert _factor(result)["matched_outside_pool_company_ids"] == [str(outside.pk)]
    assert _factor(result)["matched_outside_pool_security_listing_ids"] == [str(outside_listing.pk)]
    assert MonitoringPoolMember.objects.filter(snapshot=snapshot).count() == 1
    assert not MonitoringPoolMember.objects.filter(
        snapshot=snapshot,
        company=outside,
    ).exists()


@pytest.mark.django_db
def test_empty_snapshot_is_unmatched_and_deterministic() -> None:
    source = _source("empty-pool")
    snapshot = _snapshot()
    run = _run(source=source, snapshot=snapshot, suffix="empty-pool")
    observation = _observation(
        source=source,
        run=run,
        cik="0000000123",
        ticker="ACME",
        exchange="NASDAQ",
    )

    first = create_earnings_candidate_for_observation(
        sync_run=run,
        observation=observation,
    )
    second = create_earnings_candidate_for_observation(
        sync_run=run,
        observation=observation,
    )

    assert first.outcome is CompanyMatchOutcome.UNMATCHED
    assert second.outcome is CompanyMatchOutcome.UNMATCHED
    assert first.decision.pk == second.decision.pk
    assert first.candidate is None
    assert first.decision_created is True
    assert second.decision_created is False
    assert EarningsReconciliationDecision.objects.count() == 1


@pytest.mark.django_db
def test_candidate_matching_never_calls_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario("no-selector")
    import earnings.services.monitoring_pool as monitoring_pool_module

    def fail_selector(*args: object, **kwargs: object) -> object:
        raise AssertionError("Candidate matching must not run the selector.")

    monkeypatch.setattr(monitoring_pool_module, "select_monitoring_pool", fail_selector)
    result = _match(scenario)

    assert result.outcome is CompanyMatchOutcome.MATCHED


@pytest.mark.parametrize(
    ("suffix", "effective_from", "effective_to", "expected"),
    (
        ("before-start", AS_OF + timedelta(days=1), None, CompanyMatchOutcome.UNMATCHED),
        ("at-start", AS_OF, None, CompanyMatchOutcome.MATCHED),
        ("before-end", LISTING_FROM, AS_OF + timedelta(days=1), CompanyMatchOutcome.MATCHED),
        ("at-end", LISTING_FROM, AS_OF, CompanyMatchOutcome.UNMATCHED),
    ),
)
@pytest.mark.django_db
def test_listing_temporal_boundaries_use_half_open_interval(
    suffix: str,
    effective_from: date,
    effective_to: date | None,
    expected: CompanyMatchOutcome,
) -> None:
    source = _source(suffix)
    company = _company(suffix, cik=None)
    basis = _listing(company, ticker="BASIS")
    _membership(listing=basis)
    _listing(
        company,
        ticker="BOUNDARY",
        effective_from=effective_from,
        effective_to=effective_to,
    )
    snapshot = _snapshot()
    run = _run(source=source, snapshot=snapshot, suffix=suffix)
    observation = _observation(
        source=source,
        run=run,
        ticker="BOUNDARY",
        exchange="NASDAQ",
    )

    result = create_earnings_candidate_for_observation(
        sync_run=run,
        observation=observation,
    )

    assert result.outcome is expected


@pytest.mark.django_db
def test_ticker_rename_resolves_at_monitoring_pool_as_of() -> None:
    historical_as_of = date(2026, 3, 31)
    source = _source("ticker-rename")
    company = _company("ticker-rename", cik=None)
    old_listing = _listing(
        company,
        ticker="OLD",
        effective_from=LISTING_FROM,
        effective_to=date(2026, 7, 1),
    )
    new_listing = _listing(
        company,
        ticker="NEW",
        effective_from=date(2026, 7, 1),
    )
    _membership(
        listing=old_listing,
        effective_from=LISTING_FROM,
        effective_to=date(2026, 7, 1),
    )
    _membership(
        listing=new_listing,
        effective_from=date(2026, 7, 1),
    )
    historical_snapshot = _snapshot(as_of=historical_as_of)
    historical_run = _run(
        source=source,
        snapshot=historical_snapshot,
        suffix="ticker-rename-historical",
    )
    historical_observation = _observation(
        source=source,
        run=historical_run,
        ticker="OLD",
        exchange="NASDAQ",
    )

    historical = create_earnings_candidate_for_observation(
        sync_run=historical_run,
        observation=historical_observation,
    )

    current_snapshot = _snapshot(as_of=AS_OF)
    current_run = _run(
        source=source,
        snapshot=current_snapshot,
        suffix="ticker-rename-current",
    )
    current_observation = _observation(
        source=source,
        run=current_run,
        ticker="NEW",
        exchange="NASDAQ",
    )
    current = create_earnings_candidate_for_observation(
        sync_run=current_run,
        observation=current_observation,
    )

    assert historical.outcome is CompanyMatchOutcome.MATCHED
    assert historical.candidate is not None
    assert historical.candidate.company_id == company.pk
    assert _factor(historical)["matched_security_listing_ids"] == [str(old_listing.pk)]
    assert current.outcome is CompanyMatchOutcome.MATCHED
    assert current.candidate is not None
    assert current.candidate.company_id == company.pk
    assert _factor(current)["matched_security_listing_ids"] == [str(new_listing.pk)]


@pytest.mark.django_db
def test_earnings_date_and_current_clock_do_not_change_listing_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(
        "temporal-isolation",
        observation_overrides={
            "estimated_release": date(2027, 3, 31),
            "estimated_release_precision": "date_only",
        },
    )

    def fail_localdate() -> date:
        raise AssertionError("Matching must not use the current wall clock.")

    monkeypatch.setattr("django.utils.timezone.localdate", fail_localdate)
    result = _match(scenario)

    assert result.outcome is CompanyMatchOutcome.MATCHED


@pytest.mark.django_db
def test_same_execution_reuses_decision_candidate_and_revision() -> None:
    scenario = _scenario("same-execution")

    first = _match(scenario)
    second = _match(scenario)

    assert first.candidate is not None
    assert second.candidate is not None
    assert first.matching_input_revision == second.matching_input_revision
    assert first.match_execution_key == second.match_execution_key
    assert first.match_result_key == second.match_result_key
    assert first.decision.pk == second.decision.pk
    assert first.candidate.pk == second.candidate.pk
    assert first.decision_created is True
    assert second.decision_created is False
    assert first.candidate_created is True
    assert second.candidate_created is False
    assert EarningsEvent.objects.count() == 1
    assert EarningsReconciliationDecision.objects.count() == 1
    assert SourceEvidence.objects.count() == 1


@pytest.mark.django_db
def test_created_at_and_updated_at_changes_do_not_change_revision() -> None:
    scenario = _scenario("metadata-revision")
    first = _match(scenario)

    Company.objects.filter(pk=scenario.company.pk).update(
        created_at=timezone.now() + timedelta(days=30),
        updated_at=timezone.now() + timedelta(days=40),
    )
    second = _match(scenario)

    assert second.matching_input_revision == first.matching_input_revision
    assert second.candidate is not None
    assert first.candidate is not None
    assert second.candidate.pk == first.candidate.pk


@pytest.mark.django_db
def test_matching_input_revision_is_order_independent_and_version_sensitive() -> None:
    scenario = _scenario("revision-order")
    second_company = _company("revision-order-secondary", cik="0000000777")
    second_listing = _listing(second_company, ticker="SECOND", exchange="NYSE")
    _membership(listing=second_listing)
    snapshot = _snapshot()
    run = _run(source=scenario.source, snapshot=snapshot, suffix="revision-order")
    raw_record = _raw_record(source=scenario.source, run=run, suffix="revision-order")
    observation = _observation(
        source=scenario.source,
        run=run,
        raw_record=raw_record,
        cik=scenario.company.cik or "",
        ticker=scenario.listing.ticker,
        exchange=scenario.listing.exchange,
    )
    reference = resolve_monitoring_pool_snapshot(run)
    facts = candidate_matching_module._build_match_facts(
        sync_run=run,
        observation=observation,
        snapshot_reference=reference,
        matcher_version=EARNINGS_COMPANY_MATCHER_VERSION,
    )
    reversed_revision = candidate_matching_module._build_matching_input_revision(
        sync_run=run,
        observation=observation,
        snapshot=facts.snapshot,
        hints=facts.hints,
        pool_companies=tuple(reversed(facts.pool_companies)),
        pool_listings=tuple(reversed(facts.pool_listings)),
        cik_companies=facts.cik_companies,
        listing_matches=facts.listing_matches,
        matcher_version=EARNINGS_COMPANY_MATCHER_VERSION,
    )
    next_version_revision = candidate_matching_module._build_matching_input_revision(
        sync_run=run,
        observation=observation,
        snapshot=facts.snapshot,
        hints=facts.hints,
        pool_companies=facts.pool_companies,
        pool_listings=facts.pool_listings,
        cik_companies=facts.cik_companies,
        listing_matches=facts.listing_matches,
        matcher_version="earnings-company-match-v2",
    )

    assert facts.matching_input_revision == reversed_revision
    assert next_version_revision != facts.matching_input_revision


@pytest.mark.django_db
def test_company_cik_correction_creates_new_revision_and_candidate_lineage() -> None:
    scenario = _scenario("cik-correction", cik=None)
    first = _match(scenario)

    Company.objects.filter(pk=scenario.company.pk).update(cik="0000000777")
    second = _match(scenario)

    assert first.candidate is not None
    assert second.candidate is not None
    assert first.matching_input_revision != second.matching_input_revision
    assert first.match_execution_key != second.match_execution_key
    assert first.decision.pk != second.decision.pk
    assert first.candidate.pk != second.candidate.pk
    assert first.candidate.company_id == second.candidate.company_id == scenario.company.pk
    assert EarningsEvent.objects.count() == 2
    assert EarningsReconciliationDecision.objects.count() == 2


@pytest.mark.django_db
def test_listing_temporal_correction_creates_new_revision_and_candidate_lineage() -> None:
    scenario = _scenario("listing-correction")
    secondary = _listing(
        scenario.company,
        ticker="SECONDARY",
        exchange="NYSE",
        effective_from=LISTING_FROM,
    )
    first = _match(scenario)

    SecurityListing.objects.filter(pk=secondary.pk).update(
        effective_to=scenario.snapshot.as_of_date
    )
    second = _match(scenario)

    assert first.candidate is not None
    assert second.candidate is not None
    assert first.matching_input_revision != second.matching_input_revision
    assert first.candidate.pk != second.candidate.pk
    assert EarningsEvent.objects.filter(pk=first.candidate.pk).exists()
    assert EarningsReconciliationDecision.objects.filter(pk=first.decision.pk).exists()


@pytest.mark.django_db
def test_snapshot_change_creates_new_revision_and_candidate_lineage() -> None:
    source = _source("snapshot-change")
    first_company = _company("snapshot-change-a", cik="0000000001")
    first_listing = _listing(first_company, ticker="FIRST")
    _membership(listing=first_listing)
    first_snapshot = _snapshot()
    first_run = _run(source=source, snapshot=first_snapshot, suffix="snapshot-change-first")
    raw_record = _raw_record(source=source, run=first_run, suffix="snapshot-change")
    observation = _observation(
        source=source,
        run=first_run,
        raw_record=raw_record,
        cik="0000000001",
        ticker="FIRST",
    )
    first = create_earnings_candidate_for_observation(
        sync_run=first_run,
        observation=observation,
    )

    second_company = _company("snapshot-change-b", cik="0000000002")
    second_listing = _listing(second_company, ticker="SECOND")
    _membership(listing=second_listing)
    second_snapshot = _snapshot()
    second_run = _run(source=source, snapshot=second_snapshot, suffix="snapshot-change-second")
    RawDataObservation.objects.create(
        sync_run=second_run,
        raw_data_record=raw_record,
        observed_at=timezone.now(),
    )
    second = create_earnings_candidate_for_observation(
        sync_run=second_run,
        observation=observation,
    )

    assert first_snapshot.pk != second_snapshot.pk
    assert first.candidate is not None
    assert second.candidate is not None
    assert first.matching_input_revision != second.matching_input_revision
    assert first.candidate.pk != second.candidate.pk


@pytest.mark.django_db
def test_unknown_matcher_version_fails_closed_without_writes() -> None:
    scenario = _scenario("unknown-version")

    with pytest.raises(UnknownCompanyMatcherVersion):
        _match(scenario, matcher_version="earnings-company-match-v999")

    assert EarningsEvent.objects.count() == 0
    assert EarningsReconciliationDecision.objects.count() == 0
    assert SourceEvidence.objects.count() == 0


@pytest.mark.django_db
def test_parser_revision_creates_new_candidate_lineage_without_rewriting_old_history() -> None:
    scenario = _scenario("parser-revision")
    first = _match(scenario)
    old_candidate_id = first.candidate.pk if first.candidate is not None else None
    old_decision_id = first.decision.pk
    old_evidence_id = first.source_evidence.pk if first.source_evidence is not None else None
    old_period_end = first.candidate.period_end_date if first.candidate is not None else None

    revised_observation = _observation(
        source=scenario.source,
        run=scenario.run,
        raw_record=scenario.raw_record,
        parser_version="fixture-calendar-parser-v2",
        provider_event_id=scenario.observation.provider_event_id,
        cik=scenario.observation.cik,
        ticker=scenario.observation.ticker,
        exchange=scenario.observation.exchange,
        period_end_date=date(2026, 3, 30),
        period_type=PeriodType.Q1,
    )
    second = create_earnings_candidate_for_observation(
        sync_run=scenario.run,
        observation=revised_observation,
    )

    assert first.candidate is not None
    assert second.candidate is not None
    assert second.candidate.pk != old_candidate_id
    assert second.decision.pk != old_decision_id
    assert second.source_evidence is not None
    assert second.source_evidence.pk != old_evidence_id
    assert first.source_evidence is not None
    first.candidate.refresh_from_db()
    first.decision.refresh_from_db()
    first.source_evidence.refresh_from_db()
    assert first.candidate.period_end_date == old_period_end
    assert first.decision.pk == old_decision_id
    assert first.source_evidence.pk == old_evidence_id


@pytest.mark.django_db
def test_replay_run_reuses_same_candidate_for_same_observation_and_snapshot() -> None:
    scenario = _scenario("replay")
    first = _match(scenario)
    replay_run = _run(
        source=scenario.source,
        snapshot=scenario.snapshot,
        suffix="replay-run",
    )
    RawDataObservation.objects.create(
        sync_run=replay_run,
        raw_data_record=scenario.raw_record,
        observed_at=timezone.now(),
    )

    replay = create_earnings_candidate_for_observation(
        sync_run=replay_run,
        observation=scenario.observation,
    )

    assert first.candidate is not None
    assert replay.candidate is not None
    assert replay.candidate.pk == first.candidate.pk
    assert replay.decision.pk == first.decision.pk
    assert replay.matching_input_revision == first.matching_input_revision
    assert replay.decision_created is False
    assert replay.candidate_created is False
    assert EarningsEvent.objects.count() == 1
    assert EarningsReconciliationDecision.objects.count() == 1


@pytest.mark.django_db
def test_candidate_schedule_facts_are_written_through_schedule_service() -> None:
    scenario = _scenario(
        "schedule-service",
        observation_overrides={
            "estimated_release": date(2026, 10, 22),
            "estimated_release_precision": "date_only",
            "release_session": "after_market",
        },
    )

    result = _match(scenario)

    assert result.candidate is not None
    assert result.candidate.estimated_release_date == date(2026, 10, 22)
    assert result.candidate.estimated_release_at is None
    assert result.candidate.estimated_release_precision == "date_only"
    assert result.candidate.release_session == "after_market"
    assert set(
        EarningsDateChange.objects.filter(earnings_event=result.candidate).values_list(
            "field_name",
            flat=True,
        )
    ) == {"estimated_release", "release_session"}
    assert (
        DataChange.objects.filter(
            target_type=DataChange.TargetType.EARNINGS_EVENT,
            target_id=result.candidate.pk,
        ).count()
        == 2
    )
    assert (
        AuditRecord.objects.filter(
            sync_run=scenario.run,
            target_type=AuditRecord.TargetType.EARNINGS_EVENT,
            target_id=result.candidate.pk,
        ).count()
        == 2
    )


@pytest.mark.django_db
def test_zero_confidence_is_preserved_in_candidate_evidence() -> None:
    scenario = _scenario(
        "zero-confidence",
        observation_overrides={"confidence": Decimal("0.0000")},
    )

    result = _match(scenario)

    assert result.source_evidence is not None
    assert result.source_evidence.confidence == Decimal("0.0000")


@pytest.mark.django_db
def test_inconsistent_persisted_match_evidence_fails_closed() -> None:
    scenario = _scenario("corrupt-evidence")
    first = _match(scenario)
    assert first.source_evidence is not None
    SourceEvidence.objects.filter(pk=first.source_evidence.pk).update(
        normalized_value={"company_match": {"tampered": True}}
    )

    with pytest.raises(CandidateMatchingIntegrityError, match="SourceEvidence"):
        _match(scenario)

    assert EarningsEvent.objects.count() == 1
    assert EarningsReconciliationDecision.objects.count() == 1


@pytest.mark.django_db
def test_inconsistent_existing_candidate_fails_closed_without_second_candidate() -> None:
    scenario = _scenario("corrupt-candidate")
    first = _match(scenario)
    assert first.candidate is not None
    EarningsEvent.objects.filter(pk=first.candidate.pk).update(period_end_date=date(2026, 3, 30))

    with pytest.raises(CandidateMatchingIntegrityError, match="immutable candidate"):
        _match(scenario)

    assert EarningsEvent.objects.count() == 1
    assert EarningsReconciliationDecision.objects.count() == 1


@pytest.mark.django_db
def test_terminal_run_rejects_candidate_phase() -> None:
    source = _source("terminal-run")
    company = _company("terminal-run", cik="0000000001")
    listing = _listing(company, ticker="TERM")
    _membership(listing=listing)
    snapshot = _snapshot()
    run = _run(
        source=source,
        snapshot=snapshot,
        suffix="terminal-run",
        status=SyncRun.Status.SUCCEEDED,
    )
    observation = _observation(
        source=source,
        run=run,
        cik="0000000001",
        ticker="TERM",
    )

    with pytest.raises(InvalidCandidateMatchingInput, match="running"):
        create_earnings_candidate_for_observation(
            sync_run=run,
            observation=observation,
        )


@pytest.mark.django_db
def test_missing_snapshot_and_corrupted_snapshot_fail_closed() -> None:
    source = _source("snapshot-failure")
    snapshot = _snapshot()
    run = _run(source=source, snapshot=snapshot, suffix="snapshot-failure")
    observation = _observation(
        source=source,
        run=run,
        cik="0000000123",
        ticker="ACME",
    )
    original_scope = dict(run.scope)
    run.scope = {**original_scope, "monitoring_pool_hash": "0" * 64}
    run.save(update_fields=("scope",))

    with pytest.raises(MonitoringPoolIntegrityError, match="exactly one snapshot"):
        create_earnings_candidate_for_observation(
            sync_run=run,
            observation=observation,
        )

    run.scope = original_scope
    run.save(update_fields=("scope",))
    table = MonitoringPoolSnapshot._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE "{table}" SET member_count = %s WHERE id = %s',
            [99, snapshot.pk],
        )

    with pytest.raises(MonitoringPoolIntegrityError, match="member count"):
        create_earnings_candidate_for_observation(
            sync_run=run,
            observation=observation,
        )


@pytest.mark.django_db
def test_observation_must_belong_to_run_raw_lineage() -> None:
    scenario = _scenario("bad-lineage")
    other_run = _run(
        source=scenario.source,
        snapshot=scenario.snapshot,
        suffix="bad-lineage-other",
    )

    with pytest.raises(InvalidCandidateMatchingInput, match="observed"):
        create_earnings_candidate_for_observation(
            sync_run=other_run,
            observation=scenario.observation,
        )


@pytest.mark.django_db
def test_different_sources_do_not_merge_candidates() -> None:
    first_source = _source("independent-source-a")
    second_source = _source("independent-source-b")
    company = _company("independent-source", cik="0000000456")
    listing = _listing(company, ticker="INDEP", exchange="NASDAQ")
    _membership(listing=listing)
    snapshot = _snapshot()
    first_run = _run(
        source=first_source,
        snapshot=snapshot,
        suffix="independent-source-a",
    )
    second_run = _run(
        source=second_source,
        snapshot=snapshot,
        suffix="independent-source-b",
    )
    first_observation = _observation(
        source=first_source,
        run=first_run,
        cik="0000000456",
        ticker="INDEP",
        exchange="NASDAQ",
    )
    second_observation = _observation(
        source=second_source,
        run=second_run,
        cik="0000000456",
        ticker="INDEP",
        exchange="NASDAQ",
    )

    first_result = create_earnings_candidate_for_observation(
        sync_run=first_run,
        observation=first_observation,
    )
    second_result = create_earnings_candidate_for_observation(
        sync_run=second_run,
        observation=second_observation,
    )

    assert first_result.candidate is not None
    assert second_result.candidate is not None
    assert first_result.candidate.pk != second_result.candidate.pk
    assert first_result.candidate.company_id == second_result.candidate.company_id == company.pk
    assert first_result.decision.pk != second_result.decision.pk
    assert EarningsEvent.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_public_service_uses_existing_run_ownership() -> None:
    scenario = _scenario("ownership")
    errors: list[Exception] = []

    def contender() -> None:
        close_old_connections()
        try:
            _match(scenario)
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)
        finally:
            for database_connection in connections.all():
                database_connection.close()

    from earnings.services.calendar_run_ownership import calendar_run_ownership

    with calendar_run_ownership(
        source_id=scenario.source.pk,
        job_type=EARNINGS_CALENDAR_WINDOW_JOB_TYPE,
    ):
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=15)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], EarningsCalendarRunBusy)
    assert EarningsEvent.objects.count() == 0
    assert EarningsReconciliationDecision.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_matched_execution_creates_one_decision_and_candidate() -> None:
    scenario = _scenario("concurrent-matched")
    unwrapped = _unwrapped_matcher()

    def operation() -> CandidateCreationResult:
        return unwrapped(sync_run=scenario.run, observation=scenario.observation)

    results, errors = _run_concurrently(operation)

    assert errors == []
    assert len(results) == 2
    assert {result.decision.pk for result in results} == {results[0].decision.pk}
    first_candidate = results[0].candidate
    assert first_candidate is not None
    candidate_ids: set[uuid.UUID] = set()
    for result in results:
        assert result.candidate is not None
        candidate_ids.add(result.candidate.pk)
    assert candidate_ids == {first_candidate.pk}
    assert sum(result.decision_created for result in results) == 1
    assert EarningsEvent.objects.count() == 1
    assert EarningsReconciliationDecision.objects.count() == 1
    assert SourceEvidence.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_unmatched_execution_creates_one_decision() -> None:
    scenario = _scenario(
        "concurrent-unmatched",
        cik=None,
        observation_overrides={"ticker": "MISSING", "exchange": "NASDAQ"},
    )
    unwrapped = _unwrapped_matcher()

    def operation() -> CandidateCreationResult:
        return unwrapped(sync_run=scenario.run, observation=scenario.observation)

    results, errors = _run_concurrently(operation)

    assert errors == []
    assert len(results) == 2
    assert {result.decision.pk for result in results} == {results[0].decision.pk}
    assert all(result.candidate is None for result in results)
    assert sum(result.decision_created for result in results) == 1
    assert EarningsReconciliationDecision.objects.count() == 1
    assert EarningsEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_ambiguous_execution_creates_one_decision() -> None:
    source = _source("concurrent-ambiguous")
    first = _company("concurrent-ambiguous-a", cik="0000000111")
    second = _company("concurrent-ambiguous-b", cik="0000000222")
    first_listing = _listing(first, ticker="AAAA")
    second_listing = _listing(second, ticker="BBBB", exchange="NYSE")
    _membership(listing=first_listing)
    _membership(listing=second_listing)
    snapshot = _snapshot()
    run = _run(source=source, snapshot=snapshot, suffix="concurrent-ambiguous")
    observation = _observation(
        source=source,
        run=run,
        cik="0000000111",
        ticker="BBBB",
        exchange="NYSE",
    )
    unwrapped = _unwrapped_matcher()

    def operation() -> CandidateCreationResult:
        return unwrapped(sync_run=run, observation=observation)

    results, errors = _run_concurrently(operation)

    assert errors == []
    assert len(results) == 2
    assert {result.decision.pk for result in results} == {results[0].decision.pk}
    assert all(result.outcome is CompanyMatchOutcome.AMBIGUOUS for result in results)
    assert all(result.candidate is None for result in results)
    assert sum(result.decision_created for result in results) == 1
    assert EarningsReconciliationDecision.objects.count() == 1
    assert EarningsEvent.objects.count() == 0


@pytest.mark.django_db
def test_matching_module_has_no_provider_or_fuzzy_dependency() -> None:
    source = candidate_matching_module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as file:
        module_source = file.read().lower()

    assert "levenshtein" not in module_source
    assert "fuzzy" not in module_source
    assert "providerclient" not in module_source
    assert "requests" not in module_source
    assert "httpx" not in module_source
    assert EARNINGS_COMPANY_MATCHER_VERSION == "earnings-company-match-v1"
