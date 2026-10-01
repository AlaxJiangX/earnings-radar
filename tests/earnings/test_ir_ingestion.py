"""Raw-first IR ingestion / observation identity / replay tests (Stage 4.5B)."""

from __future__ import annotations

import http.client
from datetime import UTC, date, datetime, timedelta

import pytest

from audit.models import (
    DataSource,
    RawDataObservation,
    RawDataParseAttempt,
    RawDataRecord,
    SyncRun,
)
from companies.models import Company
from earnings.ir_parsing import FixtureInvestorRelationsParser
from earnings.models import EarningsEvent, InvestorRelationsObservation
from earnings.services import (
    InvestorRelationsSyncError,
    execute_investor_relations_replay,
    execute_investor_relations_sync,
)
from providers.fixture_ir import FixtureInvestorRelationsProvider
from providers.testing import FakeProviderScenario
from tests.earnings.helpers import make_event
from tests.earnings.ir_helpers import (
    DEFAULT_IR_SOURCE_KEY,
    IR_FETCHED_AT,
    ingest_ir,
    ir_item,
    ir_payload,
    make_ir_company,
    make_ir_source,
    make_ir_sync_run,
)

pytestmark = pytest.mark.django_db


def _setup() -> tuple[Company, DataSource, SyncRun, EarningsEvent]:
    company = make_ir_company("ingest")
    source = make_ir_source("ingest")
    event = make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
    )
    sync_run = make_ir_sync_run(company=company, source=source)
    return company, source, sync_run, event


def test_fixture_payload_creates_observation_with_internal_identity() -> None:
    company, _source, sync_run, _event = _setup()
    payload = ir_payload([ir_item(company, confirmed_release="2026-10-20")])

    result = ingest_ir(sync_run=sync_run, payload=payload, company=company)

    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.source_event_identity.startswith("internal:ir:v1:")
    assert observation.period_end_date == date(2026, 9, 30)
    assert observation.item_type == "release_confirmation"
    assert observation.confirmed_release_date == date(2026, 10, 20)
    assert observation.confirmed_release_precision == "date_only"
    assert result.parse_attempt.status == "succeeded"
    assert result.item_failures == ()


def test_provider_native_identity_is_preserved() -> None:
    company, _source, sync_run, _event = _setup()
    payload = ir_payload(
        [ir_item(company, source_event_identity="native-2026q3-1", confirmed_release="2026-10-20")]
    )

    result = ingest_ir(sync_run=sync_run, payload=payload, company=company)

    assert result.observations[0].source_event_identity == "native-2026q3-1"


def test_same_raw_and_identity_replay_is_idempotent() -> None:
    company, _source, sync_run, _event = _setup()
    payload = ir_payload([ir_item(company, confirmed_release="2026-10-20")])

    first = ingest_ir(sync_run=sync_run, payload=payload, company=company)
    second = ingest_ir(sync_run=sync_run, payload=payload, company=company)

    assert first.observations_created == 1
    assert second.observations_created == 0
    assert second.observations_reused == 1
    assert second.observations[0].pk == first.observations[0].pk
    assert InvestorRelationsObservation.objects.count() == 1
    assert RawDataRecord.objects.count() == 1
    assert RawDataObservation.objects.count() == 1


def test_new_raw_payload_creates_new_observation_revision() -> None:
    company, _source, sync_run, _event = _setup()
    first = ingest_ir(
        sync_run=sync_run,
        payload=ir_payload([ir_item(company, confirmed_release="2026-10-20")]),
        company=company,
    )
    second = ingest_ir(
        sync_run=sync_run,
        payload=ir_payload([ir_item(company, confirmed_release="2026-10-21")]),
        company=company,
        fetched_at=datetime(2026, 10, 1, 13, 0, 0, tzinfo=UTC),
    )

    assert first.observations[0].pk != second.observations[0].pk
    assert (
        first.observations[0].source_event_identity == second.observations[0].source_event_identity
    )
    assert InvestorRelationsObservation.objects.count() == 2


