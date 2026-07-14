import hashlib
import uuid
from datetime import date

import pytest
from django.db import IntegrityError
from django.utils import timezone

import companies.services as company_services
from accounts.models import User
from audit.models import AuditRecord, DataChange, DataSource, SourceEvidence, SyncRun
from audit.services import (
    DataChangeWriteResult,
    SensitiveDataChangeValue,
    build_data_change_key,
    record_raw_data_observation,
    record_source_evidence,
    start_sync_run,
)
from companies.models import Company, SecurityListing
from companies.services import (
    IDENTITY_RULE_VERSION,
    CompanyIdentityConflict,
    CompanyServiceError,
    ListingIdentityConflict,
    SecurityListingTransitionResult,
    create_company,
    create_security_listing,
    normalize_cik,
    transition_security_listing,
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


def _completed_listing_transition(
    company_sync_run: SyncRun,
) -> tuple[SecurityListing, SecurityListingTransitionResult]:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    prior = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        sync_run=company_sync_run,
    ).listing
    transition = transition_security_listing(
        listing=prior,
        transition_date=date(2026, 3, 1),
        ticker="NEW",
        exchange="XNYS",
        sync_run=company_sync_run,
    )
    return prior, transition


def _replay_listing_transition(
    prior: SecurityListing, company_sync_run: SyncRun
) -> SecurityListingTransitionResult:
    return transition_security_listing(
        listing=prior,
        transition_date=date(2026, 3, 1),
        ticker="NEW",
        exchange="XNYS",
        sync_run=company_sync_run,
    )


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


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("company", object()),
        ("ticker", "NEW"),
        ("exchange", "XNYS"),
        ("effective_from", date(2025, 12, 31)),
        ("effective_to", date(2026, 2, 1)),
    ),
)
def test_listing_update_rejects_historical_identity_fields(
    company_sync_run: SyncRun,
    field_name: str,
    value: object,
) -> None:
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
    prior_audits = AuditRecord.objects.count()

    with pytest.raises(CompanyServiceError) as error:
        update_security_listing(
            listing=listing,
            changes={field_name: value},
            sync_run=company_sync_run,
        )

    assert "transition_security_listing" in str(error.value)
    current = SecurityListing.objects.get(pk=listing.pk)
    assert current.company_id == company.pk
    assert current.ticker == "FIX"
    assert current.exchange == "XNAS"
    assert current.effective_from == date(2026, 1, 1)
    assert current.effective_to is None
    assert DataChange.objects.filter(target_id=listing.pk).count() == 0
    assert AuditRecord.objects.count() == prior_audits


@pytest.mark.django_db
def test_transition_listing_closes_history_and_creates_successor(
    company_sync_run: SyncRun,
) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    prior = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        is_primary=True,
        sync_run=company_sync_run,
    ).listing

    result = transition_security_listing(
        listing=prior,
        transition_date=date(2026, 3, 1),
        ticker="NEW",
        exchange="XNYS",
        security_name="Fixture Class A",
        is_primary=True,
        sync_run=company_sync_run,
    )

    old_listing = SecurityListing.objects.get(pk=prior.pk)
    successor = SecurityListing.objects.get(pk=result.successor_listing.pk)
    assert result.successor_created is True
    assert old_listing.pk == prior.pk
    assert old_listing.effective_to == date(2026, 3, 1)
    assert successor.pk != old_listing.pk
    assert successor.effective_from == date(2026, 3, 1)
    assert successor.effective_to is None
    assert (old_listing.ticker, old_listing.exchange) == ("FIX", "XNAS")
    assert (successor.ticker, successor.exchange) == ("NEW", "XNYS")
    assert (
        DataChange.objects.filter(target_id=old_listing.pk, field_name="effective_to").count() == 1
    )
    assert DataChange.objects.filter(target_id=successor.pk).count() == 0
    assert result.prior_audit_record is not None
    assert result.successor_audit_record is not None


