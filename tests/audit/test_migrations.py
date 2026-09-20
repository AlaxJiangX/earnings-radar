import hashlib
import re
import uuid
from decimal import Decimal
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

MIGRATE_FROM = ("audit", "0003_source_evidence")
MIGRATE_TO = ("audit", "0004_rekey_source_evidence_by_raw_record")
MIGRATE_AUDIT_0007 = ("audit", "0007_raw_data_parse_attempt")
MIGRATE_AUDIT_0008 = (
    "audit",
    "0008_extend_audit_targets_for_reconciliation_decision",
)


@pytest.mark.django_db(transaction=True)
def test_0004_rekeys_nonempty_source_evidence_without_changing_history() -> None:
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()

    try:
        executor.migrate([MIGRATE_FROM])
        old_apps = executor.loader.project_state([MIGRATE_FROM]).apps
        evidence_ids = _create_pre_0004_history(old_apps)
        EvidenceBefore = old_apps.get_model("audit", "SourceEvidence")
        before_count = EvidenceBefore.objects.count()
        before = {
            str(row["id"]): row
            for row in EvidenceBefore.objects.filter(id__in=evidence_ids).values(
                "id",
                "raw_data_record_id",
                "raw_data_record__source_id",
                "sync_run_id",
                "sync_run__source_id",
                "target_type",
                "target_id",
                "field_name",
                "raw_value",
                "normalized_value",
                "normalizer_version",
                "evidence_key",
            )
        }

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_TO])
        new_apps = executor.loader.project_state([MIGRATE_TO]).apps
        EvidenceAfter = new_apps.get_model("audit", "SourceEvidence")
        after = {
            str(row["id"]): row
            for row in EvidenceAfter.objects.filter(id__in=evidence_ids).values(
                "id",
                "raw_data_record_id",
                "raw_data_record__source_id",
                "sync_run_id",
                "sync_run__source_id",
                "target_type",
                "target_id",
                "field_name",
                "raw_value",
                "normalized_value",
                "normalizer_version",
                "evidence_key",
            )
        }

        assert EvidenceAfter.objects.count() == before_count
        assert EvidenceAfter.objects.filter(id__in=evidence_ids).count() == 2
        assert set(after) == set(before)
        for evidence_id, prior in before.items():
            current = after[evidence_id]
            for field_name in (
                "id",
                "raw_data_record_id",
                "raw_data_record__source_id",
                "sync_run_id",
                "sync_run__source_id",
                "target_type",
                "target_id",
                "field_name",
                "raw_value",
                "normalized_value",
                "normalizer_version",
            ):
                assert current[field_name] == prior[field_name]
            assert current["evidence_key"] != prior["evidence_key"]
            assert re.fullmatch(r"[0-9a-f]{64}", current["evidence_key"])

        assert len({row["evidence_key"] for row in after.values()}) == 2
        assert {row["normalized_value"] for row in after.values()} == {"NVDA"}
    finally:
        MigrationExecutor(connection).migrate(latest_targets)


def _create_pre_0004_history(apps: Any) -> list[uuid.UUID]:
    DataSource = apps.get_model("audit", "DataSource")
    SyncRun = apps.get_model("audit", "SyncRun")
    RawDataRecord = apps.get_model("audit", "RawDataRecord")
    RawDataObservation = apps.get_model("audit", "RawDataObservation")
    SourceEvidence = apps.get_model("audit", "SourceEvidence")
    observed_at = timezone.now()
    target_id = uuid.uuid4()
    evidence_ids: list[uuid.UUID] = []

    for index, raw_value in enumerate(("nvda", "NVDA"), start=1):
        source = DataSource.objects.create(
            key=f"migration-source-{index}",
            name=f"Migration source {index}",
            source_type="manual",
            base_url=f"https://source-{index}.example.test/",
            is_official=index == 1,
        )
        sync_run = SyncRun.objects.create(
            job_type="migration.fixture",
            source=source,
            scope={"fixture": index},
            idempotency_key=f"migration.fixture:{index}",
            started_at=observed_at,
            heartbeat_at=observed_at,
        )
        payload = f'{{"ticker":"{raw_value}"}}'.encode()
        raw_record = RawDataRecord.objects.create(
            source=source,
            first_sync_run=sync_run,
            source_url=f"https://source-{index}.example.test/company",
            request_fingerprint=hashlib.sha256(f"request-{index}".encode()).hexdigest(),
            fetched_at=observed_at,
            http_status=200,
            content_type="application/json",
            encoding="utf-8",
            content_hash=hashlib.sha256(payload).hexdigest(),
            payload=payload,
            payload_size_bytes=len(payload),
        )
        RawDataObservation.objects.create(
            sync_run=sync_run,
            raw_data_record=raw_record,
            observed_at=observed_at,
        )
        evidence = SourceEvidence.objects.create(
            raw_data_record=raw_record,
            sync_run=sync_run,
            target_type="company",
            target_id=target_id,
            field_name="ticker",
            raw_value=raw_value,
            normalized_value="NVDA",
            is_official=source.is_official,
            confidence=Decimal("0.9000"),
            observed_at=observed_at,
            normalizer_version="ticker-normalizer-v1",
            evidence_key=hashlib.sha256(f"old-evidence-{index}".encode()).hexdigest(),
        )
        evidence_ids.append(evidence.pk)

    return evidence_ids


