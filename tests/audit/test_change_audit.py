import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import override_settings
from django.utils import timezone

from accounts.models import User
from audit.models import (
    AppendOnlyRecordError,
    AuditRecord,
    DataChange,
    SourceEvidence,
    SyncRun,
)
from audit.services import (
    InvalidAuditRecord,
    InvalidDataChange,
    SensitiveAuditRecordValue,
    SensitiveDataChangeValue,
    record_data_change,
    record_raw_data_observation,
    record_source_evidence,
    record_system_action,
    record_user_action,
)
from audit.services.change_audit import AUDIT_IP_HASH_CONTEXT, AUDIT_IP_HASH_VERSION


@pytest.fixture
def actor_user(db: object) -> User:
    del db
    return User.objects.create_user(
        email="audit-actor@example.com",
        password="fixture-password-only",
        is_staff=True,
    )


def _source_evidence(
    sync_run: SyncRun,
    *,
    target_id: uuid.UUID,
    field_name: str = "status",
) -> SourceEvidence:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/change",
        payload=b'{"status":"new"}',
    )
    return record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name=field_name,
        raw_value="new",
        normalized_value="new",
        confidence=1,
        normalizer_version="change-fixture-v1",
    ).evidence


def test_audit_ip_hash_version_and_context_are_explicit() -> None:
    assert AUDIT_IP_HASH_VERSION == "v1"
    assert AUDIT_IP_HASH_CONTEXT == "earnings-radar.audit.ip-hash.v1"


@pytest.mark.django_db
def test_record_data_change_records_traceable_automatic_change(sync_run: SyncRun) -> None:
    target_id = uuid.uuid4()
    evidence = _source_evidence(sync_run, target_id=target_id)

    result = record_data_change(
        target_type=DataChange.TargetType.COMPANY,
        target_id=target_id,
        field_name="status",
        old_value="old",
        new_value="new",
        source_evidence=evidence,
        sync_run=sync_run,
        rule_version="domain-reconcile-v1",
    )

    assert result.created is True
    assert result.skipped is False
    assert result.change is not None
    assert result.change.source_evidence_id == evidence.pk
    assert result.change.sync_run_id == sync_run.pk
    assert result.change.actor_user_id is None
    assert result.change.origin_key == f"source_evidence:{evidence.pk}"
    assert timezone.is_aware(result.change.changed_at)
    assert timezone.is_aware(result.change.created_at)


@pytest.mark.django_db
def test_record_data_change_records_manual_correction(actor_user: User) -> None:
    target_id = uuid.uuid4()

    result = record_data_change(
        target_type=DataChange.TargetType.COMPANY,
        target_id=target_id,
        field_name="display_name",
        old_value="Fixture Incorporated",
        new_value="Fixture Corp",
        actor_user=actor_user,
        reason="Corrected the verified legal display name.",
        origin_key="request:manual-correction-1",
        rule_version="manual-correction-v1",
    )

    assert result.created is True
    assert result.change is not None
    assert result.change.actor_user_id == actor_user.pk
    assert result.change.sync_run_id is None
    assert result.change.source_evidence_id is None
    assert result.change.reason == "Corrected the verified legal display name."


@pytest.mark.django_db
def test_record_data_change_skips_canonically_equal_values() -> None:
    result = record_data_change(
        target_type=DataChange.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        field_name="metadata",
        old_value={"ticker": "NVDA", "flags": ("active",)},
        new_value={"flags": ["active"], "ticker": "NVDA"},
        rule_version="domain-reconcile-v1",
    )

    assert result.change is None
    assert result.created is False
    assert result.skipped is True
    assert DataChange.objects.count() == 0


