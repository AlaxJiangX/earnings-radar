import hashlib
import re
import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import DataSource, RawDataObservation, SourceEvidence, SyncRun
from audit.services import (
    InvalidEvidenceReference,
    InvalidSourceEvidence,
    SensitiveEvidenceValue,
    record_raw_data_observation,
    record_source_evidence,
    start_sync_run,
)


@pytest.mark.django_db
def test_service_records_traceable_source_evidence(sync_run: SyncRun) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b'{"ticker":"NVDA"}',
    )
    target_id = uuid.uuid4()

    result = record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name="ticker",
        raw_value={"ticker": "nvda"},
        normalized_value={"ticker": "NVDA"},
        confidence=Decimal("0.9750"),
        normalizer_version="company-normalizer-v1",
    )

    evidence = result.evidence
    assert result.created is True
    assert evidence.raw_data_record_id == raw_result.record.pk
    assert evidence.sync_run_id == sync_run.pk
    assert evidence.raw_data_record.source_id == sync_run.source_id
    assert evidence.target_type == SourceEvidence.TargetType.COMPANY
    assert evidence.target_id == target_id
    assert evidence.raw_value == {"ticker": "nvda"}
    assert evidence.normalized_value == {"ticker": "NVDA"}
    assert evidence.is_official is sync_run.source.is_official
    assert evidence.confidence == Decimal("0.9750")
    assert evidence.observed_at == raw_result.observation.observed_at
    assert timezone.is_aware(evidence.observed_at)
    assert evidence.normalizer_version == "company-normalizer-v1"
    assert re.fullmatch(r"[0-9a-f]{64}", evidence.evidence_key)


@pytest.mark.django_db
def test_service_is_idempotent_for_identical_evidence(sync_run: SyncRun) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b'{"ticker":"NVDA"}',
    )
    target_id = uuid.uuid4()

    first = record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name="ticker",
        raw_value="nvda",
        normalized_value={"ticker": "NVDA", "exchange": "NASDAQ"},
        confidence=Decimal("0.9000"),
        normalizer_version="company-normalizer-v1",
    )
    second = record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name="ticker",
        raw_value="nvda",
        normalized_value={"exchange": "NASDAQ", "ticker": "NVDA"},
        confidence=Decimal("0.9000"),
        normalizer_version="company-normalizer-v1",
    )

    assert first.evidence.pk == second.evidence.pk
    assert first.created is True
    assert second.created is False
    assert SourceEvidence.objects.count() == 1


@pytest.mark.django_db
def test_different_raw_records_create_distinct_evidence_for_same_normalized_value(
    data_source: DataSource,
    sync_run: SyncRun,
) -> None:
    first_raw = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b'{"ticker":"nvda"}',
    )
    target_id = uuid.uuid4()
    first = record_source_evidence(
        raw_data_record=first_raw.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name="ticker",
        raw_value="nvda",
        normalized_value="NVDA",
        confidence=Decimal("0.8000"),
        normalizer_version="company-normalizer-v1",
    )
    later_run = start_sync_run(
        job_type="fixture.raw-data",
        source=data_source,
        scope={"fixture": "later"},
        idempotency_key="fixture.raw-data:evidence-later",
    )
    later_raw = record_raw_data_observation(
        sync_run=later_run,
        source_url="https://example.test/company",
        payload=b'{"ticker":"NVDA","unchanged":true}',
    )

    second = record_source_evidence(
        raw_data_record=later_raw.record,
        sync_run=later_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name="ticker",
        raw_value="NVDA",
        normalized_value="NVDA",
        confidence=Decimal("0.9500"),
        normalizer_version="company-normalizer-v1",
    )

    assert second.evidence.pk != first.evidence.pk
    assert second.created is True
    assert second.evidence.raw_data_record_id == later_raw.record.pk
    assert SourceEvidence.objects.count() == 2


@pytest.mark.django_db
def test_later_run_reuses_evidence_for_the_same_raw_record(
    data_source: DataSource,
    sync_run: SyncRun,
) -> None:
    first_raw = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b'{"ticker":"NVDA"}',
    )
    target_id = uuid.uuid4()
    first = record_source_evidence(
        raw_data_record=first_raw.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name="ticker",
        raw_value="NVDA",
        normalized_value="NVDA",
        confidence=Decimal("0.8000"),
        normalizer_version="company-normalizer-v1",
    )
    later_run = start_sync_run(
        job_type="fixture.raw-data",
        source=data_source,
        scope={"fixture": "same-record-later"},
        idempotency_key="fixture.raw-data:evidence-same-record-later",
    )
    later_raw = record_raw_data_observation(
        sync_run=later_run,
        source_url="https://example.test/company",
        payload=b'{"ticker":"NVDA"}',
    )

    second = record_source_evidence(
        raw_data_record=later_raw.record,
        sync_run=later_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name="ticker",
        raw_value="NVDA",
        normalized_value="NVDA",
        confidence=Decimal("0.9500"),
        normalizer_version="company-normalizer-v1",
    )

    assert later_raw.record.pk == first_raw.record.pk
    assert second.evidence.pk == first.evidence.pk
    assert second.created is False
    assert SourceEvidence.objects.count() == 1


