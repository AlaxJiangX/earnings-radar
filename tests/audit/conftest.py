import pytest

from audit.models import DataSource, SyncRun
from audit.services import start_sync_run


@pytest.fixture
def data_source(db: object) -> DataSource:
    del db
    return DataSource.objects.create(
        key="fixture-source",
        name="Fixture source",
        source_type=DataSource.SourceType.MANUAL,
        base_url="https://example.test/",
        is_official=False,
        provider_adapter="",
        license_notes="Test fixture only; no external access.",
    )


@pytest.fixture
def sync_run(data_source: DataSource) -> SyncRun:
    return start_sync_run(
        job_type="fixture.raw-data",
        source=data_source,
        scope={"fixture": True},
        idempotency_key="fixture.raw-data:initial",
        code_version="test-code",
        parser_version="test-parser",
    )