@pytest.mark.django_db
def test_record_data_change_is_idempotent(sync_run: SyncRun) -> None:
    target_id = uuid.uuid4()
    evidence = _source_evidence(sync_run, target_id=target_id)
    first = record_data_change(
        target_type=DataChange.TargetType.COMPANY,
        target_id=target_id,
        field_name="status",
        old_value="old",
        new_value="new",
        source_evidence=evidence,
        sync_run=sync_run,
        rule_version="domain-reconcile-v1",
    )
    second = record_data_change(
        target_type=DataChange.TargetType.COMPANY,
        target_id=target_id,
        field_name="status",
        old_value="old",
        new_value="new",
        source_evidence=evidence,
        sync_run=sync_run,
        rule_version="domain-reconcile-v1",
    )

    assert first.change is not None
    assert second.change is not None
    assert first.change.pk == second.change.pk
    assert first.created is True
    assert second.created is False
    assert DataChange.objects.count() == 1


@pytest.mark.django_db
def test_manual_data_change_requires_reason(actor_user: User) -> None:
    with pytest.raises(InvalidDataChange):
        record_data_change(
            target_type=DataChange.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="display_name",
            old_value="Old",
            new_value="New",
            actor_user=actor_user,
            reason="   ",
            origin_key="request:manual-no-reason",
            rule_version="manual-correction-v1",
        )

    assert DataChange.objects.count() == 0


@pytest.mark.django_db
def test_manual_data_change_requires_stable_origin_key(actor_user: User) -> None:
    with pytest.raises(InvalidDataChange):
        record_data_change(
            target_type=DataChange.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="display_name",
            old_value="Old",
            new_value="New",
            actor_user=actor_user,
            reason="Verified correction.",
            origin_key=" ",
            rule_version="manual-correction-v1",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("target_type", "field_name", "error_type"),
    (
        ("arbitrary", "status", InvalidDataChange),
        (DataChange.TargetType.COMPANY, "api_key", SensitiveDataChangeValue),
    ),
)
def test_data_change_service_rejects_invalid_target_or_sensitive_field(
    sync_run: SyncRun,
    target_type: str,
    field_name: str,
    error_type: type[InvalidDataChange],
) -> None:
    with pytest.raises(error_type):
        record_data_change(
            target_type=target_type,
            target_id=uuid.uuid4(),
            field_name=field_name,
            old_value="old",
            new_value="new",
            sync_run=sync_run,
            rule_version="domain-reconcile-v1",
        )


@pytest.mark.django_db
def test_automatic_data_change_requires_sync_or_evidence() -> None:
    with pytest.raises(InvalidDataChange):
        record_data_change(
            target_type=DataChange.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="status",
            old_value="old",
            new_value="new",
            rule_version="domain-reconcile-v1",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("old_value", "new_value"),
    (
        ({"token": "fixture-secret"}, "new"),
        ("old", {"Authorization": "Bearer fixture-secret"}),
        ({"nested": [{"Cookie": "fixture-secret"}]}, "new"),
        ("password=fixture-secret", "new"),
        ("https://user:fixture-secret@example.test/data", "new"),
        ("old", "https://example.test/data?api_key=fixture-secret"),
    ),
)
def test_data_change_rejects_sensitive_values_without_echoing_secret(
    sync_run: SyncRun,
    old_value: object,
    new_value: object,
) -> None:
    with pytest.raises(SensitiveDataChangeValue) as error:
        record_data_change(
            target_type=DataChange.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="status",
            old_value=old_value,
            new_value=new_value,
            sync_run=sync_run,
            rule_version="domain-reconcile-v1",
        )

    assert "fixture-secret" not in str(error.value)
    assert DataChange.objects.count() == 0


@pytest.mark.django_db
def test_data_change_key_is_lowercase_sha256(sync_run: SyncRun) -> None:
    result = record_data_change(
        target_type=DataChange.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        field_name="status",
        old_value="old",
        new_value="new",
        sync_run=sync_run,
        rule_version="domain-reconcile-v1",
    )

    assert result.change is not None
    assert re.fullmatch(r"[0-9a-f]{64}", result.change.change_key)


