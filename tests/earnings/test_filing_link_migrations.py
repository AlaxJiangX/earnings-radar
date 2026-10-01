# mypy: ignore-errors
"""Migration behavior for filings.reported_items and the 4.5A audit target."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

MIGRATE_FROM = ("filings", "0001_initial")
MIGRATE_TO = ("filings", "0002_filing_reported_items_and_more")


@pytest.mark.django_db(transaction=True)
def test_reported_items_migration_preserves_history_and_enforces_canonical_form() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATE_FROM])
    old_apps = executor.loader.project_state([MIGRATE_FROM]).apps
    Company = old_apps.get_model("companies", "Company")
    Filing = old_apps.get_model("filings", "Filing")
    company = Company.objects.create(
        legal_name="Migration Co",
        display_name="Migration Co",
    )
    historical = Filing.objects.create(
        company_id=company.pk,
        accession_number="0000001234-26-000101",
        form_type="8-K",
        accepted_at=datetime(2026, 10, 1, tzinfo=UTC),
        primary_document="main.htm",
        filing_url=("https://www.sec.gov/Archives/edgar/data/1234/000000123426000101/main.htm"),
    )

    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATE_TO])
    new_apps = executor.loader.project_state([MIGRATE_TO]).apps
    NewFiling = new_apps.get_model("filings", "Filing")
    migrated = NewFiling.objects.get(pk=historical.pk)
    assert migrated.reported_items == ""

    with pytest.raises(IntegrityError), transaction.atomic():
        NewFiling.objects.create(
            company_id=company.pk,
            accession_number="0000001234-26-000102",
            form_type="8-K",
            accepted_at=datetime(2026, 10, 1, tzinfo=UTC),
            primary_document="main.htm",
            filing_url=("https://www.sec.gov/Archives/edgar/data/1234/000000123426000102/main.htm"),
            reported_items="2.02,bad",
        )
    canonical = NewFiling.objects.create(
        company_id=company.pk,
        accession_number="0000001234-26-000103",
        form_type="8-K",
        accepted_at=datetime(2026, 10, 1, tzinfo=UTC),
        primary_document="main.htm",
        filing_url=("https://www.sec.gov/Archives/edgar/data/1234/000000123426000103/main.htm"),
        reported_items="2.02,9.01",
    )
    assert canonical.reported_items == "2.02,9.01"

    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATE_FROM])
    reverted_apps = executor.loader.project_state([MIGRATE_FROM]).apps
    RevertedFiling = reverted_apps.get_model("filings", "Filing")
    assert "reported_items" not in {field.name for field in RevertedFiling._meta.fields}
    assert RevertedFiling.objects.filter(pk=historical.pk).exists()

    executor = MigrationExecutor(connection)
    executor.migrate([MIGRATE_TO])
    restored_apps = executor.loader.project_state([MIGRATE_TO]).apps
    RestoredFiling = restored_apps.get_model("filings", "Filing")
    restored = RestoredFiling.objects.get(pk=historical.pk)
    assert restored.reported_items == ""
