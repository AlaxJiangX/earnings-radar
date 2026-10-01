"""SEC sync command contract tests for Stage 4.5A-I1 and I2."""

from __future__ import annotations

import uuid
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command, get_commands
from django.core.management.base import CommandError
from django.utils import timezone

from audit.models import AuditRecord, DataSource, SyncRun
from companies.models import Company, SecurityListing
from earnings.management.commands import sync_sec_filings as sync_sec_filings_command
from earnings.models import FilingEarningsDecision, MonitoringPoolSnapshot
from earnings.services import EARNINGS_MONITORING_POOL_SELECTOR_VERSION, select_monitoring_pool
from filings.models import Filing
from filings.sync import SEC_JOB_TYPE
from indexes.models import IndexMembership, MarketIndex
from providers.sec_edgar import SecEdgarProvider
from tests.filings.test_sec_sync import FixtureTransport

AS_OF = date(2026, 9, 30)


def _data_source(*, key: str) -> DataSource:
    return DataSource.objects.create(
        key=key,
        name="SEC official",
        source_type=DataSource.SourceType.SEC,
        base_url="https://data.sec.gov",
        is_official=True,
        provider_adapter="sec-edgar",
    )


def _setup(
    *, source_key: str = "sec-official"
) -> tuple[Company, DataSource, MonitoringPoolSnapshot]:
    company = Company.objects.create(
        legal_name="SEC Command", display_name="SEC Command", cik="0000001234"
    )
    listing = SecurityListing.objects.create(
        company=company,
        ticker=f"CMD{uuid.uuid4().hex[:4].upper()}",
        exchange="NYSE",
        security_name="SEC Command common",
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
    snapshot = select_monitoring_pool(
        as_of=AS_OF,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        enabled_index_codes=("SP500",),
    ).snapshot
    return company, _data_source(key=source_key), snapshot


def _patch_provider(monkeypatch: pytest.MonkeyPatch, transport: FixtureTransport) -> None:
    def build(**options: object) -> SecEdgarProvider:
        del options
        return SecEdgarProvider(user_agent="Earnings Radar test@example.org", transport=transport)

    monkeypatch.setattr(sync_sec_filings_command, "SecEdgarProvider", build)


def _call(**options: object) -> str:
    output = StringIO()
    call_command("sync_sec_filings", stdout=output, **options)
    return output.getvalue()


def _failed_run(*, source: DataSource, pool_hash: str) -> SyncRun:
    now = timezone.now()
    return SyncRun.objects.create(
        job_type=SEC_JOB_TYPE,
        source=source,
        scope={
            "monitoring_pool_as_of": AS_OF.isoformat(),
            "selector_version": EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
            "monitoring_pool_hash": pool_hash,
        },
        idempotency_key="fixture-failed-run",
        status=SyncRun.Status.FAILED,
        started_at=now,
        heartbeat_at=now,
        finished_at=now,
        error_summary="fixture failure",
    )


def test_command_is_discovered_from_earnings_app() -> None:
    assert get_commands()["sync_sec_filings"] == "earnings"


@pytest.mark.django_db(transaction=True)
def test_command_runs_explicit_snapshot_and_keeps_frozen_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source, snapshot = _setup()
    _patch_provider(monkeypatch, FixtureTransport())

    output = _call(source_key=source.key, snapshot_id=str(snapshot.pk))

    run = SyncRun.objects.get(job_type=SEC_JOB_TYPE)
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.scope == {
        "monitoring_pool_as_of": AS_OF.isoformat(),
        "selector_version": EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        "monitoring_pool_hash": snapshot.pool_hash,
    }
    assert Filing.objects.count() == 1
    assert f"SEC run {run.pk}: succeeded" in output
    assert (
        "matching evaluated=1; release=0; periodic=0; review=0; no_match=1; manual=0; failures=0"
    ) in output


@pytest.mark.django_db(transaction=True)
def test_command_default_as_of_selects_monitoring_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, _ = _setup()
    _patch_provider(monkeypatch, FixtureTransport())

    output = _call(source_key=source.key, as_of=AS_OF)

    run = SyncRun.objects.get(job_type=SEC_JOB_TYPE)
    snapshot = MonitoringPoolSnapshot.objects.get(as_of_date=AS_OF)
    assert run.status == SyncRun.Status.SUCCEEDED
    assert run.scope["monitoring_pool_hash"] == snapshot.pool_hash
    assert f"SEC run {run.pk}: succeeded" in output


@pytest.mark.django_db(transaction=True)
def test_command_retry_run_reuses_frozen_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, snapshot = _setup()
    _patch_provider(monkeypatch, FixtureTransport(broken_index=True))

    with pytest.raises(CommandError, match="ended partial"):
        _call(source_key=source.key, snapshot_id=str(snapshot.pk))
    original = SyncRun.objects.get(job_type=SEC_JOB_TYPE)
    assert original.status == SyncRun.Status.PARTIAL

    _patch_provider(monkeypatch, FixtureTransport())
    output = _call(source_key=source.key, retry_run=str(original.pk))

    retry = SyncRun.objects.exclude(pk=original.pk).get(job_type=SEC_JOB_TYPE)
    assert retry.status == SyncRun.Status.SUCCEEDED
    assert retry.scope == original.scope
    assert f"SEC run {retry.pk}: succeeded" in output


@pytest.mark.django_db(transaction=True)
def test_command_idempotency_key_reuses_succeeded_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source, snapshot = _setup()
    transport = FixtureTransport()
    _patch_provider(monkeypatch, transport)

    first_output = _call(
        source_key=source.key, snapshot_id=str(snapshot.pk), idempotency_key="fixed-key"
    )
    second_output = _call(
        source_key=source.key, snapshot_id=str(snapshot.pk), idempotency_key="fixed-key"
    )

    run = SyncRun.objects.get(job_type=SEC_JOB_TYPE)
    assert SyncRun.objects.filter(job_type=SEC_JOB_TYPE).count() == 1
    assert len(transport.requests) == 2
    assert f"SEC run {run.pk}" in first_output
    assert f"SEC run {run.pk}" in second_output
    assert "matching evaluated=1" in first_output
    assert "matching evaluated=0" in second_output
    assert FilingEarningsDecision.objects.count() == 1


@pytest.mark.django_db
def test_command_rejects_unknown_source() -> None:
    with pytest.raises(CommandError, match="DataSource does not exist"):
        _call(source_key="missing-source")


@pytest.mark.django_db(transaction=True)
def test_command_rejects_retry_run_from_another_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source, snapshot = _setup()
    _patch_provider(monkeypatch, FixtureTransport())
    other_source = _data_source(key="sec-other")
    failed = _failed_run(source=other_source, pool_hash=snapshot.pool_hash)

    with pytest.raises(CommandError, match="different SEC DataSource"):
        _call(source_key=source.key, retry_run=str(failed.pk))


@pytest.mark.django_db(transaction=True)
def test_command_rejects_succeeded_retry_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, snapshot = _setup()
    _patch_provider(monkeypatch, FixtureTransport())
    now = timezone.now()
    succeeded = SyncRun.objects.create(
        job_type=SEC_JOB_TYPE,
        source=source,
        scope={
            "monitoring_pool_as_of": AS_OF.isoformat(),
            "selector_version": EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
            "monitoring_pool_hash": snapshot.pool_hash,
        },
        idempotency_key="fixture-succeeded-run",
        status=SyncRun.Status.SUCCEEDED,
        started_at=now,
        heartbeat_at=now,
        finished_at=now,
    )

    with pytest.raises(CommandError, match="failed or partial"):
        _call(source_key=source.key, retry_run=str(succeeded.pk))


@pytest.mark.django_db(transaction=True)
def test_command_rejects_retry_and_snapshot_together(monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, snapshot = _setup()
    _patch_provider(monkeypatch, FixtureTransport())
    failed = _failed_run(source=source, pool_hash=snapshot.pool_hash)

    with pytest.raises(CommandError, match="not both"):
        _call(
            source_key=source.key,
            retry_run=str(failed.pk),
            snapshot_id=str(snapshot.pk),
        )


@pytest.mark.django_db(transaction=True)
def test_command_match_only_replays_without_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, snapshot = _setup()
    _patch_provider(monkeypatch, FixtureTransport())
    _call(source_key=source.key, snapshot_id=str(snapshot.pk))
    run = SyncRun.objects.get(job_type=SEC_JOB_TYPE)
    decision_count = FilingEarningsDecision.objects.count()
    audit_count = AuditRecord.objects.count()

    def fail_provider(**options: object) -> SecEdgarProvider:
        del options
        raise AssertionError("match-only must not construct the SEC provider")

    monkeypatch.setattr(sync_sec_filings_command, "SecEdgarProvider", fail_provider)

    output = _call(source_key=source.key, match_only=True, sync_run=str(run.pk))

    assert "filing matching replay" in output
    assert "evaluated=1" in output
    assert "failures=0" in output
    assert SyncRun.objects.filter(job_type=SEC_JOB_TYPE).count() == 1
    assert FilingEarningsDecision.objects.count() == decision_count
    assert AuditRecord.objects.count() == audit_count


@pytest.mark.django_db(transaction=True)
def test_command_match_only_requires_sync_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, _ = _setup()
    _patch_provider(monkeypatch, FixtureTransport())

    with pytest.raises(CommandError, match="requires --sync-run"):
        _call(source_key=source.key, match_only=True)


@pytest.mark.django_db(transaction=True)
def test_command_match_only_rejects_live_scope_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, snapshot = _setup()
    _patch_provider(monkeypatch, FixtureTransport())

    with pytest.raises(CommandError, match="accepts only"):
        _call(
            source_key=source.key,
            match_only=True,
            sync_run=str(uuid.uuid4()),
            snapshot_id=str(snapshot.pk),
        )


@pytest.mark.django_db(transaction=True)
def test_command_sync_run_requires_match_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _, source, _ = _setup()
    _patch_provider(monkeypatch, FixtureTransport())

    with pytest.raises(CommandError, match="requires --match-only"):
        _call(source_key=source.key, sync_run=str(uuid.uuid4()))
