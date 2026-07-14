import uuid
from datetime import date

import pytest
from django.db import IntegrityError
from django.utils import timezone

import companies.services as company_services
from accounts.models import User
from audit.models import AuditRecord, DataChange, SourceEvidence, SyncRun
from audit.services import (
    SensitiveDataChangeValue,
    record_raw_data_observation,
    record_source_evidence,
)
from companies.models import Company, SecurityListing
from companies.services import (
    CompanyIdentityConflict,
    CompanyServiceError,
    ListingIdentityConflict,
    create_company,
    create_security_listing,
    normalize_cik,
    update_company,
    update_security_listing,
)


@pytest.fixture
def company_actor(db: object) -> User:
    del db
    return User.objects.create_user(
        email="company-actor@example.com",
        password="fixture-password-only",
        is_staff=True,
    )


def _evidence(
    sync_run: SyncRun,
    *,
    target_type: str,
    target_id: uuid.UUID,
    field_name: str,
) -> SourceEvidence:
    raw_result = record_raw_data_observation(
        sync_run=sync_run,
        source_url="https://example.test/company-identity",
        payload=b'{"identity":"fixture"}',
    )
    return record_source_evidence(
        raw_data_record=raw_result.record,
        sync_run=sync_run,
        target_type=target_type,
        target_id=target_id,
        field_name=field_name,
        raw_value="fixture",
        normalized_value="fixture",
        confidence=1,
        normalizer_version="company-identity-fixture-v1",
    ).evidence


def test_normalize_cik_pads_digits_and_rejects_invalid_values() -> None:
    assert normalize_cik("123456") == "0000123456"
    assert normalize_cik(123456) == "0000123456"
    assert normalize_cik(None) is None

    with pytest.raises(CompanyServiceError):
        normalize_cik("12-3456")


@pytest.mark.django_db
def test_create_company_is_idempotent_by_normalized_cik(company_sync_run: SyncRun) -> None:
    first = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        country_code="us",
        sync_run=company_sync_run,
    )
    second = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="0000123456",
        country_code="US",
        sync_run=company_sync_run,
    )

    assert first.created is True
    assert second.created is False
    assert first.company.pk == second.company.pk
    assert Company.objects.count() == 1
    assert AuditRecord.objects.filter(target_id=first.company.pk).count() == 1


@pytest.mark.django_db
def test_create_company_without_cik_requires_stable_uuid(company_sync_run: SyncRun) -> None:
    with pytest.raises(CompanyServiceError):
        create_company(
            legal_name="Fixture Incorporated",
            display_name="Fixture",
            sync_run=company_sync_run,
        )

    company_id = uuid.uuid4()
    result = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        company_id=company_id,
        sync_run=company_sync_run,
    )

    assert result.company.pk == company_id


@pytest.mark.django_db
def test_conflicting_company_cik_requires_audited_update(company_sync_run: SyncRun) -> None:
    create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    )

    with pytest.raises(CompanyIdentityConflict):
        create_company(
            legal_name="Different Fixture Incorporated",
            display_name="Fixture",
            cik="123456",
            sync_run=company_sync_run,
        )


@pytest.mark.django_db
def test_company_create_recovers_from_unique_identity_race(
    company_sync_run: SyncRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = Company.objects.create(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="0000123456",
    )
    identities = iter((None, existing))

    def find_identity(**_: object) -> Company | None:
        return next(identities)

    def raise_unique_conflict(**_: object) -> Company:
        raise IntegrityError("synthetic unique identity race")

    monkeypatch.setattr(company_services, "_find_company_identity", find_identity)
    monkeypatch.setattr(Company.objects, "create", raise_unique_conflict)

    result = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    )

    assert result.company.pk == existing.pk
    assert result.created is False
    assert Company.objects.count() == 1


@pytest.mark.django_db
def test_update_company_creates_traceable_data_change(company_sync_run: SyncRun) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    evidence = _evidence(
        company_sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=company.pk,
        field_name="display_name",
    )

    result = update_company(
        company=company,
        changes={"display_name": "Updated Fixture"},
        source_evidence=evidence,
    )

    assert result.data_changes[0].created is True
    change = result.data_changes[0].change
    assert change is not None
    assert change.source_evidence_id == evidence.pk
    assert change.sync_run_id == company_sync_run.pk
    assert change.old_value == "Fixture"
    assert change.new_value == "Updated Fixture"
    assert timezone.is_aware(change.changed_at)
    assert result.audit_record is not None


@pytest.mark.django_db
def test_update_company_skips_equal_values(company_sync_run: SyncRun) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company

    result = update_company(
        company=company,
        changes={"display_name": "Fixture"},
        sync_run=company_sync_run,
    )

    assert result.data_changes == ()
    assert result.audit_record is None
    assert DataChange.objects.count() == 0


@pytest.mark.django_db
def test_automatic_update_requires_a_sync_source(company_sync_run: SyncRun) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company

    with pytest.raises(CompanyServiceError):
        update_company(
            company=company,
            changes={"display_name": "Updated Fixture"},
        )

    assert Company.objects.get(pk=company.pk).display_name == "Fixture"
    assert DataChange.objects.count() == 0


