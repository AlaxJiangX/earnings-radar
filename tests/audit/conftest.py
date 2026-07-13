import pytest

from audit.models import DataSource


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