@pytest.mark.django_db
def test_data_change_database_enforces_unique_key(sync_run: SyncRun) -> None:
    first = record_data_change(
        target_type=DataChange.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        field_name="status",
        old_value="old",
        new_value="new",
        sync_run=sync_run,
        rule_version="domain-reconcile-v1",
    ).change
    assert first is not None

    with pytest.raises(IntegrityError), transaction.atomic():
        DataChange.objects.create(
            target_type=DataChange.TargetType.FILING,
            target_id=uuid.uuid4(),
            field_name="form_type",
            old_value="8-K",
            new_value="10-Q",
            sync_run=sync_run,
            origin_key=f"sync_run:{sync_run.pk}",
            rule_version="filing-reconcile-v1",
            change_key=first.change_key,
        )


@pytest.mark.django_db
def test_data_change_timestamps_are_aware_and_naive_input_is_rejected(
    sync_run: SyncRun,
) -> None:
    timestamp = datetime(2026, 7, 13, 8, tzinfo=UTC)
    result = record_data_change(
        target_type=DataChange.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        field_name="status",
        old_value="old",
        new_value="new",
        sync_run=sync_run,
        rule_version="domain-reconcile-v1",
        changed_at=timestamp,
    )
    assert result.change is not None
    assert result.change.changed_at == timestamp
    assert timezone.is_aware(result.change.created_at)

    with pytest.raises(InvalidDataChange):
        record_data_change(
            target_type=DataChange.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="status",
            old_value="old",
            new_value="new",
            sync_run=sync_run,
            rule_version="domain-reconcile-v1",
            changed_at=datetime(2026, 7, 13, 8),
        )


@pytest.mark.django_db
def test_data_change_requires_matching_source_evidence_target(sync_run: SyncRun) -> None:
    evidence = _source_evidence(sync_run, target_id=uuid.uuid4())

    with pytest.raises(InvalidDataChange):
        record_data_change(
            target_type=DataChange.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="status",
            old_value="old",
            new_value="new",
            source_evidence=evidence,
            sync_run=sync_run,
            rule_version="domain-reconcile-v1",
        )


@pytest.mark.django_db
def test_data_change_database_rejects_equal_values_and_missing_source(sync_run: SyncRun) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        DataChange.objects.create(
            target_type=DataChange.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="status",
            old_value={"value": "same"},
            new_value={"value": "same"},
            sync_run=sync_run,
            origin_key=f"sync_run:{sync_run.pk}",
            rule_version="domain-reconcile-v1",
            change_key=hashlib.sha256(b"equal-values").hexdigest(),
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        DataChange.objects.create(
            target_type=DataChange.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="status",
            old_value="old",
            new_value="new",
            origin_key="missing-source",
            rule_version="domain-reconcile-v1",
            change_key=hashlib.sha256(b"missing-source").hexdigest(),
        )


@pytest.mark.django_db
def test_data_change_database_requires_manual_reason(actor_user: User) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        DataChange.objects.create(
            target_type=DataChange.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="display_name",
            old_value="Old",
            new_value="New",
            actor_user=actor_user,
            reason="",
            origin_key="request:db-manual-no-reason",
            rule_version="manual-correction-v1",
            change_key=hashlib.sha256(b"db-manual-no-reason").hexdigest(),
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("target_type", "arbitrary"),
        ("field_name", " "),
        ("rule_version", " "),
        ("origin_key", " "),
        ("change_key", "invalid"),
    ),
)
def test_data_change_database_rejects_invalid_fields(
    sync_run: SyncRun,
    field_name: str,
    invalid_value: object,
) -> None:
    values: dict[str, object] = {
        "target_type": DataChange.TargetType.COMPANY,
        "target_id": uuid.uuid4(),
        "field_name": "status",
        "old_value": "old",
        "new_value": "new",
        "sync_run": sync_run,
        "origin_key": f"sync_run:{sync_run.pk}",
        "rule_version": "domain-reconcile-v1",
        "change_key": hashlib.sha256(f"invalid-{field_name}".encode()).hexdigest(),
    }
    values[field_name] = invalid_value

    with pytest.raises(IntegrityError), transaction.atomic():
        DataChange.objects.create(**values)


