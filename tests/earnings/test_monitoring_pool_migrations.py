from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from audit.models import DataSource, SyncRun
from earnings.models import MonitoringPoolMember, MonitoringPoolSnapshot

MIGRATE_FROM = ("earnings", "0005_earningsreconciliationdecision")
MIGRATE_TO = ("earnings", "0006_monitoringpoolsnapshot_monitoringpoolmember")


@pytest.mark.django_db(transaction=True)
def test_monitoring_pool_migration_round_trip_preserves_historical_sync_run() -> None:
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()
    observed_at = datetime(2026, 9, 24, 15, 30, tzinfo=UTC)

    try:
        executor.migrate([MIGRATE_FROM])
        source = DataSource.objects.create(
            key="monitoring-pool-migration",
            name="Monitoring pool migration source",
            source_type="manual",
            base_url="https://monitoring-pool-migration.example.test/",
        )
        sync_run = SyncRun.objects.create(
            job_type="migration.monitoring-pool",
            source=source,
            scope={"fixture": "historical"},
            idempotency_key="migration.monitoring-pool",
            started_at=observed_at,
            heartbeat_at=observed_at,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        assert MonitoringPoolSnapshot.objects.count() == 0
        assert MonitoringPoolMember.objects.count() == 0

        migrated = SyncRun.objects.get(pk=sync_run.pk)
        assert migrated.scope == {"fixture": "historical"}
        assert migrated.job_type == "migration.monitoring-pool"

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_FROM])
        tables = set(connection.introspection.table_names())
        assert MonitoringPoolSnapshot._meta.db_table not in tables
        assert MonitoringPoolMember._meta.db_table not in tables

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        assert MonitoringPoolSnapshot.objects.count() == 0
        assert SyncRun.objects.get(pk=sync_run.pk).job_type == "migration.monitoring-pool"
    finally:
        MigrationExecutor(connection).migrate(latest_targets)


@pytest.mark.django_db
def test_latest_monitoring_pool_schema_has_no_backfilled_snapshot_rows() -> None:
    from earnings.models import MonitoringPoolMember, MonitoringPoolSnapshot

    tables = set(connection.introspection.table_names())
    assert MonitoringPoolSnapshot._meta.db_table in tables
    assert MonitoringPoolMember._meta.db_table in tables
    assert MonitoringPoolSnapshot.objects.count() == 0
    assert MonitoringPoolMember.objects.count() == 0