@pytest.mark.django_db
def test_rule_or_normalized_value_change_creates_new_evidence(sync_run: SyncRun) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b'{"ticker":"NVDA"}',
    )
    target_id = uuid.uuid4()
    first = record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name="ticker",
        raw_value="nvda",
        normalized_value="NVDA",
        confidence=1,
        normalizer_version="company-normalizer-v1",
    )
    changed_value = record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name="ticker",
        raw_value="nvda.a",
        normalized_value="NVDA.A",
        confidence=1,
        normalizer_version="company-normalizer-v1",
    )
    changed_rule = record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=target_id,
        field_name="ticker",
        raw_value="nvda",
        normalized_value="NVDA",
        confidence=1,
        normalizer_version="company-normalizer-v2",
    )

    assert len({first.evidence.pk, changed_value.evidence.pk, changed_rule.evidence.pk}) == 3


@pytest.mark.django_db
def test_future_target_uuid_does_not_require_a_domain_model(sync_run: SyncRun) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/future-company",
        payload=b"future-company-fixture",
    )

    result = record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        field_name="",
        raw_value={"name": "Fixture Corp"},
        normalized_value={"display_name": "Fixture Corp"},
        confidence=1,
        normalizer_version="company-normalizer-v1",
    )

    assert result.created is True
    assert result.evidence.field_name == ""


@pytest.mark.django_db
def test_service_requires_matching_raw_data_observation(
    data_source: DataSource,
    sync_run: SyncRun,
) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b"company-fixture",
    )
    unrelated_run = start_sync_run(
        job_type="fixture.raw-data",
        source=data_source,
        scope={},
        idempotency_key="fixture.raw-data:no-observation",
    )

    with pytest.raises(InvalidEvidenceReference):
        record_source_evidence(
            raw_data_record=raw_result.record,
            sync_run=unrelated_run,
            target_type=SourceEvidence.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="ticker",
            raw_value="nvda",
            normalized_value="NVDA",
            confidence=1,
            normalizer_version="company-normalizer-v1",
        )

    assert SourceEvidence.objects.count() == 0


@pytest.mark.django_db
def test_service_rejects_observation_with_mismatched_source(sync_run: SyncRun) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b"company-fixture",
    )
    other_source = DataSource.objects.create(
        key="other-fixture-source",
        name="Other fixture source",
        source_type=DataSource.SourceType.MANUAL,
    )
    other_run = start_sync_run(
        job_type="fixture.raw-data",
        source=other_source,
        scope={},
        idempotency_key="fixture.raw-data:other-source",
    )
    RawDataObservation.objects.create(
        sync_run=other_run,
        raw_data_record=raw_result.record,
    )

    with pytest.raises(InvalidEvidenceReference):
        record_source_evidence(
            raw_data_record=raw_result.record,
            sync_run=other_run,
            target_type=SourceEvidence.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="ticker",
            raw_value="nvda",
            normalized_value="NVDA",
            confidence=1,
            normalizer_version="company-normalizer-v1",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("raw_value", "normalized_value"),
    (
        ({"Authorization": "Bearer fixture-secret"}, "NVDA"),
        ("nvda", {"access_token": "fixture-secret"}),
        ("token=fixture-secret", "NVDA"),
        ("nvda", "Authorization: Bearer fixture-secret"),
        ({"auth": "Basic dXNlcjpwYXNz"}, "NVDA"),
        ({"nested": {"ToKeN": "fixture-secret"}}, "NVDA"),
        ({"headers": {"AUTHORIZATION": "Bearer fixture-secret"}}, "NVDA"),
        ("Basic dXNlcjpwYXNz", "NVDA"),
        ("nvda", "Bearer fixture-secret"),
        ("https://user:fixture-secret@example.test/data", "NVDA"),
        ("nvda", "https://example.test/data?api_key=fixture-secret"),
    ),
)
def test_service_rejects_sensitive_evidence_values(
    sync_run: SyncRun,
    raw_value: object,
    normalized_value: object,
) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b"company-fixture",
    )

    with pytest.raises(SensitiveEvidenceValue) as error:
        record_source_evidence(
            raw_data_record=raw_result.record,
            sync_run=sync_run,
            target_type=SourceEvidence.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="ticker",
            raw_value=raw_value,
            normalized_value=normalized_value,
            confidence=1,
            normalizer_version="company-normalizer-v1",
        )

    assert "fixture-secret" not in str(error.value)
    assert "dXNlcjpwYXNz" not in str(error.value)
    assert SourceEvidence.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("confidence", (-0.1, 1.1, "NaN", True))