def test_incomplete_period_identity_keeps_raw_lineage_only() -> None:
    company, _source, sync_run, _event = _setup()
    item = ir_item(company, confirmed_release="2026-10-20")
    item.pop("period_type")

    result = ingest_ir(
        sync_run=sync_run,
        payload=ir_payload([item]),
        company=company,
    )

    assert result.observations == ()
    assert len(result.item_failures) == 1
    assert result.item_failures[0].reason_code == "INCOMPLETE_PERIOD_IDENTITY"
    assert result.parse_attempt.status == "data_error"
    assert RawDataRecord.objects.count() == 1
    assert InvestorRelationsObservation.objects.count() == 0


def test_malformed_item_does_not_block_sibling_item() -> None:
    company, _source, sync_run, _event = _setup()
    malformed = ir_item(company, confirmed_release="2026-10-20")
    malformed.pop("company_id")
    valid = ir_item(company, confirmed_release="2026-10-21", source_event_identity="native-2")

    result = ingest_ir(
        sync_run=sync_run,
        payload=ir_payload([malformed, valid]),
        company=company,
    )

    assert len(result.observations) == 1
    assert result.observations[0].source_event_identity == "native-2"
    assert len(result.item_failures) == 1
    assert result.item_failures[0].reason_code == "MISSING_COMPANY"


def test_fixture_provider_and_replay_never_open_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company, source, sync_run, _event = _setup()
    payload = ir_payload([ir_item(company, confirmed_release="2026-10-20")])
    provider = FixtureInvestorRelationsProvider(
        fixtures={(DEFAULT_IR_SOURCE_KEY, str(company.pk)): payload},
        fetched_at=IR_FETCHED_AT,
    )

    def fail_if_network_is_used(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("fixture-first IR code must not open real network connections.")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", fail_if_network_is_used)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", fail_if_network_is_used)

    sync_result = execute_investor_relations_sync(
        sync_run=sync_run,
        provider=provider,
        parser=FixtureInvestorRelationsParser(),
        request_started_at=IR_FETCHED_AT - timedelta(seconds=1),
    )

    assert sync_result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert sync_result.fetched_payloads == 1
    assert sync_result.observations_created == 1
    assert sync_result.decisions_created == 1

    replay_result = execute_investor_relations_replay(
        source_run=sync_result.sync_run,
        parser=FixtureInvestorRelationsParser(),
    )
    assert replay_result.sync_run.status == SyncRun.Status.SUCCEEDED
    assert replay_result.sync_run.fetched_count == 0
    assert replay_result.replayed_payloads == 1
    assert replay_result.decisions_created == 0
    assert replay_result.sync_run.replay_input_digest != ""
    assert provider.transport is not None


def test_malformed_transport_payload_is_a_failed_parse_attempt() -> None:
    company, _source, sync_run, _event = _setup()
    provider = FixtureInvestorRelationsProvider(
        fixtures={(DEFAULT_IR_SOURCE_KEY, str(company.pk)): b"{not-json"},
        scenario=FakeProviderScenario.SUCCESS,
        fetched_at=IR_FETCHED_AT,
    )

    with pytest.raises(InvestorRelationsSyncError):
        execute_investor_relations_sync(
            sync_run=sync_run,
            provider=provider,
            parser=FixtureInvestorRelationsParser(),
            request_started_at=IR_FETCHED_AT - timedelta(seconds=1),
        )

    sync_run.refresh_from_db()
    assert sync_run.status == SyncRun.Status.FAILED
    assert sync_run.failed_count == 1
    assert RawDataRecord.objects.count() == 1
    assert RawDataRecord.objects.get().parser_status == "failed"
    assert RawDataParseAttempt.objects.get().status == "data_error"
    assert InvestorRelationsObservation.objects.count() == 0