@pytest.mark.django_db
def test_data_change_foreign_keys_are_protected(sync_run: SyncRun) -> None:
    change = record_data_change(
        target_type=DataChange.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        field_name="status",
        old_value="old",
        new_value="new",
        sync_run=sync_run,
        rule_version="domain-reconcile-v1",
    ).change
    assert change is not None

    with pytest.raises(ProtectedError):
        sync_run.delete()


@pytest.mark.django_db
@override_settings(AUDIT_IP_HASH_KEY="fixture-audit-ip-hash-key")
def test_record_user_action_records_hashed_ip_and_traceable_actor(actor_user: User) -> None:
    raw_ip = "203.0.113.42"
    result = record_user_action(
        actor_user=actor_user,
        action=AuditRecord.Action.MANUAL_CORRECTION,
        target_type=AuditRecord.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        before={"display_name": "Old"},
        after={"display_name": "New"},
        reason="Verified manual correction.",
        request_id="request-user-action-1",
        ip_address=raw_ip,
    )

    assert result.created is True
    assert result.record.actor_user_id == actor_user.pk
    assert result.record.sync_run_id is None
    assert result.record.ip_hash != raw_ip
    assert re.fullmatch(r"v1:[0-9a-f]{64}", result.record.ip_hash)
    assert raw_ip not in result.record.ip_hash
    assert timezone.is_aware(result.record.created_at)


@pytest.mark.django_db
def test_same_ip_and_audit_key_produce_same_hash_independent_of_django_secret(
    actor_user: User,
) -> None:
    raw_ip = "203.0.113.42"
    with override_settings(
        AUDIT_IP_HASH_KEY="fixture-stable-audit-ip-hash-key",
        SECRET_KEY="first-fixture-django-secret",
    ):
        first = record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            before={"status": "old"},
            after={"status": "new"},
            reason="Verified update.",
            request_id="request-same-ip-key-1",
            ip_address=raw_ip,
        )

    with override_settings(
        AUDIT_IP_HASH_KEY="fixture-stable-audit-ip-hash-key",
        SECRET_KEY="second-fixture-django-secret",
    ):
        second = record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            before={"status": "old"},
            after={"status": "new"},
            reason="Verified update.",
            request_id="request-same-ip-key-2",
            ip_address=raw_ip,
        )

    assert first.record.ip_hash == second.record.ip_hash


@pytest.mark.django_db
def test_same_ip_and_different_audit_keys_produce_different_hashes(
    actor_user: User,
) -> None:
    raw_ip = "203.0.113.42"
    with override_settings(AUDIT_IP_HASH_KEY="fixture-first-audit-ip-hash-key"):
        first = record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            before={"status": "old"},
            after={"status": "new"},
            reason="Verified update.",
            request_id="request-different-ip-key-1",
            ip_address=raw_ip,
        )

    with override_settings(AUDIT_IP_HASH_KEY="fixture-second-audit-ip-hash-key"):
        second = record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            before={"status": "old"},
            after={"status": "new"},
            reason="Verified update.",
            request_id="request-different-ip-key-2",
            ip_address=raw_ip,
        )

    assert first.record.ip_hash != second.record.ip_hash


@pytest.mark.django_db
def test_audit_idempotency_does_not_depend_on_ip_hash_key_rotation(
    actor_user: User,
) -> None:
    target_id = uuid.uuid4()
    with override_settings(AUDIT_IP_HASH_KEY="fixture-first-rotation-audit-key"):
        first = record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=target_id,
            before={"status": "old"},
            after={"status": "new"},
            reason="Verified update.",
            request_id="request-key-rotation-1",
            ip_address="203.0.113.42",
        )
        first_hash = first.record.ip_hash

    with override_settings(AUDIT_IP_HASH_KEY="fixture-second-rotation-audit-key"):
        repeated = record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=target_id,
            before={"status": "old"},
            after={"status": "new"},
            reason="Verified update.",
            request_id="request-key-rotation-1",
            ip_address="203.0.113.42",
        )

    assert first.created is True
    assert repeated.created is False
    assert repeated.record.pk == first.record.pk
    assert repeated.record.ip_hash == first_hash
    assert AuditRecord.objects.count() == 1