@pytest.mark.django_db(transaction=True)
def test_0008_extends_audit_record_targets_and_reverses_safely() -> None:
    executor = MigrationExecutor(connection)
    latest_targets = executor.loader.graph.leaf_nodes()
    observed_at = timezone.now()

    try:
        executor.migrate([MIGRATE_AUDIT_0007])
        old_apps = executor.loader.project_state([MIGRATE_AUDIT_0007]).apps
        DataSource = old_apps.get_model("audit", "DataSource")
        SyncRun = old_apps.get_model("audit", "SyncRun")
        AuditRecord = old_apps.get_model("audit", "AuditRecord")

        source = DataSource.objects.create(
            key="migration-audit-0008",
            name="Migration audit 0008 source",
            source_type="manual",
            base_url="https://migration-audit.example.test/",
        )
        sync_run = SyncRun.objects.create(
            job_type="migration.audit-0008",
            source=source,
            scope={"fixture": "audit-0008"},
            idempotency_key="migration.audit-0008",
            started_at=observed_at,
            heartbeat_at=observed_at,
        )
        existing = AuditRecord.objects.create(
            sync_run=sync_run,
            action="create",
            target_type="earnings_event",
            target_id=uuid.uuid4(),
            before={},
            after={},
            reason="",
            request_id="migration-audit-0008-existing",
            audit_key=hashlib.sha256(b"audit-0008-existing").hexdigest(),
        )

        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_AUDIT_0008])
        new_apps = executor.loader.project_state([MIGRATE_AUDIT_0008]).apps
        NewAuditRecord = new_apps.get_model("audit", "AuditRecord")
        NewSyncRun = new_apps.get_model("audit", "SyncRun")
        current_sync_run = NewSyncRun.objects.get(pk=sync_run.pk)

        preserved = NewAuditRecord.objects.get(pk=existing.pk)
        assert preserved.target_type == "earnings_event"
        new_target = NewAuditRecord.objects.create(
            sync_run=current_sync_run,
            action="create",
            target_type="earnings_reconciliation_decision",
            target_id=uuid.uuid4(),
            before={},
            after={},
            reason="",
            request_id="migration-audit-0008-new",
            audit_key=hashlib.sha256(b"audit-0008-new").hexdigest(),
        )
        assert new_target.target_type == "earnings_reconciliation_decision"

        NewAuditRecord.objects.filter(pk=new_target.pk).delete()
        executor = MigrationExecutor(connection)
        executor.migrate([MIGRATE_AUDIT_0007])
        reverted_apps = executor.loader.project_state([MIGRATE_AUDIT_0007]).apps
        RevertedAuditRecord = reverted_apps.get_model("audit", "AuditRecord")
        RevertedSyncRun = reverted_apps.get_model("audit", "SyncRun")
        reverted_sync_run = RevertedSyncRun.objects.get(pk=sync_run.pk)
        assert RevertedAuditRecord.objects.filter(pk=existing.pk).exists()
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                RevertedAuditRecord.objects.create(
                    sync_run=reverted_sync_run,
                    action="create",
                    target_type="earnings_reconciliation_decision",
                    target_id=uuid.uuid4(),
                    before={},
                    after={},
                    reason="",
                    request_id="migration-audit-0008-reverted",
                    audit_key=hashlib.sha256(b"audit-0008-reverted").hexdigest(),
                )
    finally:
        MigrationExecutor(connection).migrate(latest_targets)