def test_service_rejects_invalid_confidence(
    sync_run: SyncRun,
    confidence: Decimal | int | float | str,
) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b"company-fixture",
    )

    with pytest.raises(InvalidSourceEvidence):
        record_source_evidence(
            raw_data_record=raw_result.record,
            sync_run=sync_run,
            target_type=SourceEvidence.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="ticker",
            raw_value="nvda",
            normalized_value="NVDA",
            confidence=confidence,
            normalizer_version="company-normalizer-v1",
        )


@pytest.mark.django_db
@pytest.mark.parametrize("target_type", ("arbitrary", "", "notification"))
def test_service_rejects_unlisted_target_types(sync_run: SyncRun, target_type: str) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b"company-fixture",
    )

    with pytest.raises(InvalidSourceEvidence):
        record_source_evidence(
            raw_data_record=raw_result.record,
            sync_run=sync_run,
            target_type=target_type,
            target_id=uuid.uuid4(),
            field_name="ticker",
            raw_value="nvda",
            normalized_value="NVDA",
            confidence=1,
            normalizer_version="company-normalizer-v1",
        )


@pytest.mark.django_db
def test_service_rejects_empty_normalizer_version(sync_run: SyncRun) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b"company-fixture",
    )

    with pytest.raises(InvalidSourceEvidence):
        record_source_evidence(
            raw_data_record=raw_result.record,
            sync_run=sync_run,
            target_type=SourceEvidence.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="ticker",
            raw_value="nvda",
            normalized_value="NVDA",
            confidence=1,
            normalizer_version="   ",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("target_type", "arbitrary"),
        ("confidence", Decimal("-0.0001")),
        ("confidence", Decimal("1.0001")),
        ("normalizer_version", ""),
        ("normalizer_version", "   "),
        ("evidence_key", "not-a-sha256"),
    ),
)
def test_database_rejects_invalid_evidence_fields(
    sync_run: SyncRun,
    field_name: str,
    invalid_value: object,
) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b"company-fixture",
    )
    evidence = record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        field_name="ticker",
        raw_value="nvda",
        normalized_value="NVDA",
        confidence=1,
        normalizer_version="company-normalizer-v1",
    ).evidence

    with pytest.raises(IntegrityError), transaction.atomic():
        SourceEvidence.objects.filter(pk=evidence.pk).update(**{field_name: invalid_value})


@pytest.mark.django_db
def test_database_requires_matching_raw_data_observation(
    data_source: DataSource,
    sync_run: SyncRun,
) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b"company-fixture",
    )
    unrelated_run = start_sync_run(
        job_type="fixture.raw-data",
        source=data_source,
        scope={},
        idempotency_key="fixture.raw-data:db-no-observation",
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        SourceEvidence.objects.create(
            raw_data_record=raw_result.record,
            sync_run=unrelated_run,
            target_type=SourceEvidence.TargetType.COMPANY,
            target_id=uuid.uuid4(),
            field_name="ticker",
            raw_value="nvda",
            normalized_value="NVDA",
            is_official=False,
            confidence=Decimal("1.0000"),
            observed_at=timezone.now(),
            normalizer_version="company-normalizer-v1",
            evidence_key=hashlib.sha256(b"db-no-observation").hexdigest(),
        )


@pytest.mark.django_db
def test_database_requires_unique_evidence_key(sync_run: SyncRun) -> None:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company",
        payload=b"company-fixture",
    )
    evidence = record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        field_name="ticker",
        raw_value="nvda",
        normalized_value="NVDA",
        confidence=1,
        normalizer_version="company-normalizer-v1",
    ).evidence

    with pytest.raises(IntegrityError), transaction.atomic():
        SourceEvidence.objects.create(
            raw_data_record=raw_result.record,
            sync_run=sync_run,
            target_type=SourceEvidence.TargetType.FILING,
            target_id=uuid.uuid4(),
            field_name="form_type",
            raw_value="10-q",
            normalized_value="10-Q",
            is_official=False,
            confidence=Decimal("1.0000"),
            observed_at=raw_result.observation.observed_at,
            normalizer_version="filing-normalizer-v1",
            evidence_key=evidence.evidence_key,
        )
