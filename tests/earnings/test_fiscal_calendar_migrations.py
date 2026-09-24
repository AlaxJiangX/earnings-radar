from __future__ import annotations

import uuid

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

MIGRATE_FROM = ("earnings", "0006_monitoringpoolsnapshot_monitoringpoolmember")
MIGRATE_TO = ("earnings", "0007_fiscal_calendar_unknown")


@pytest.mark.django_db(transaction=True)
def test_0007_preserves_historical_month_based_and_sets_unknown_default() -> None:
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate([MIGRATE_FROM])
        old_apps = executor.loader.project_state([MIGRATE_FROM]).apps
        Company = old_apps.get_model("companies", "Company")
        EarningsEvent = old_apps.get_model("earnings", "EarningsEvent")

        company = Company.objects.create(
            id=uuid.uuid4(),
            cik="0000097001",
            legal_name="Fiscal Calendar Migration Co",
            display_name="Fiscal Calendar Migration Co",
        )
        known_event = EarningsEvent.objects.create(
            id=uuid.uuid4(),
            company=company,
            identity_status="candidate",
            fiscal_calendar_type="month_based",
            status="scheduled_estimated",
        )

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        migrated_apps = executor.loader.project_state([MIGRATE_TO]).apps
        MigratedEvent = migrated_apps.get_model("earnings", "EarningsEvent")

        preserved = MigratedEvent.objects.get(pk=known_event.pk)
        assert preserved.fiscal_calendar_type == "month_based"

        unknown_event = MigratedEvent.objects.create(
            id=uuid.uuid4(),
            company_id=company.pk,
            identity_status="candidate",
            status="scheduled_estimated",
        )
        assert unknown_event.fiscal_calendar_type == "unknown"
    finally:
        MigrationExecutor(connection).migrate(latest_targets)
