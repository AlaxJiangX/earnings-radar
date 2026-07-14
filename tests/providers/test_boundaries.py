import ast
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from audit.models import DataSource, RawDataObservation, RawDataRecord, SyncRun
from audit.services import record_raw_data_observation, start_sync_run
from providers.testing import FakeProvider, make_fake_provider_request

PROVIDERS_ROOT = Path(__file__).resolve().parents[2] / "providers"
FORBIDDEN_IMPORT_PREFIXES = (
    "audit.models",
    "audit.services",
    "companies",
    "earnings",
    "filings",
    "indexes",
    "notifications",
    "watchlists",
)


def test_providers_do_not_import_audit_writers_or_future_domain_apps() -> None:
    violations: list[str] = []
    for path in sorted(PROVIDERS_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
                line_number = node.lineno
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.append(node.module)
                line_number = node.lineno
            else:
                continue
            for module_name in imported_modules:
                if module_name.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{path.name}:{line_number}:{module_name}")

    assert violations == []


@pytest.mark.django_db
def test_fake_provider_performs_no_database_queries_or_audit_writes(
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    before = (
        DataSource.objects.count(),
        SyncRun.objects.count(),
        RawDataRecord.objects.count(),
        RawDataObservation.objects.count(),
    )

    with django_assert_num_queries(0):
        FakeProvider().fetch(make_fake_provider_request())

    after = (
        DataSource.objects.count(),
        SyncRun.objects.count(),
        RawDataRecord.objects.count(),
        RawDataObservation.objects.count(),
    )
    assert after == before


@pytest.mark.django_db
def test_future_orchestrator_can_persist_provider_result_through_audit_service() -> None:
    source = DataSource.objects.create(
        key="fixture-provider-source",
        name="Fixture provider source",
        source_type=DataSource.SourceType.MANUAL,
        base_url="https://provider.example.test/",
        provider_adapter="fixture-provider",
        license_notes="Artificial test fixture only.",
    )
    sync_run = start_sync_run(
        job_type="fixture.provider-contract",
        source=source,
        scope={"fixture": True},
        idempotency_key="fixture.provider-contract:2026-07-14",
    )
    request = make_fake_provider_request(api_key_in_url="fixture-query-key")
    provider_result = FakeProvider(api_key="fixture-header-key").fetch(request)

    assert RawDataRecord.objects.count() == 0
    assert RawDataObservation.objects.count() == 0

    ingest_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url=provider_result.source_url,
        payload=provider_result.raw_content,
        request_method=provider_result.request_method,
        request_identity=provider_result.request_identity,
        fetched_at=provider_result.fetched_at,
        observed_at=provider_result.fetched_at,
        http_status=provider_result.http_status,
        content_type=provider_result.content_type,
    )

    assert ingest_result.record.request_fingerprint == provider_result.request_fingerprint
    assert ingest_result.record.source_url == provider_result.source_url
    assert bytes(ingest_result.record.payload) == provider_result.raw_content
    assert ingest_result.observation.sync_run_id == sync_run.pk