@pytest.mark.django_db
def test_transition_listing_is_idempotent_only_for_same_closed_successor(
    company_sync_run: SyncRun,
) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    prior = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        sync_run=company_sync_run,
    ).listing
    first = transition_security_listing(
        listing=prior,
        transition_date=date(2026, 3, 1),
        ticker="NEW",
        exchange="XNYS",
        sync_run=company_sync_run,
    )
    audit_count = AuditRecord.objects.count()
    change_count = DataChange.objects.count()

    second = transition_security_listing(
        listing=prior,
        transition_date=date(2026, 3, 1),
        ticker="NEW",
        exchange="XNYS",
        sync_run=company_sync_run,
    )

    assert second.successor_created is False
    assert second.successor_listing.pk == first.successor_listing.pk
    assert AuditRecord.objects.count() == audit_count
    assert DataChange.objects.count() == change_count

    with pytest.raises(ListingIdentityConflict):
        transition_security_listing(
            listing=prior,
            transition_date=date(2026, 4, 1),
            ticker="NEW",
            exchange="XNYS",
            sync_run=company_sync_run,
        )


@pytest.mark.django_db
def test_transition_change_key_matches_the_public_audit_calculation(
    company_sync_run: SyncRun,
) -> None:
    prior, transition = _completed_listing_transition(company_sync_run)
    data_change = transition.data_changes[0].change
    assert data_change is not None

    assert data_change.change_key == build_data_change_key(
        target_type=DataChange.TargetType.SECURITY_LISTING,
        target_id=prior.pk,
        field_name="effective_to",
        old_value=None,
        new_value="2026-03-01",
        rule_version=IDENTITY_RULE_VERSION,
        source_evidence=None,
        sync_run=company_sync_run,
        actor_user=None,
        origin_key=f"sync_run:{company_sync_run.pk}",
    )


@pytest.mark.django_db
@pytest.mark.parametrize("missing_record", ("data_change", "prior_audit", "successor_audit"))
def test_transition_replay_rejects_missing_audit_history(
    company_sync_run: SyncRun,
    missing_record: str,
) -> None:
    prior, transition = _completed_listing_transition(company_sync_run)
    data_change = transition.data_changes[0].change
    assert data_change is not None
    assert transition.prior_audit_record is not None
    assert transition.successor_audit_record is not None

    # Simulate a corrupted/legacy partial history; production code cannot use
    # QuerySet deletion for append-only audit records.
    record_id = {
        "data_change": data_change.pk,
        "prior_audit": transition.prior_audit_record.pk,
        "successor_audit": transition.successor_audit_record.pk,
    }[missing_record]
    if missing_record == "data_change":
        DataChange.objects.filter(pk=record_id).delete()
    else:
        AuditRecord.objects.filter(pk=record_id).delete()

    audit_count = AuditRecord.objects.count()
    change_count = DataChange.objects.count()
    with pytest.raises(ListingIdentityConflict, match="audit history"):
        _replay_listing_transition(prior, company_sync_run)

    assert SecurityListing.objects.get(pk=prior.pk).effective_to == date(2026, 3, 1)
    assert SecurityListing.objects.filter(ticker="NEW", exchange="XNYS").count() == 1
    assert AuditRecord.objects.count() == audit_count
    assert DataChange.objects.count() == change_count


