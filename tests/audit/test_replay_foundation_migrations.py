from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

MIGRATE_FROM = ("audit", "0008_extend_audit_targets_for_reconciliation_decision")
MIGRATE_TO = ("audit", "0009_syncrun_offline_replay_foundation")


@pytest.mark.django_db(transaction=True)
def test_replay_foundation_migration_preserves_historical_ingestion_rows() -> None:
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()
    observed_at = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

    try:
        executor.migrate([MIGRATE_FROM])
        old_apps = executor.loader.project_state([MIGRATE_FROM]).apps
        DataSource = old_apps.get_model("audit", "DataSource")
        SyncRun = old_apps.get_model("audit", "SyncRun")
        source = DataSource.objects.create(
            key="migration-replay-foundation",
            name="Replay foundation migration source",
            source_type="manual",
            base_url="https://migration-replay.example.test/",
        )
        sync_run = SyncRun.objects.create(
            job_type="migration.fixture",
            source=source,
            scope={"fixture": "historical"},
            idempotency_key="migration.replay-foundation",
            started_at=observed_at,
            heartbeat_at=observed_at,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        new_apps = executor.loader.project_state([MIGRATE_TO]).apps
        NewSyncRun = new_apps.get_model("audit", "SyncRun")
        migrated = NewSyncRun.objects.get(pk=sync_run.pk)
        assert migrated.run_mode == "ingestion"
        assert migrated.replay_source_sync_run_id is None
        assert migrated.replay_contract_version == ""
        assert migrated.replay_input_digest == ""
        assert migrated.replayed_count == 0
        assert migrated.fetched_count == 0
        assert migrated.scope == {"fixture": "historical"}
        assert migrated.job_type == "migration.fixture"

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_FROM])
        reverted_apps = executor.loader.project_state([MIGRATE_FROM]).apps
        RevertedSyncRun = reverted_apps.get_model("audit", "SyncRun")
        reverted = RevertedSyncRun.objects.get(pk=sync_run.pk)
        assert reverted.scope == {"fixture": "historical"}
        assert reverted.job_type == "migration.fixture"

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        NewSyncRun = executor.loader.project_state([MIGRATE_TO]).apps.get_model("audit", "SyncRun")
        assert NewSyncRun.objects.get(pk=sync_run.pk).run_mode == "ingestion"
    finally:
        MigrationExecutor(connection).migrate(latest_targets)
