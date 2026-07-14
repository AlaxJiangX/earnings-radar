import pytest

from audit.models import DataSource, SyncRun
from audit.services import start_sync_run


@pytest.fixture
def company_sync_run(db: object) -> SyncRun:
    del db
    source = DataSource.objects.create(
        key="company-identity-fixture",
        name="Company identity fixture",
        source_type=DataSource.SourceType.MANUAL,
        base_url="https://example.test/identity",
        license_notes="Synthetic test-only source.",
    )
    return start_sync_run(
        job_type="fixture.company-identity",
        source=source,
        scope={"fixture": "company-identity"},
        idempotency_key="fixture.company-identity:initial",
    )
