"""IR frozen scope / SyncRun identity / zero-network replay / concurrency tests."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier

import pytest
from django.db import close_old_connections, connections

from audit.models import RawDataObservation, SyncRun
from earnings.ir_parsing import FixtureInvestorRelationsParser
from earnings.models import InvestorRelationsDecision, InvestorRelationsObservation
from earnings.services import (
    InvestorRelationsDecisionWriteResult,
    InvestorRelationsEvaluationResult,
    InvestorRelationsObservationWriteResult,
    InvestorRelationsRunBusy,
    InvestorRelationsSyncError,
    build_investor_relations_sync_scope,
    evaluate_investor_relations_observation,
    execute_investor_relations_replay,
    execute_investor_relations_sync,
    record_investor_relations_decision,
    record_investor_relations_observation,
    resolve_investor_relations_observation_manually,
    start_investor_relations_sync_run,
)
from providers.fixture_ir import FixtureInvestorRelationsProvider
from tests.earnings.helpers import make_event, make_user
from tests.earnings.ir_helpers import (
    DEFAULT_IR_SOURCE_KEY,
    DEFAULT_IR_SOURCE_URL_BASE,
    IR_FETCHED_AT,
    ingest_ir,
    ir_item,
    ir_payload,
    make_ir_company,
    make_ir_source,
    make_ir_sync_run,
)


def test_frozen_scope_is_deterministic_and_digest_covers_stable_facts() -> None:
    first = build_investor_relations_sync_scope(
        provider_key="fixture-ir",
        company_ids=[
            "00000000-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000000001",
        ],
        source_keys=["b-source", "a-source"],
    )
    second = build_investor_relations_sync_scope(
        provider_key="fixture-ir",
        company_ids=[
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        ],
        source_keys=["a-source", "b-source"],
    )

    assert first["company_ids"] == second["company_ids"]
    assert first["source_keys"] == second["source_keys"]
    assert first["scope_digest"] == second["scope_digest"]
    assert first["scope_version"] == "ir-confirmation-scope-v1"


@pytest.mark.django_db
def test_scope_requires_explicit_company_with_ir_url() -> None:
    company = make_ir_company("no-url")
    company.investor_relations_url = ""
    company.save(update_fields=("investor_relations_url",))
    source = make_ir_source("no-url")

    with pytest.raises(InvestorRelationsSyncError):
        start_investor_relations_sync_run(
            source=source,
            provider_key="fixture-ir",
            company_ids=[company.pk],
            source_keys=[DEFAULT_IR_SOURCE_KEY],
            request_id="ir-missing-url",
            provider_version="fixture-v1",
        )


@pytest.mark.django_db
def test_start_run_reports_busy_then_idempotent_after_finalization() -> None:
    company = make_ir_company("start")
    source = make_ir_source("start")
    first = start_investor_relations_sync_run(
        source=source,
        provider_key="fixture-ir",
        company_ids=[company.pk],
        source_keys=[DEFAULT_IR_SOURCE_KEY],
        request_id="ir-start-1",
        provider_version="fixture-v1",
        parser_version=FixtureInvestorRelationsParser.parser_version,
    )
    assert first.created is True

    with pytest.raises(InvestorRelationsRunBusy):
        start_investor_relations_sync_run(
            source=source,
            provider_key="fixture-ir",
            company_ids=[company.pk],
            source_keys=[DEFAULT_IR_SOURCE_KEY],
            request_id="ir-start-1",
            provider_version="fixture-v1",
            parser_version=FixtureInvestorRelationsParser.parser_version,
        )

    from audit.services import mark_sync_run_succeeded

    mark_sync_run_succeeded(first.sync_run.pk)
    second = start_investor_relations_sync_run(
        source=source,
        provider_key="fixture-ir",
        company_ids=[company.pk],
        source_keys=[DEFAULT_IR_SOURCE_KEY],
        request_id="ir-start-1",
        provider_version="fixture-v1",
        parser_version=FixtureInvestorRelationsParser.parser_version,
    )
    assert second.created is False
    assert second.sync_run.pk == first.sync_run.pk


@pytest.mark.django_db
def test_replay_uses_persisted_scope_not_current_ir_url() -> None:
    company = make_ir_company("replay-scope")
    source = make_ir_source("replay-scope")
    event = make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
    )
    run = make_ir_sync_run(company=company, source=source, request_id="ir-replay-scope")
    provider = FixtureInvestorRelationsProvider(
        fixtures={
            (DEFAULT_IR_SOURCE_KEY, str(company.pk)): ir_payload(
                [ir_item(company, confirmed_release="2026-10-20")]
            )
        },
        fetched_at=IR_FETCHED_AT,
    )
    sync_result = execute_investor_relations_sync(
        sync_run=run,
        provider=provider,
        parser=FixtureInvestorRelationsParser(),
        request_started_at=IR_FETCHED_AT - timedelta(seconds=1),
    )
    assert sync_result.sync_run.status == SyncRun.Status.SUCCEEDED

    company.investor_relations_url = f"{DEFAULT_IR_SOURCE_URL_BASE}/changed/{company.pk}"
    company.save(update_fields=("investor_relations_url",))

    replay = execute_investor_relations_replay(
        source_run=sync_result.sync_run,
        parser=FixtureInvestorRelationsParser(),
    )

    assert replay.sync_run.status == SyncRun.Status.SUCCEEDED
    assert replay.sync_run.scope["company_ids"] == [str(company.pk)]
    assert replay.sync_run.scope["window_kind"] == "replay"
    assert replay.reused_terminal_run is False
    event.refresh_from_db()
    assert event.confirmed_release_date == date(2026, 10, 20)


@pytest.mark.django_db
def test_terminal_replay_is_reused_without_new_observations() -> None:
    company = make_ir_company("replay-reuse")
    source = make_ir_source("replay-reuse")
    make_event(company=company, period_end_date=date(2026, 9, 30), period_type="Q3")
    run = make_ir_sync_run(company=company, source=source, request_id="ir-replay-reuse")
    provider = FixtureInvestorRelationsProvider(
        fixtures={
            (DEFAULT_IR_SOURCE_KEY, str(company.pk)): ir_payload(
                [ir_item(company, confirmed_release="2026-10-20")]
            )
        },
        fetched_at=IR_FETCHED_AT,
    )
    sync_result = execute_investor_relations_sync(
        sync_run=run,
        provider=provider,
        parser=FixtureInvestorRelationsParser(),
        request_started_at=IR_FETCHED_AT - timedelta(seconds=1),
    )
    first_replay = execute_investor_relations_replay(
        source_run=sync_result.sync_run,
        parser=FixtureInvestorRelationsParser(),
    )
    observation_count = RawDataObservation.objects.count()

    second_replay = execute_investor_relations_replay(
        source_run=sync_result.sync_run,
        parser=FixtureInvestorRelationsParser(),
    )

    assert second_replay.reused_terminal_run is True
    assert second_replay.sync_run.pk == first_replay.sync_run.pk
    assert RawDataObservation.objects.count() == observation_count


@pytest.mark.django_db
def test_replay_does_not_override_manual_leaf() -> None:
    company = make_ir_company("replay-manual")
    source = make_ir_source("replay-manual")
    event = make_event(company=company, period_end_date=date(2026, 9, 30), period_type="Q3")
    run = make_ir_sync_run(company=company, source=source, request_id="ir-replay-manual")
    provider = FixtureInvestorRelationsProvider(
        fixtures={
            (DEFAULT_IR_SOURCE_KEY, str(company.pk)): ir_payload(
                [ir_item(company, confirmed_release="2026-10-20")]
            )
        },
        fetched_at=IR_FETCHED_AT,
    )
    sync_result = execute_investor_relations_sync(
        sync_run=run,
        provider=provider,
        parser=FixtureInvestorRelationsParser(),
        request_started_at=IR_FETCHED_AT - timedelta(seconds=1),
    )
    observation = sync_result.ingestions[0].observations[0]
    actor = make_user("replay-manual")
    resolve_investor_relations_observation_manually(
        observation=observation,
        actor_user=actor,
        reason="Manual authority wins.",
        request_id="replay-manual-1",
        target_event=event,
    )

    replay = execute_investor_relations_replay(
        source_run=sync_result.sync_run,
        parser=FixtureInvestorRelationsParser(),
    )

    event.refresh_from_db()
    assert replay.sync_run.status in {SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL}
    assert event.confirmed_release_date == date(2026, 10, 20)
    assert InvestorRelationsDecision.objects.filter(actor_user=actor).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_evaluations_are_idempotent() -> None:
    company = make_ir_company("concurrent-eval")
    source = make_ir_source("concurrent-eval")
    event = make_event(company=company, period_end_date=date(2026, 9, 30), period_type="Q3")
    run = make_ir_sync_run(company=company, source=source, request_id="ir-concurrent-eval")
    ingestion = ingest_ir(
        sync_run=run,
        payload=ir_payload([ir_item(company, confirmed_release="2026-10-20")]),
        company=company,
    )
    observation = ingestion.observations[0]
    barrier = Barrier(2, timeout=10)

    def evaluate() -> InvestorRelationsEvaluationResult:
        close_old_connections()
        try:
            barrier.wait()
            return evaluate_investor_relations_observation(
                observation=observation,
                sync_run=run,
            )
        finally:
            for connection in connections.all():
                connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: evaluate(), range(2)))

    assert InvestorRelationsDecision.objects.count() == 1
    assert sorted(result.decision_created for result in results) == [False, True]
    event.refresh_from_db()
    assert event.status == "scheduled_confirmed"


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_observation_insert_uses_unique_constraint() -> None:
    company = make_ir_company("concurrent-observation")
    source = make_ir_source("concurrent-observation")
    run = make_ir_sync_run(
        company=company,
        source=source,
        request_id=f"ir-{uuid.uuid4().hex[:8]}",
    )
    payload = ir_payload([ir_item(company, confirmed_release="2026-10-20")])
    from audit.services import record_raw_data_observation

    ingest_result = record_raw_data_observation(
        sync_run=run,
        source_url=f"{DEFAULT_IR_SOURCE_URL_BASE}/{DEFAULT_IR_SOURCE_KEY}/{company.pk}",
        payload=payload,
        fetched_at=IR_FETCHED_AT,
        observed_at=IR_FETCHED_AT,
    )
    parsed = FixtureInvestorRelationsParser().parse(
        payload,
        provider_key="fixture-ir",
        provider_version="fixture-v1",
    )
    normalized = parsed.records[0]
    barrier = Barrier(2, timeout=10)

    def record() -> InvestorRelationsObservationWriteResult:
        close_old_connections()
        try:
            barrier.wait()
            return record_investor_relations_observation(
                source=source,
                raw_data_record=ingest_result.record,
                company=company,
                provider_key="fixture-ir",
                provider_version="fixture-v1",
                parser_version=normalized.parser_version,
                source_event_identity=normalized.source_event_identity,
                raw_position=normalized.raw_position,
                period_end_date=normalized.period_end_date,
                period_type=normalized.period_type,
                item_type=normalized.item_type,
                confirmed_release=normalized.confirmed_release.date_value,
            )
        finally:
            for connection in connections.all():
                connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: record(), range(2)))

    assert sorted(result.created for result in results) == [False, True]
    assert InvestorRelationsObservation.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_decision_insert_is_idempotent() -> None:
    company = make_ir_company("concurrent-decision")
    source = make_ir_source("concurrent-decision")
    run = make_ir_sync_run(
        company=company,
        source=source,
        request_id=f"ir-{uuid.uuid4().hex[:8]}",
    )
    ingestion = ingest_ir(
        sync_run=run,
        payload=ir_payload([ir_item(company, confirmed_release="2026-10-20")]),
        company=company,
    )
    observation = ingestion.observations[0]
    barrier = Barrier(2, timeout=10)

    def record() -> InvestorRelationsDecisionWriteResult:
        close_old_connections()
        try:
            barrier.wait()
            return record_investor_relations_decision(
                observation=observation,
                decision_type="conflict",
                status="open",
                covered_fields=("confirmed_release",),
                match_factors={"fixture": True},
                reason="FIXTURE_CONFLICT",
                sync_run=run,
            )
        finally:
            for connection in connections.all():
                connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: record(), range(2)))

    assert sorted(result.created for result in results) == [False, True]
    assert InvestorRelationsDecision.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_conflicting_event_evaluations_serialize() -> None:
    company = make_ir_company("concurrent-event")
    source = make_ir_source("concurrent-event")
    event = make_event(company=company, period_end_date=date(2026, 9, 30), period_type="Q3")
    run = make_ir_sync_run(
        company=company,
        source=source,
        request_id=f"ir-{uuid.uuid4().hex[:8]}",
    )
    first = ingest_ir(
        sync_run=run,
        payload=ir_payload(
            [ir_item(company, source_event_identity="a", confirmed_release="2026-10-20")]
        ),
        company=company,
    ).observations[0]
    second = ingest_ir(
        sync_run=run,
        payload=ir_payload(
            [ir_item(company, source_event_identity="b", confirmed_release="2026-10-21")]
        ),
        company=company,
    ).observations[0]
    barrier = Barrier(2, timeout=10)

    def evaluate(
        observation: InvestorRelationsObservation,
    ) -> InvestorRelationsEvaluationResult:
        close_old_connections()
        try:
            barrier.wait()
            return evaluate_investor_relations_observation(
                observation=observation,
                sync_run=run,
            )
        finally:
            for connection in connections.all():
                connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(evaluate, first)
        second_future = executor.submit(evaluate, second)
        results = [first_future.result(timeout=20), second_future.result(timeout=20)]

    event.refresh_from_db()
    assert event.confirmed_release_date in {date(2026, 10, 20), date(2026, 10, 21)}
    assert InvestorRelationsDecision.objects.count() == 2
    assert sum(result.decision.decision_type == "conflict" for result in results) == 1
    from audit.models import DataChange

    assert (
        DataChange.objects.filter(
            target_type="earnings_event",
            target_id=event.pk,
            field_name="confirmed_release",
        ).count()
        == 1
    )