@pytest.mark.django_db
def test_manual_update_requires_reason_and_creates_manual_audit(
    company_sync_run: SyncRun,
    company_actor: User,
) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company

    with pytest.raises(CompanyServiceError):
        update_company(
            company=company,
            changes={"display_name": "Corrected Fixture"},
            actor_user=company_actor,
            request_id="company-manual-1",
        )

    result = update_company(
        company=company,
        changes={"display_name": "Corrected Fixture"},
        actor_user=company_actor,
        reason="Verified correction.",
        request_id="company-manual-1",
    )

    assert result.audit_record is not None
    assert result.audit_record.action == AuditRecord.Action.MANUAL_CORRECTION
    assert result.audit_record.actor_user_id == company_actor.pk


@pytest.mark.django_db
def test_sensitive_manual_reason_is_rejected_without_persisting_it(
    company_sync_run: SyncRun,
    company_actor: User,
) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company

    with pytest.raises(SensitiveDataChangeValue) as error:
        update_company(
            company=company,
            changes={"display_name": "Updated Fixture"},
            actor_user=company_actor,
            reason="Bearer fixture-reason-secret",
            request_id="company-manual-sensitive",
        )

    assert "fixture-reason-secret" not in str(error.value)
    assert Company.objects.get(pk=company.pk).display_name == "Fixture"
    assert DataChange.objects.count() == 0


@pytest.mark.django_db
def test_company_service_rejects_sensitive_values_without_persisting_them(
    company_sync_run: SyncRun,
) -> None:
    with pytest.raises(CompanyServiceError) as error:
        create_company(
            legal_name="Bearer fixture-secret",
            display_name="Fixture",
            cik="123456",
            sync_run=company_sync_run,
        )

    assert "fixture-secret" not in str(error.value)
    assert Company.objects.count() == 0


@pytest.mark.django_db
def test_create_listing_is_idempotent_and_normalizes_ticker(company_sync_run: SyncRun) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    first = create_security_listing(
        company=company,
        ticker=" fix ",
        exchange=" xnas ",
        effective_from=date(2026, 1, 1),
        is_primary=True,
        sync_run=company_sync_run,
    )
    second = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        is_primary=True,
        sync_run=company_sync_run,
    )

    assert first.created is True
    assert second.created is False
    assert first.listing.pk == second.listing.pk
    assert first.listing.ticker == "FIX"
    assert first.listing.exchange == "XNAS"


@pytest.mark.django_db
def test_listing_can_link_preallocated_source_evidence(company_sync_run: SyncRun) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    listing_id = uuid.uuid4()
    evidence = _evidence(
        company_sync_run,
        target_type=SourceEvidence.TargetType.SECURITY_LISTING,
        target_id=listing_id,
        field_name="",
    )

    result = create_security_listing(
        company=company,
        listing_id=listing_id,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        source_evidence=evidence,
    )

    assert result.listing.source_evidence_id == evidence.pk


@pytest.mark.django_db
def test_creation_with_evidence_requires_matching_preallocated_uuid(
    company_sync_run: SyncRun,
) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    evidence = _evidence(
        company_sync_run,
        target_type=SourceEvidence.TargetType.SECURITY_LISTING,
        target_id=uuid.uuid4(),
        field_name="",
    )

    with pytest.raises(CompanyServiceError):
        create_security_listing(
            company=company,
            ticker="FIX",
            exchange="XNAS",
            effective_from=date(2026, 1, 1),
            source_evidence=evidence,
        )


@pytest.mark.django_db
def test_listing_allows_same_ticker_on_different_exchanges(company_sync_run: SyncRun) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    first = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        sync_run=company_sync_run,
    )
    second = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNYS",
        effective_from=date(2026, 1, 1),
        sync_run=company_sync_run,
    )

    assert first.listing.pk != second.listing.pk


@pytest.mark.django_db
def test_listing_rejects_blank_ticker(company_sync_run: SyncRun) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company

    with pytest.raises(CompanyServiceError):
        create_security_listing(
            company=company,
            ticker="  ",
            exchange="XNAS",
            effective_from=date(2026, 1, 1),
            sync_run=company_sync_run,
        )


@pytest.mark.django_db
def test_listing_overlap_is_rejected_by_service(company_sync_run: SyncRun) -> None:
    first_company = create_company(
        legal_name="First Incorporated",
        display_name="First",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    second_company = create_company(
        legal_name="Second Incorporated",
        display_name="Second",
        cik="123457",
        sync_run=company_sync_run,
    ).company
    create_security_listing(
        company=first_company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        sync_run=company_sync_run,
    )

    with pytest.raises(ListingIdentityConflict):
        create_security_listing(
            company=second_company,
            ticker="FIX",
            exchange="XNAS",
            effective_from=date(2026, 2, 1),
            sync_run=company_sync_run,
        )


@pytest.mark.django_db
def test_listing_update_creates_data_change(company_sync_run: SyncRun) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    listing = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        sync_run=company_sync_run,
    ).listing
    evidence = _evidence(
        company_sync_run,
        target_type=SourceEvidence.TargetType.SECURITY_LISTING,
        target_id=listing.pk,
        field_name="security_name",
    )

    result = update_security_listing(
        listing=listing,
        changes={"security_name": "Fixture Class A"},
        source_evidence=evidence,
    )

    assert result.data_changes[0].change is not None
    assert result.data_changes[0].change.source_evidence_id == evidence.pk
    assert SecurityListing.objects.get(pk=listing.pk).security_name == "Fixture Class A"


@pytest.mark.django_db
def test_listing_update_skips_equal_values(company_sync_run: SyncRun) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    listing = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        sync_run=company_sync_run,
    ).listing

    result = update_security_listing(
        listing=listing,
        changes={"security_name": ""},
        sync_run=company_sync_run,
    )

    assert result.data_changes == ()
    assert result.audit_record is None