@pytest.mark.django_db
@pytest.mark.parametrize(
    "corruption",
    (
        "data_change_old_value",
        "data_change_new_value",
        "data_change_field_name",
        "data_change_origin_key",
        "data_change_change_key",
        "prior_audit_action",
        "prior_audit_after",
        "successor_audit_target",
        "prior_audit_request_id",
        "successor_audit_request_id",
        "data_change_sync_run",
    ),
)
def test_transition_replay_rejects_inconsistent_audit_history(
    company_sync_run: SyncRun,
    corruption: str,
) -> None:
    prior, transition = _completed_listing_transition(company_sync_run)
    data_change = transition.data_changes[0].change
    assert data_change is not None
    assert transition.prior_audit_record is not None
    assert transition.successor_audit_record is not None

    if corruption == "data_change_old_value":
        DataChange.objects.filter(pk=data_change.pk).update(old_value="not-null")
    elif corruption == "data_change_new_value":
        DataChange.objects.filter(pk=data_change.pk).update(new_value="2026-03-02")
    elif corruption == "data_change_field_name":
        DataChange.objects.filter(pk=data_change.pk).update(field_name="security_name")
    elif corruption == "data_change_origin_key":
        DataChange.objects.filter(pk=data_change.pk).update(origin_key="other-operation")
    elif corruption == "data_change_change_key":
        DataChange.objects.filter(pk=data_change.pk).update(
            change_key=hashlib.sha256(b"wrong-but-valid-transition-key").hexdigest()
        )
    elif corruption == "prior_audit_action":
        AuditRecord.objects.filter(pk=transition.prior_audit_record.pk).update(
            action=AuditRecord.Action.DEACTIVATE
        )
    elif corruption == "prior_audit_after":
        AuditRecord.objects.filter(pk=transition.prior_audit_record.pk).update(
            after={"effective_to": "2026-03-02"}
        )
    elif corruption == "successor_audit_target":
        AuditRecord.objects.filter(pk=transition.successor_audit_record.pk).update(
            target_id=uuid.uuid4()
        )
    elif corruption == "prior_audit_request_id":
        AuditRecord.objects.filter(pk=transition.prior_audit_record.pk).update(
            request_id="other-request"
        )
    elif corruption == "successor_audit_request_id":
        AuditRecord.objects.filter(pk=transition.successor_audit_record.pk).update(
            request_id="other-successor-request"
        )
    else:
        other_run = start_sync_run(
            job_type="fixture.company-transition-other",
            source=company_sync_run.source,
            scope={"fixture": "transition-other"},
            idempotency_key="fixture.company-transition-other:initial",
        )
        DataChange.objects.filter(pk=data_change.pk).update(sync_run=other_run)

    audit_count = AuditRecord.objects.count()
    change_count = DataChange.objects.count()
    with pytest.raises(ListingIdentityConflict, match="audit history"):
        _replay_listing_transition(prior, company_sync_run)

    assert SecurityListing.objects.get(pk=prior.pk).effective_to == date(2026, 3, 1)
    assert SecurityListing.objects.filter(ticker="NEW", exchange="XNYS").count() == 1
    assert AuditRecord.objects.count() == audit_count
    assert DataChange.objects.count() == change_count


@pytest.mark.django_db
def test_transition_listing_rejects_invalid_date_and_rolls_back_conflicts(
    company_sync_run: SyncRun,
) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    prior = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        sync_run=company_sync_run,
    ).listing
    other = create_company(
        legal_name="Other Incorporated",
        display_name="Other",
        cik="123457",
        sync_run=company_sync_run,
    ).company
    create_security_listing(
        company=other,
        ticker="NEW",
        exchange="XNYS",
        effective_from=date(2026, 2, 1),
        sync_run=company_sync_run,
    )

    with pytest.raises(CompanyServiceError):
        transition_security_listing(
            listing=prior,
            transition_date=date(2026, 1, 1),
            ticker="NEW",
            exchange="XNYS",
            sync_run=company_sync_run,
        )
    with pytest.raises(ListingIdentityConflict):
        transition_security_listing(
            listing=prior,
            transition_date=date(2026, 3, 1),
            ticker="NEW",
            exchange="XNYS",
            sync_run=company_sync_run,
        )

    assert SecurityListing.objects.get(pk=prior.pk).effective_to is None
    assert DataChange.objects.filter(target_id=prior.pk).count() == 0


@pytest.mark.django_db
def test_transition_listing_primary_conflict_rolls_back_old_interval(
    company_sync_run: SyncRun,
) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    prior = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        sync_run=company_sync_run,
    ).listing
    create_security_listing(
        company=company,
        ticker="PRIMARY",
        exchange="XNYS",
        effective_from=date(2026, 1, 1),
        is_primary=True,
        sync_run=company_sync_run,
    )

    with pytest.raises(ListingIdentityConflict):
        transition_security_listing(
            listing=prior,
            transition_date=date(2026, 3, 1),
            ticker="NEW",
            exchange="ARCX",
            is_primary=True,
            sync_run=company_sync_run,
        )

    assert SecurityListing.objects.get(pk=prior.pk).effective_to is None
    assert SecurityListing.objects.filter(company=company, ticker="NEW").count() == 0


