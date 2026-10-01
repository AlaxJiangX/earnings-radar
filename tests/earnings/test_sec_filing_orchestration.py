"""Stage 4.5A-I2 live orchestration and persisted-only replay tests."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from django.utils import timezone

import earnings.services.sec_filing_sync as sec_filing_sync_module
from audit.models import AuditRecord, DataChange, DataSource, DomainTargetType, SyncRun
from audit.services import (
    record_raw_data_observation,
    record_source_evidence,
    start_sync_run_with_result,
)
from earnings.models import FilingEarningsDecision, FilingEarningsLink, MonitoringPoolSnapshot
from earnings.services import (
    FilingEarningsEvaluationResult,
    SecFilingOrchestrationResult,
    confirm_filing_earnings_link,
    evaluate_filing_earnings_link,
    execute_sec_filing_sync,
    replay_filing_earnings_matching,
)
from earnings.services.sec_filing_sync import SecFilingReplayError
from filings.models import Filing
from filings.parsing import PARSER_VERSION
from filings.sync import SEC_JOB_TYPE
from providers.sec_edgar import SecEdgarProvider
from tests.earnings.helpers import make_event, make_user
from tests.filings.sec_sync_helpers import (
    FilingSpec,
    SecFilingTransport,
    make_company,
    make_pool,
    make_sec_source,
)


def _provider(transport: SecFilingTransport) -> SecEdgarProvider:
    return SecEdgarProvider(user_agent="Earnings Radar test@example.org", transport=transport)


def _run(
    *,
    source: DataSource,
    snapshot: MonitoringPoolSnapshot,
    transport: SecFilingTransport,
    key: str = "live",
) -> SecFilingOrchestrationResult:
    return execute_sec_filing_sync(
        source=source,
        provider=_provider(transport),
        snapshot=snapshot,
        idempotency_key=key,
    )


@pytest.mark.django_db(transaction=True)
def test_live_sync_evaluates_periodic_filing_in_same_run() -> None:
    company = make_company(cik="0000001234")
    event = make_event(company=company, period_end_date=date(2026, 3, 31), period_type="Q1")
    source = make_sec_source()
    snapshot = make_pool(companies=(company,))
    transport = SecFilingTransport(FilingSpec(form="10-Q", period_of_report="2026-03-31"))

    result = _run(source=source, snapshot=snapshot, transport=transport)

    run = result.sec_sync_result.sync_run
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.failed_count == 0
    assert result.matching.filings_evaluated == 1
    assert result.matching.matched_periodic == 1
    assert result.matching.matching_failures == 0
    link = FilingEarningsLink.objects.get()
    assert link.earnings_event_id == event.pk
    assert link.relation_type == "PERIODIC_FILING"
    decision = FilingEarningsDecision.objects.get()
    assert decision.sync_run_id == run.pk
    assert decision.status == "resolved"


@pytest.mark.django_db(transaction=True)
def test_live_sync_evaluates_release_filing_and_classification() -> None:
    company = make_company(cik="0000001234")
    event = make_event(
        company=company,
        period_end_date=date(2026, 3, 31),
        period_type="Q1",
        estimated_release_date=date(2026, 3, 9),
        estimated_release_precision="date_only",
    )
    source = make_sec_source()
    snapshot = make_pool(companies=(company,))
    transport = SecFilingTransport(FilingSpec(form="8-K", items="2.02", exhibit_type="EX-99.1"))

    result = _run(source=source, snapshot=snapshot, transport=transport)

    assert result.sec_sync_result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert result.matching.matched_release == 1
    assert result.matching.matching_failures == 0
    link = FilingEarningsLink.objects.get()
    assert link.earnings_event_id == event.pk
    assert link.relation_type == "RELEASE_FILING"
    assert link.release_filing_classification == "YES"


@pytest.mark.django_db(transaction=True)
def test_review_required_and_no_match_keep_run_successful() -> None:
    no_match_company = make_company(cik="0000001234")
    review_company = make_company(cik="0000005678")
    make_event(
        company=review_company,
        period_type=None,
        estimated_release_date=date(2026, 3, 9),
        estimated_release_precision="date_only",
    )
    source = make_sec_source()
    snapshot = make_pool(companies=(no_match_company, review_company))
    transport = SecFilingTransport(
        {
            "0000001234": FilingSpec(form="10-Q", period_of_report="2026-03-31"),
            "0000005678": FilingSpec(form="8-K"),
        }
    )

    result = _run(source=source, snapshot=snapshot, transport=transport)

    run = result.sec_sync_result.sync_run
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.failed_count == 0
    assert result.matching.review_required == 1
    assert result.matching.no_match == 1
    assert result.matching.matching_failures == 0
    assert FilingEarningsLink.objects.count() == 0
    assert FilingEarningsDecision.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_matching_failure_marks_run_partial_and_replay_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = make_company(cik="0000001234")
    second = make_company(cik="0000005678")
    make_event(company=first, period_end_date=date(2026, 3, 31), period_type="Q1")
    make_event(company=second, period_end_date=date(2026, 3, 31), period_type="Q1")
    source = make_sec_source()
    snapshot = make_pool(companies=(first, second))
    transport = SecFilingTransport(FilingSpec(form="10-Q", period_of_report="2026-03-31"))

    calls = 0
    real = evaluate_filing_earnings_link

    def flaky(*, filing: Filing, sync_run: SyncRun) -> FilingEarningsEvaluationResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fixture matching failure")
        return real(filing=filing, sync_run=sync_run)

    monkeypatch.setattr(sec_filing_sync_module, "evaluate_filing_earnings_link", flaky)

    result = _run(source=source, snapshot=snapshot, transport=transport)

    run = result.sec_sync_result.sync_run
    assert run.status == SyncRun.Status.PARTIAL
    assert run.failed_count == 1
    assert "SecFilingMatchingError" in run.error_summary
    assert result.matching.matching_failures == 1
    assert result.matching.filings_evaluated == 1
    assert Filing.objects.count() == 2
    assert FilingEarningsDecision.objects.count() == 1
    assert FilingEarningsLink.objects.count() == 1

    monkeypatch.undo()
    replay = replay_filing_earnings_matching(source=source, sync_run_id=run.pk)

    assert replay.matching.filings_evaluated == 2
    assert replay.matching.matching_failures == 0
    assert FilingEarningsDecision.objects.count() == 2
    assert FilingEarningsLink.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_manual_authority_is_preserved_in_replay() -> None:
    company = make_company(cik="0000001234")
    event = make_event(company=company, period_end_date=date(2026, 3, 31), period_type="Q1")
    source = make_sec_source()
    snapshot = make_pool(companies=(company,))
    transport = SecFilingTransport(FilingSpec(form="10-Q", period_of_report="2026-03-31"))
    live = _run(source=source, snapshot=snapshot, transport=transport)
    filing = Filing.objects.get()
    actor = make_user("i2-manual")
    confirmation = confirm_filing_earnings_link(
        filing=filing,
        relation_type="PERIODIC_FILING",
        target_event=event,
        actor_user=actor,
        reason="Confirmed.",
        request_id="i2-manual-1",
    )
    decision_count = FilingEarningsDecision.objects.count()

    replay = replay_filing_earnings_matching(
        source=source,
        sync_run_id=live.sec_sync_result.sync_run.pk,
    )

    assert replay.matching.manual_authority == 1
    assert replay.matching.matching_failures == 0
    assert FilingEarningsDecision.objects.count() == decision_count
    link = FilingEarningsLink.objects.get()
    assert link.current_decision_id == confirmation.decision.pk


@pytest.mark.django_db(transaction=True)
def test_replay_enumerates_filings_in_stable_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = make_company(cik="0000001234")
    newer = make_company(cik="0000005678")
    make_event(company=older, period_end_date=date(2026, 3, 31), period_type="Q1")
    make_event(company=newer, period_end_date=date(2026, 3, 31), period_type="Q1")
    source = make_sec_source()
    snapshot = make_pool(companies=(older, newer))
    transport = SecFilingTransport(
        {
            "0000001234": FilingSpec(
                form="10-Q",
                period_of_report="2026-03-31",
                acceptance_datetime="2026-03-08T16:30:00",
            ),
            "0000005678": FilingSpec(
                form="10-Q",
                period_of_report="2026-03-31",
                acceptance_datetime="2026-03-10T16:30:00",
            ),
        }
    )
    live = _run(source=source, snapshot=snapshot, transport=transport)
    run_id = live.sec_sync_result.sync_run.pk
    seen: list[uuid.UUID] = []
    real = evaluate_filing_earnings_link

    def record(*, filing: Filing, sync_run: SyncRun) -> FilingEarningsEvaluationResult:
        seen.append(filing.pk)
        return real(filing=filing, sync_run=sync_run)

    monkeypatch.setattr(sec_filing_sync_module, "evaluate_filing_earnings_link", record)

    replay_filing_earnings_matching(source=source, sync_run_id=run_id)

    assert seen == [
        Filing.objects.get(company=older).pk,
        Filing.objects.get(company=newer).pk,
    ]


@pytest.mark.django_db(transaction=True)
def test_replay_is_idempotent_and_network_free() -> None:
    company = make_company(cik="0000001234")
    make_event(company=company, period_end_date=date(2026, 3, 31), period_type="Q1")
    source = make_sec_source()
    snapshot = make_pool(companies=(company,))
    transport = SecFilingTransport(FilingSpec(form="10-Q", period_of_report="2026-03-31"))
    live = _run(source=source, snapshot=snapshot, transport=transport)
    run_id = live.sec_sync_result.sync_run.pk
    request_count = len(transport.requests)
    durable_counts = (
        FilingEarningsDecision.objects.count(),
        FilingEarningsLink.objects.count(),
        AuditRecord.objects.count(),
        DataChange.objects.count(),
        SyncRun.objects.count(),
    )

    first = replay_filing_earnings_matching(source=source, sync_run_id=run_id)
    second = replay_filing_earnings_matching(source=source, sync_run_id=run_id)

    assert first.matching.filings_evaluated == 1
    assert second.matching.filings_evaluated == 1
    assert first.matching.matching_failures == 0
    assert second.matching.matching_failures == 0
    assert len(transport.requests) == request_count
    assert (
        FilingEarningsDecision.objects.count(),
        FilingEarningsLink.objects.count(),
        AuditRecord.objects.count(),
        DataChange.objects.count(),
        SyncRun.objects.count(),
    ) == durable_counts


@pytest.mark.django_db(transaction=True)
def test_replay_rejects_missing_run_wrong_source_and_wrong_job_type() -> None:
    company = make_company(cik="0000001234")
    source = make_sec_source()
    snapshot = make_pool(companies=(company,))
    transport = SecFilingTransport(FilingSpec(form="10-Q", period_of_report="2026-03-31"))
    live = _run(source=source, snapshot=snapshot, transport=transport)
    run_id = live.sec_sync_result.sync_run.pk

    with pytest.raises(SecFilingReplayError, match="does not exist"):
        replay_filing_earnings_matching(source=source, sync_run_id=uuid.uuid4())

    other_source = make_sec_source()
    with pytest.raises(SecFilingReplayError, match="different SEC DataSource"):
        replay_filing_earnings_matching(source=other_source, sync_run_id=run_id)

    wrong_job = SyncRun.objects.create(
        job_type="fixture.not-sec",
        source=source,
        idempotency_key="wrong-job",
    )
    with pytest.raises(SecFilingReplayError, match="not a SEC filing run"):
        replay_filing_earnings_matching(source=source, sync_run_id=wrong_job.pk)


@pytest.mark.django_db(transaction=True)
def test_replay_fails_closed_on_missing_provenance_target() -> None:
    source = make_sec_source()
    started = start_sync_run_with_result(
        job_type=SEC_JOB_TYPE,
        source=source,
        scope={"fixture": "dirty"},
        idempotency_key="dirty-run",
        parser_version=PARSER_VERSION,
        provider_version="sec-submissions-v1",
    )
    ingested = record_raw_data_observation(
        sync_run=started.sync_run,
        source_url="https://data.sec.gov/fixture/dirty.json",
        payload=b"{}",
        fetched_at=timezone.now(),
        observed_at=timezone.now(),
        http_status=200,
        content_type="application/json",
    )
    record_source_evidence(
        raw_data_record=ingested.record,
        sync_run=started.sync_run,
        target_type=DomainTargetType.FILING,
        target_id=uuid.uuid4(),
        field_name="",
        raw_value={"fixture": True},
        normalized_value={"fixture": True},
        confidence="1",
        normalizer_version="fixture-v1",
    )

    with pytest.raises(SecFilingReplayError, match="missing Filing targets"):
        replay_filing_earnings_matching(source=source, sync_run_id=started.sync_run.pk)


@pytest.mark.django_db(transaction=True)
def test_replay_with_no_targets_is_clean_zero_work() -> None:
    source = make_sec_source()
    started = start_sync_run_with_result(
        job_type=SEC_JOB_TYPE,
        source=source,
        scope={"fixture": "empty"},
        idempotency_key="empty-run",
        parser_version=PARSER_VERSION,
        provider_version="sec-submissions-v1",
    )

    result = replay_filing_earnings_matching(source=source, sync_run_id=started.sync_run.pk)

    assert result.matching.filings_evaluated == 0
    assert result.matching.matching_failures == 0
    assert SyncRun.objects.count() == 1
    assert MonitoringPoolSnapshot.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_successful_idempotent_live_run_does_not_match_again() -> None:
    company = make_company(cik="0000001234")
    make_event(company=company, period_end_date=date(2026, 3, 31), period_type="Q1")
    source = make_sec_source()
    snapshot = make_pool(companies=(company,))
    transport = SecFilingTransport(FilingSpec(form="10-Q", period_of_report="2026-03-31"))

    first = _run(source=source, snapshot=snapshot, transport=transport, key="same-key")
    decision_count = FilingEarningsDecision.objects.count()
    second = _run(source=source, snapshot=snapshot, transport=transport, key="same-key")

    assert first.sec_sync_result.run_created is True
    assert second.sec_sync_result.run_created is False
    assert second.matching.filings_evaluated == 0
    assert FilingEarningsDecision.objects.count() == decision_count