@pytest.mark.django_db
def test_record_system_action_records_sync_source(sync_run: SyncRun) -> None:
    result = record_system_action(
        sync_run=sync_run,
        action=AuditRecord.Action.UPDATE,
        target_type=AuditRecord.TargetType.SOURCE_EVIDENCE,
        target_id=uuid.uuid4(),
        before=None,
        after={"status": "normalized"},
        reason="Normalizer completed.",
        request_id="system-action-1",
    )

    assert result.created is True
    assert result.record.actor_user_id is None
    assert result.record.sync_run_id == sync_run.pk
    assert result.record.ip_hash == ""


@pytest.mark.django_db
def test_audit_record_service_rejects_missing_operation_source() -> None:
    with pytest.raises(InvalidAuditRecord):
        record_user_action(
            actor_user=cast(User, None),
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            before="old",
            after="new",
            reason="Missing actor fixture.",
            request_id="missing-source",
        )


@pytest.mark.django_db
def test_user_audit_action_requires_reason(actor_user: User) -> None:
    with pytest.raises(InvalidAuditRecord):
        record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            before="old",
            after="new",
            reason=" ",
            request_id="user-action-no-reason",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("before", "after"),
    (
        ({"token": "fixture-secret"}, "new"),
        ("old", {"Authorization": "Bearer fixture-secret"}),
        ({"Cookie": "fixture-secret"}, "new"),
        ("old", {"PASSWORD": "fixture-secret"}),
        ("https://user:fixture-secret@example.test/data", "new"),
        ("old", "https://example.test/data?token=fixture-secret"),
    ),
)
def test_audit_record_rejects_sensitive_values_without_echoing_secret(
    actor_user: User,
    before: object,
    after: object,
) -> None:
    with pytest.raises(SensitiveAuditRecordValue) as error:
        record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            before=before,
            after=after,
            reason="Verified correction.",
            request_id="sensitive-audit-fixture",
        )

    assert "fixture-secret" not in str(error.value)
    assert AuditRecord.objects.count() == 0


@pytest.mark.django_db
def test_audit_record_is_idempotent_for_identical_input(actor_user: User) -> None:
    target_id = uuid.uuid4()
    first = record_user_action(
        actor_user=actor_user,
        action=AuditRecord.Action.UPDATE,
        target_type=AuditRecord.TargetType.COMPANY,
        target_id=target_id,
        before={"status": "old"},
        after={"status": "new"},
        reason="Verified correction.",
        request_id="idempotent-user-action",
        ip_address="2001:db8::1",
    )
    second = record_user_action(
        actor_user=actor_user,
        action=AuditRecord.Action.UPDATE,
        target_type=AuditRecord.TargetType.COMPANY,
        target_id=target_id,
        before={"status": "old"},
        after={"status": "new"},
        reason="Verified correction.",
        request_id="idempotent-user-action",
        ip_address="2001:db8::1",
    )

    assert first.record.pk == second.record.pk
    assert first.created is True
    assert second.created is False
    assert AuditRecord.objects.count() == 1


@pytest.mark.django_db
def test_audit_record_rejects_blank_request_id(actor_user: User) -> None:
    with pytest.raises(InvalidAuditRecord):
        record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            before="old",
            after="new",
            reason="Verified correction.",
            request_id="  ",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("action", "target_type"),
    (
        ("arbitrary", AuditRecord.TargetType.COMPANY),
        (AuditRecord.Action.UPDATE, "arbitrary"),
    ),
)
def test_audit_record_service_rejects_unlisted_action_or_target(
    actor_user: User,
    action: str,
    target_type: str,
) -> None:
    with pytest.raises(InvalidAuditRecord):
        record_user_action(
            actor_user=actor_user,
            action=action,
            target_type=target_type,
            target_id=uuid.uuid4(),
            before="old",
            after="new",
            reason="Verified correction.",
            request_id="invalid-enum-action",
        )