@pytest.mark.django_db
def test_transition_listing_rolls_back_when_audit_or_change_write_fails(
    company_sync_run: SyncRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    company = create_company(
        legal_name="Fixture Incorporated",
        display_name="Fixture",
        cik="123456",
        sync_run=company_sync_run,
    ).company
    prior = create_security_listing(
        company=company,
        ticker="FIX",
        exchange="XNAS",
        effective_from=date(2026, 1, 1),
        sync_run=company_sync_run,
    ).listing

    def fail_data_changes(**_: object) -> tuple[DataChangeWriteResult, ...]:
        raise CompanyServiceError("fixture data-change failure")

    monkeypatch.setattr(company_services, "_record_data_changes", fail_data_changes)
    with pytest.raises(CompanyServiceError):
        transition_security_listing(
            listing=prior,
            transition_date=date(2026, 3, 1),
            ticker="NEW",
            exchange="XNYS",
            sync_run=company_sync_run,
        )
    assert SecurityListing.objects.get(pk=prior.pk).effective_to is None

    monkeypatch.undo()

    def fail_audit(**_: object) -> AuditRecord:
        raise CompanyServiceError("fixture audit failure")

    monkeypatch.setattr(company_services, "_record_action", fail_audit)
    with pytest.raises(CompanyServiceError):
        transition_security_listing(
            listing=prior,
            transition_date=date(2026, 3, 1),
            ticker="NEW",
            exchange="XNYS",
            sync_run=company_sync_run,
        )
    assert SecurityListing.objects.get(pk=prior.pk).effective_to is None
    assert SecurityListing.objects.filter(company=company, ticker="NEW").count() == 0


@pytest.mark.django_db
def test_company_service_uses_persisted_source_evidence_context(
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
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=uuid.uuid4(),
        field_name="display_name",
    )
    evidence.target_id = company.pk
    evidence.sync_run = company_sync_run

    with pytest.raises(CompanyServiceError) as error:
        update_company(
            company=company,
            changes={"display_name": "Updated Fixture"},
            source_evidence=evidence,
        )

    assert "domain target" in str(error.value)
    assert Company.objects.get(pk=company.pk).display_name == "Fixture"
    assert DataChange.objects.count() == 0
    assert AuditRecord.objects.filter(target_id=company.pk).count() == 1


@pytest.mark.django_db
def test_company_service_rejects_evidence_and_sync_run_from_different_sources(
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
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=company.pk,
        field_name="display_name",
    )
    other_source = DataSource.objects.create(
        key="company-other-source",
        name="Company other source",
        source_type=DataSource.SourceType.MANUAL,
        license_notes="Synthetic test-only source.",
    )
    other_run = start_sync_run(
        job_type="fixture.company-other",
        source=other_source,
        scope={"fixture": "other"},
        idempotency_key="fixture.company-other:initial",
    )

    with pytest.raises(CompanyServiceError) as error:
        update_company(
            company=company,
            changes={"display_name": "Updated Fixture"},
            source_evidence=evidence,
            sync_run=other_run,
        )

    assert "SourceEvidence and SyncRun" in str(error.value)
    assert "fixture" not in str(error.value)
    assert Company.objects.get(pk=company.pk).display_name == "Fixture"
    assert DataChange.objects.count() == 0


@pytest.mark.django_db
def test_listing_write_rejects_unsaved_or_wrong_target_evidence_without_writes(
    company_sync_run: SyncRun,
) -> None:
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

    with pytest.raises(CompanyServiceError):
        update_security_listing(
            listing=listing,
            changes={"security_name": "Updated"},
            source_evidence=SourceEvidence(),
        )

    wrong_evidence = _evidence(
        company_sync_run,
        target_type=SourceEvidence.TargetType.COMPANY,
        target_id=listing.pk,
        field_name="security_name",
    )
    with pytest.raises(CompanyServiceError):
        update_security_listing(
            listing=listing,
            changes={"security_name": "Updated"},
            source_evidence=wrong_evidence,
        )

    assert SecurityListing.objects.get(pk=listing.pk).security_name == ""
    assert DataChange.objects.filter(target_id=listing.pk).count() == 0