@pytest.mark.django_db
def test_audit_record_database_rejects_missing_source_and_manual_reason(
    actor_user: User,
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        AuditRecord.objects.create(
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            before="old",
            after="new",
            reason="",
            request_id="db-missing-source",
            audit_key=hashlib.sha256(b"db-missing-source").hexdigest(),
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        AuditRecord.objects.create(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            before="old",
            after="new",
            reason="",
            request_id="db-manual-no-reason",
            audit_key=hashlib.sha256(b"db-manual-no-reason").hexdigest(),
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("action", "arbitrary"),
        ("target_type", "arbitrary"),
        ("request_id", " "),
        ("ip_hash", "203.0.113.42"),
        ("audit_key", "invalid"),
    ),
)
def test_audit_record_database_rejects_invalid_fields(
    sync_run: SyncRun,
    field_name: str,
    invalid_value: object,
) -> None:
    values: dict[str, object] = {
        "sync_run": sync_run,
        "action": AuditRecord.Action.UPDATE,
        "target_type": AuditRecord.TargetType.COMPANY,
        "target_id": uuid.uuid4(),
        "before": "old",
        "after": "new",
        "reason": "",
        "request_id": "db-valid-system-action",
        "audit_key": hashlib.sha256(f"invalid-{field_name}".encode()).hexdigest(),
    }
    values[field_name] = invalid_value

    with pytest.raises(IntegrityError), transaction.atomic():
        AuditRecord.objects.create(**values)


@pytest.mark.django_db
def test_audit_record_database_enforces_unique_key(sync_run: SyncRun) -> None:
    first = record_system_action(
        sync_run=sync_run,
        action=AuditRecord.Action.UPDATE,
        target_type=AuditRecord.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        before="old",
        after="new",
        request_id="unique-audit-key",
    ).record

    with pytest.raises(IntegrityError), transaction.atomic():
        AuditRecord.objects.create(
            sync_run=sync_run,
            action=AuditRecord.Action.CREATE,
            target_type=AuditRecord.TargetType.FILING,
            target_id=uuid.uuid4(),
            before=None,
            after="created",
            request_id="duplicate-audit-key",
            audit_key=first.audit_key,
        )


@pytest.mark.django_db
def test_audit_history_models_are_append_only(
    sync_run: SyncRun,
    actor_user: User,
) -> None:
    change = record_data_change(
        target_type=DataChange.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        field_name="status",
        old_value="old",
        new_value="new",
        sync_run=sync_run,
        rule_version="domain-reconcile-v1",
    ).change
    record = record_user_action(
        actor_user=actor_user,
        action=AuditRecord.Action.UPDATE,
        target_type=AuditRecord.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        before="old",
        after="new",
        reason="Verified correction.",
        request_id="append-only-audit",
    ).record
    assert change is not None

    change.reason = "Attempted overwrite"
    with pytest.raises(AppendOnlyRecordError):
        change.save()
    with pytest.raises(AppendOnlyRecordError):
        change.delete()

    record.reason = "Attempted overwrite"
    with pytest.raises(AppendOnlyRecordError):
        record.save()
    with pytest.raises(AppendOnlyRecordError):
        record.delete()

    assert DataChange.objects.filter(pk=change.pk).exists()
    assert AuditRecord.objects.filter(pk=record.pk).exists()


@pytest.mark.django_db
def test_audit_record_foreign_keys_are_protected(sync_run: SyncRun) -> None:
    record_system_action(
        sync_run=sync_run,
        action=AuditRecord.Action.UPDATE,
        target_type=AuditRecord.TargetType.SYNC_RUN,
        target_id=sync_run.pk,
        before={"status": "running"},
        after={"status": "succeeded"},
        request_id="protected-sync-run",
    )

    with pytest.raises(ProtectedError):
        sync_run.delete()
