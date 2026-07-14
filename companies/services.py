from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, cast

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from audit.models import AuditRecord, DataChange, SourceEvidence, SyncRun
from audit.security import InvalidAuditValue, SensitiveAuditData, normalize_json_without_credentials
from audit.services import (
    DataChangeWriteResult,
    InvalidEvidenceReference,
    SourceEvidenceReference,
    record_data_change,
    record_system_action,
    record_user_action,
    resolve_source_evidence_reference,
)
from companies.models import Company, SecurityListing

if TYPE_CHECKING:
    from accounts.models import User


IDENTITY_RULE_VERSION = "company-listing-identity-v1"
_CIK_RE = re.compile(r"^[0-9]{1,10}$")
_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")
_MONTH_DAY_RE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")


class CompanyServiceError(ValueError):
    pass


class CompanyIdentityConflict(CompanyServiceError):
    pass


class ListingIdentityConflict(CompanyServiceError):
    pass


@dataclass(frozen=True, slots=True)
class CompanyWriteResult:
    company: Company
    created: bool
    data_changes: tuple[DataChangeWriteResult, ...]
    audit_record: AuditRecord | None


@dataclass(frozen=True, slots=True)
class SecurityListingWriteResult:
    listing: SecurityListing
    created: bool
    data_changes: tuple[DataChangeWriteResult, ...]
    audit_record: AuditRecord | None


@dataclass(frozen=True, slots=True)
class SecurityListingTransitionResult:
    prior_listing: SecurityListing
    successor_listing: SecurityListing
    successor_created: bool
    data_changes: tuple[DataChangeWriteResult, ...]
    prior_audit_record: AuditRecord | None
    successor_audit_record: AuditRecord | None


def normalize_cik(value: str | int | None) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise CompanyServiceError("CIK must be a string of at most 10 digits.")
    normalized = str(value).strip()
    if not _CIK_RE.fullmatch(normalized):
        raise CompanyServiceError("CIK must contain 1 to 10 ASCII digits.")
    return normalized.zfill(10)


def normalize_ticker(value: str) -> str:
    return _normalize_required_text(value, value_name="ticker", maximum_length=32).upper()


def normalize_exchange(value: str) -> str:
    return _normalize_required_text(value, value_name="exchange", maximum_length=32).upper()


def create_company(
    *,
    legal_name: str,
    display_name: str,
    cik: str | int | None = None,
    country_code: str | None = None,
    issuer_type: str = Company.IssuerType.UNKNOWN,
    fiscal_year_end_month_day: str | None = None,
    investor_relations_url: str | None = None,
    monitoring_status: str = Company.MonitoringStatus.PENDING_IDENTITY,
    company_id: uuid.UUID | None = None,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
) -> CompanyWriteResult:
    values = _normalize_company_values(
        legal_name=legal_name,
        display_name=display_name,
        cik=cik,
        country_code=country_code,
        issuer_type=issuer_type,
        fiscal_year_end_month_day=fiscal_year_end_month_day,
        investor_relations_url=investor_relations_url,
        monitoring_status=monitoring_status,
    )
    normalized_id = _normalize_optional_uuid(company_id, value_name="company_id")
    if values["cik"] is None and normalized_id is None:
        raise CompanyServiceError("A Company without a CIK requires a stable company_id.")

    with transaction.atomic():
        existing = _find_company_identity(
            cik=cast(str | None, values["cik"]), company_id=normalized_id
        )
        if existing is not None:
            _ensure_company_matches(existing, values)
            return CompanyWriteResult(existing, False, (), None)

        target_id = normalized_id or uuid.uuid4()
        context = _resolve_write_context(
            target_type=DataChange.TargetType.COMPANY,
            target_id=target_id,
            source_evidence=source_evidence,
            sync_run=sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        try:
            with transaction.atomic():
                company = Company.objects.create(id=target_id, **values)
        except IntegrityError as error:
            existing = _find_company_identity(
                cik=cast(str | None, values["cik"]),
                company_id=normalized_id,
            )
            if existing is None:
                raise error from None
            _ensure_company_matches(existing, values)
            return CompanyWriteResult(existing, False, (), None)

        audit_record = _record_action(
            context=context,
            action=AuditRecord.Action.CREATE,
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=company.pk,
            before={},
            after=_company_snapshot(company),
        )
        return CompanyWriteResult(company, True, (), audit_record)


def update_company(
    *,
    company: Company,
    changes: Mapping[str, object],
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
) -> CompanyWriteResult:
    with transaction.atomic():
        current = Company.objects.select_for_update().get(pk=company.pk)
        normalized_changes = _normalize_company_changes(changes, company=current)
        before = _company_snapshot(current)
        changed_fields = _apply_changes(current, normalized_changes)
        if not changed_fields:
            return CompanyWriteResult(current, False, (), None)
        context = _resolve_write_context(
            target_type=DataChange.TargetType.COMPANY,
            target_id=current.pk,
            source_evidence=source_evidence,
            sync_run=sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
            field_names=changed_fields,
        )
        try:
            current.save(update_fields=(*changed_fields, "updated_at"))
        except IntegrityError as error:
            raise CompanyIdentityConflict(
                "Company update conflicts with an existing stable identity."
            ) from error
        after = _company_snapshot(current)
        data_changes = _record_data_changes(
            target_type=DataChange.TargetType.COMPANY,
            target_id=current.pk,
            before=before,
            after=after,
            changed_fields=changed_fields,
            context=context,
        )
        audit_record = _record_action(
            context=context,
            action=(
                AuditRecord.Action.MANUAL_CORRECTION
                if context.actor_user is not None
                else AuditRecord.Action.UPDATE
            ),
            target_type=AuditRecord.TargetType.COMPANY,
            target_id=current.pk,
            before=before,
            after=after,
        )
        return CompanyWriteResult(current, False, data_changes, audit_record)


def create_security_listing(
    *,
    company: Company,
    ticker: str,
    exchange: str,
    effective_from: date,
    effective_to: date | None = None,
    security_name: str | None = None,
    security_type: str = "unknown",
    share_class: str | None = None,
    is_primary: bool = False,
    source_evidence: SourceEvidence | None = None,
    listing_id: uuid.UUID | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
) -> SecurityListingWriteResult:
    values = _normalize_listing_values(
        ticker=ticker,
        exchange=exchange,
        effective_from=effective_from,
        effective_to=effective_to,
        security_name=security_name,
        security_type=security_type,
        share_class=share_class,
        is_primary=is_primary,
        source_evidence=source_evidence,
    )
    normalized_id = _normalize_optional_uuid(listing_id, value_name="listing_id")
    with transaction.atomic():
        current_company = Company.objects.select_for_update().get(pk=company.pk)
        existing = _find_exact_listing(company=current_company, values=values)
        if existing is not None:
            return SecurityListingWriteResult(existing, False, (), None)

        target_id = normalized_id or uuid.uuid4()
        context = _resolve_write_context(
            target_type=DataChange.TargetType.SECURITY_LISTING,
            target_id=target_id,
            source_evidence=source_evidence,
            sync_run=sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        values["source_evidence"] = context.source_evidence
        try:
            with transaction.atomic():
                listing = SecurityListing.objects.create(
                    id=target_id,
                    company=current_company,
                    **values,
                )
        except IntegrityError as error:
            existing = _find_exact_listing(company=current_company, values=values)
            if existing is not None:
                return SecurityListingWriteResult(existing, False, (), None)
            raise ListingIdentityConflict(
                "SecurityListing conflicts with an overlapping exchange and ticker interval."
            ) from error

        audit_record = _record_action(
            context=context,
            action=AuditRecord.Action.CREATE,
            target_type=AuditRecord.TargetType.SECURITY_LISTING,
            target_id=listing.pk,
            before={},
            after=_listing_snapshot(listing),
        )
        return SecurityListingWriteResult(listing, True, (), audit_record)


def update_security_listing(
    *,
    listing: SecurityListing,
    changes: Mapping[str, object],
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
) -> SecurityListingWriteResult:
    with transaction.atomic():
        current = SecurityListing.objects.select_for_update().get(pk=listing.pk)
        normalized_changes = _normalize_listing_changes(changes, listing=current)
        before = _listing_snapshot(current)
        changed_fields = _apply_changes(current, normalized_changes)
        if not changed_fields:
            return SecurityListingWriteResult(current, False, (), None)
        context = _resolve_write_context(
            target_type=DataChange.TargetType.SECURITY_LISTING,
            target_id=current.pk,
            source_evidence=source_evidence,
            sync_run=sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
            field_names=changed_fields,
        )
        try:
            current.save(update_fields=(*changed_fields, "updated_at"))
        except IntegrityError as error:
            raise ListingIdentityConflict(
                "SecurityListing update conflicts with an overlapping exchange and ticker interval."
            ) from error
        after = _listing_snapshot(current)
        data_changes = _record_data_changes(
            target_type=DataChange.TargetType.SECURITY_LISTING,
            target_id=current.pk,
            before=before,
            after=after,
            changed_fields=changed_fields,
            context=context,
        )
        audit_record = _record_action(
            context=context,
            action=(
                AuditRecord.Action.MANUAL_CORRECTION
                if context.actor_user is not None
                else AuditRecord.Action.UPDATE
            ),
            target_type=AuditRecord.TargetType.SECURITY_LISTING,
            target_id=current.pk,
            before=before,
            after=after,
        )
        return SecurityListingWriteResult(current, False, data_changes, audit_record)


def transition_security_listing(
    *,
    listing: SecurityListing,
    transition_date: date,
    ticker: str,
    exchange: str,
    security_name: str | None = None,
    security_type: str = "unknown",
    share_class: str | None = None,
    is_primary: bool = False,
    successor_effective_to: date | None = None,
    source_evidence: SourceEvidence | None = None,
    successor_source_evidence: SourceEvidence | None = None,
    successor_listing_id: uuid.UUID | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
) -> SecurityListingTransitionResult:
    """Close a listing at ``transition_date`` and create its successor atomically."""

    normalized_transition_date = _normalize_date(
        transition_date,
        value_name="transition_date",
    )
    if successor_source_evidence is not None and successor_listing_id is None:
        raise CompanyServiceError(
            "A successor SourceEvidence requires a preallocated successor_listing_id."
        )
    normalized_successor_id = (
        _normalize_optional_uuid(
            successor_listing_id,
            value_name="successor_listing_id",
        )
        or uuid.uuid4()
    )
    successor_values = _normalize_listing_values(
        ticker=ticker,
        exchange=exchange,
        effective_from=normalized_transition_date,
        effective_to=successor_effective_to,
        security_name=security_name,
        security_type=security_type,
        share_class=share_class,
        is_primary=is_primary,
        source_evidence=successor_source_evidence,
    )

    with transaction.atomic():
        current = (
            SecurityListing.objects.select_for_update().select_related("company").get(pk=listing.pk)
        )
        if normalized_transition_date <= current.effective_from:
            raise CompanyServiceError("transition_date must be later than effective_from.")

        effective_sync_run = sync_run
        if source_evidence is None and successor_source_evidence is not None and sync_run is None:
            effective_sync_run = _resolve_source_evidence(
                source_evidence=successor_source_evidence,
                sync_run=None,
                target_type=DataChange.TargetType.SECURITY_LISTING,
                target_id=normalized_successor_id,
                field_names=(),
            ).sync_run

        closing_context = _resolve_write_context(
            target_type=DataChange.TargetType.SECURITY_LISTING,
            target_id=current.pk,
            source_evidence=source_evidence,
            sync_run=effective_sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
            field_names=("effective_to",),
        )
        successor_context = _resolve_write_context(
            target_type=DataChange.TargetType.SECURITY_LISTING,
            target_id=normalized_successor_id,
            source_evidence=successor_source_evidence,
            sync_run=closing_context.sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        successor_values["source_evidence"] = successor_context.source_evidence

        existing_successor = _find_exact_listing(company=current.company, values=successor_values)
        if current.effective_to is not None:
            if current.effective_to != normalized_transition_date:
                raise ListingIdentityConflict(
                    "SecurityListing is already closed at a different transition date."
                )
            if existing_successor is None:
                raise ListingIdentityConflict(
                    "SecurityListing is already closed without the requested successor identity."
                )
            _assert_transition_audit_complete(
                prior_listing=current,
                successor_listing=existing_successor,
                transition_date=normalized_transition_date,
                closing_context=closing_context,
                successor_context=successor_context,
            )
            return SecurityListingTransitionResult(
                prior_listing=current,
                successor_listing=existing_successor,
                successor_created=False,
                data_changes=(),
                prior_audit_record=None,
                successor_audit_record=None,
            )
        if existing_successor is not None:
            raise ListingIdentityConflict(
                "SecurityListing successor already exists before the current listing is closed."
            )

        before = _listing_snapshot(current)
        current.effective_to = normalized_transition_date
        current.save(update_fields=("effective_to", "updated_at"))
        after = _listing_snapshot(current)
        data_changes = _record_data_changes(
            target_type=DataChange.TargetType.SECURITY_LISTING,
            target_id=current.pk,
            before=before,
            after=after,
            changed_fields=("effective_to",),
            context=closing_context,
        )
        prior_audit_record = _record_action(
            context=closing_context,
            action=(
                AuditRecord.Action.MANUAL_CORRECTION
                if closing_context.actor_user is not None
                else AuditRecord.Action.UPDATE
            ),
            target_type=AuditRecord.TargetType.SECURITY_LISTING,
            target_id=current.pk,
            before=before,
            after=after,
        )
        successor_result = create_security_listing(
            company=current.company,
            listing_id=normalized_successor_id,
            ticker=cast(str, successor_values["ticker"]),
            exchange=cast(str, successor_values["exchange"]),
            effective_from=cast(date, successor_values["effective_from"]),
            effective_to=cast(date | None, successor_values["effective_to"]),
            security_name=cast(str, successor_values["security_name"]),
            security_type=cast(str, successor_values["security_type"]),
            share_class=cast(str, successor_values["share_class"]),
            is_primary=cast(bool, successor_values["is_primary"]),
            source_evidence=cast(SourceEvidence | None, successor_values["source_evidence"]),
            sync_run=successor_context.sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        if not successor_result.created:
            raise ListingIdentityConflict(
                "SecurityListing successor creation did not create a new row."
            )
        return SecurityListingTransitionResult(
            prior_listing=current,
            successor_listing=successor_result.listing,
            successor_created=True,
            data_changes=data_changes,
            prior_audit_record=prior_audit_record,
            successor_audit_record=successor_result.audit_record,
        )


@dataclass(frozen=True, slots=True)
class _WriteContext:
    source_evidence: SourceEvidence | None
    sync_run: SyncRun | None
    actor_user: User | None
    reason: str
    request_id: str
    ip_address: str | None


def _normalize_company_values(**values: object) -> dict[str, object]:
    return {
        "legal_name": _normalize_required_text(
            values["legal_name"], value_name="legal_name", maximum_length=255
        ),
        "display_name": _normalize_required_text(
            values["display_name"], value_name="display_name", maximum_length=255
        ),
        "cik": normalize_cik(cast(str | int | None, values["cik"])),
        "country_code": _normalize_country_code(cast(str | None, values["country_code"])),
        "issuer_type": _normalize_choice(
            values["issuer_type"],
            allowed={value for value, _ in Company.IssuerType.choices},
            value_name="issuer_type",
        ),
        "fiscal_year_end_month_day": _normalize_month_day(
            cast(str | None, values["fiscal_year_end_month_day"])
        ),
        "investor_relations_url": _normalize_url(
            cast(str | None, values["investor_relations_url"])
        ),
        "monitoring_status": _normalize_choice(
            values["monitoring_status"],
            allowed={value for value, _ in Company.MonitoringStatus.choices},
            value_name="monitoring_status",
        ),
    }


def _normalize_company_changes(
    changes: Mapping[str, object],
    *,
    company: Company,
) -> dict[str, object]:
    allowed = {
        "legal_name",
        "display_name",
        "cik",
        "country_code",
        "issuer_type",
        "fiscal_year_end_month_day",
        "investor_relations_url",
        "monitoring_status",
    }
    _require_allowed_change_fields(changes, allowed=allowed, domain_name="Company")
    current: dict[str, object] = {
        "legal_name": company.legal_name,
        "display_name": company.display_name,
        "cik": company.cik,
        "country_code": company.country_code,
        "issuer_type": company.issuer_type,
        "fiscal_year_end_month_day": company.fiscal_year_end_month_day,
        "investor_relations_url": company.investor_relations_url,
        "monitoring_status": company.monitoring_status,
    }
    current.update(changes)
    normalized = _normalize_company_values(**current)
    return {field: normalized[field] for field in changes}


def _normalize_listing_values(**values: object) -> dict[str, object]:
    effective_from = _normalize_date(values["effective_from"], value_name="effective_from")
    effective_to = _normalize_optional_date(values["effective_to"], value_name="effective_to")
    if effective_to is not None and effective_to <= effective_from:
        raise CompanyServiceError("effective_to must be later than effective_from.")
    return {
        "ticker": normalize_ticker(cast(str, values["ticker"])),
        "exchange": normalize_exchange(cast(str, values["exchange"])),
        "effective_from": effective_from,
        "effective_to": effective_to,
        "security_name": _normalize_optional_text(
            cast(str | None, values["security_name"]),
            value_name="security_name",
            maximum_length=255,
        ),
        "security_type": _normalize_required_text(
            values["security_type"],
            value_name="security_type",
            maximum_length=64,
        ).lower(),
        "share_class": _normalize_optional_text(
            cast(str | None, values["share_class"]),
            value_name="share_class",
            maximum_length=32,
        ).upper(),
        "is_primary": _normalize_bool(values["is_primary"], value_name="is_primary"),
        "source_evidence": values["source_evidence"],
    }


def _normalize_listing_changes(
    changes: Mapping[str, object],
    *,
    listing: SecurityListing,
) -> dict[str, object]:
    historical_fields = {
        "company",
        "ticker",
        "exchange",
        "effective_from",
        "effective_to",
    }
    if historical_fields.intersection(changes):
        raise CompanyServiceError(
            "SecurityListing identity and effective period fields must be changed "
            "through transition_security_listing()."
        )
    allowed = {
        "security_name",
        "security_type",
        "share_class",
        "is_primary",
        "source_evidence",
    }
    _require_allowed_change_fields(changes, allowed=allowed, domain_name="SecurityListing")
    current: dict[str, object] = {
        "ticker": listing.ticker,
        "exchange": listing.exchange,
        "security_name": listing.security_name,
        "security_type": listing.security_type,
        "share_class": listing.share_class,
        "is_primary": listing.is_primary,
        "effective_from": listing.effective_from,
        "effective_to": listing.effective_to,
        "source_evidence": listing.source_evidence,
    }
    current.update(changes)
    normalized = _normalize_listing_values(**current)
    return {field: normalized[field] for field in changes}


def _find_company_identity(*, cik: str | None, company_id: uuid.UUID | None) -> Company | None:
    if cik is not None:
        return Company.objects.filter(cik=cik).first()
    if company_id is not None:
        return Company.objects.filter(pk=company_id).first()
    return None


def _find_exact_listing(
    *, company: Company, values: Mapping[str, object]
) -> SecurityListing | None:
    source_evidence = cast(SourceEvidence | None, values["source_evidence"])
    return SecurityListing.objects.filter(
        company=company,
        ticker=cast(str, values["ticker"]),
        exchange=cast(str, values["exchange"]),
        effective_from=cast(date, values["effective_from"]),
        effective_to=cast(date | None, values["effective_to"]),
        security_name=cast(str, values["security_name"]),
        security_type=cast(str, values["security_type"]),
        share_class=cast(str, values["share_class"]),
        is_primary=cast(bool, values["is_primary"]),
        source_evidence=source_evidence,
    ).first()


def _ensure_company_matches(company: Company, values: Mapping[str, object]) -> None:
    comparable = _company_snapshot(company)
    expected = {key: _serialize_company_value(key, value) for key, value in values.items()}
    if comparable != expected:
        raise CompanyIdentityConflict(
            "The stable Company identity already exists with different values; "
            "resolve it through an audited update."
        )


def _validate_write_context(
    *,
    target_type: str,
    target_id: uuid.UUID | None,
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    request_id: str,
    field_names: tuple[str, ...] = (),
) -> None:
    if source_evidence is not None and target_id is None:
        raise CompanyServiceError(
            "Creating from SourceEvidence requires a preallocated stable target UUID."
        )
    if actor_user is None and source_evidence is None and sync_run is None:
        raise CompanyServiceError("Automatic writes require a SyncRun or SourceEvidence.")
    if actor_user is not None and not reason.strip():
        raise CompanyServiceError("Manual writes require a non-empty reason.")
    if actor_user is not None and not request_id.strip():
        raise CompanyServiceError("Manual writes require a request_id.")
    if source_evidence is not None and target_id is not None:
        _resolve_source_evidence(
            source_evidence=source_evidence,
            sync_run=sync_run,
            target_type=target_type,
            target_id=target_id,
            field_names=field_names,
        )


def _resolve_write_context(
    *,
    target_type: str,
    target_id: uuid.UUID,
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    request_id: str,
    ip_address: str | None,
    field_names: tuple[str, ...] = (),
) -> _WriteContext:
    _validate_write_context(
        target_type=target_type,
        target_id=target_id,
        source_evidence=source_evidence,
        sync_run=sync_run,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        field_names=field_names,
    )
    evidence_reference = (
        _resolve_source_evidence(
            source_evidence=source_evidence,
            sync_run=sync_run,
            target_type=target_type,
            target_id=target_id,
            field_names=field_names,
        )
        if source_evidence is not None
        else None
    )
    persisted_evidence = evidence_reference.evidence if evidence_reference is not None else None
    derived_sync_run = (
        evidence_reference.sync_run
        if evidence_reference is not None
        else _resolve_sync_run(sync_run)
    )
    if actor_user is None and derived_sync_run is None:
        raise CompanyServiceError("Automatic writes require a SyncRun or SourceEvidence.")
    if request_id.strip():
        normalized_request_id = request_id.strip()
    else:
        if derived_sync_run is None:
            raise CompanyServiceError("Automatic audit records require a SyncRun.")
        normalized_request_id = f"sync-run:{derived_sync_run.pk}"
    return _WriteContext(
        source_evidence=persisted_evidence,
        sync_run=derived_sync_run,
        actor_user=actor_user,
        reason=reason.strip(),
        request_id=normalized_request_id,
        ip_address=ip_address,
    )


def _resolve_source_evidence(
    source_evidence: SourceEvidence,
    *,
    sync_run: SyncRun | None,
    target_type: str,
    target_id: uuid.UUID,
    field_names: tuple[str, ...],
) -> SourceEvidenceReference:
    try:
        return resolve_source_evidence_reference(
            source_evidence=source_evidence,
            sync_run=sync_run,
            target_type=target_type,
            target_id=target_id,
            field_names=field_names,
        )
    except InvalidEvidenceReference as error:
        raise CompanyServiceError(str(error)) from None


def _resolve_sync_run(sync_run: SyncRun | None) -> SyncRun | None:
    if sync_run is None:
        return None
    if sync_run._state.adding or sync_run.pk is None:
        raise CompanyServiceError("sync_run must be saved before use.")
    try:
        return SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise CompanyServiceError("sync_run must exist before use.") from error


def _record_data_changes(
    *,
    target_type: str,
    target_id: uuid.UUID,
    before: Mapping[str, object],
    after: Mapping[str, object],
    changed_fields: tuple[str, ...],
    context: _WriteContext,
) -> tuple[DataChangeWriteResult, ...]:
    results: list[DataChangeWriteResult] = []
    for field_name in changed_fields:
        evidence = context.source_evidence
        if evidence is not None and evidence.field_name not in ("", field_name):
            evidence = None
        results.append(
            record_data_change(
                target_type=target_type,
                target_id=target_id,
                field_name=field_name,
                old_value=before[field_name],
                new_value=after[field_name],
                rule_version=IDENTITY_RULE_VERSION,
                source_evidence=evidence,
                sync_run=context.sync_run,
                actor_user=context.actor_user,
                reason=context.reason,
                origin_key=context.request_id if context.actor_user is not None else "",
            )
        )
    return tuple(results)


def _assert_transition_audit_complete(
    *,
    prior_listing: SecurityListing,
    successor_listing: SecurityListing,
    transition_date: date,
    closing_context: _WriteContext,
    successor_context: _WriteContext,
) -> None:
    """Reject an idempotent transition replay whose append-only history is incomplete.

    A successful first transition can only close an open listing, so its recorded
    ``effective_to`` change is canonically ``null -> transition_date``.  This
    function intentionally only reads persisted history: a partial or altered
    legacy history requires manual review rather than a fabricated backfill.
    """

    prior_after = _listing_snapshot(prior_listing)
    prior_before = {**prior_after, "effective_to": None}
    expected_data_change_origin = _data_change_origin_key(closing_context)
    expected_transition_value = transition_date.isoformat()
    if prior_after["effective_to"] != expected_transition_value:
        _raise_incomplete_transition_audit()

    data_change_candidates = DataChange.objects.filter(
        target_type=DataChange.TargetType.SECURITY_LISTING,
        target_id=prior_listing.pk,
        field_name="effective_to",
        source_evidence=closing_context.source_evidence,
        sync_run=closing_context.sync_run,
        actor_user=closing_context.actor_user,
        reason=closing_context.reason,
        origin_key=expected_data_change_origin,
        rule_version=IDENTITY_RULE_VERSION,
    )
    data_change_exists = any(
        change.old_value == prior_before["effective_to"]
        and change.new_value == expected_transition_value
        for change in data_change_candidates
    )
    if not data_change_exists:
        _raise_incomplete_transition_audit()

    closing_action = (
        AuditRecord.Action.MANUAL_CORRECTION
        if closing_context.actor_user is not None
        else AuditRecord.Action.UPDATE
    )
    prior_audit_candidates = AuditRecord.objects.filter(
        actor_user=closing_context.actor_user,
        sync_run=closing_context.sync_run,
        action=closing_action,
        target_type=AuditRecord.TargetType.SECURITY_LISTING,
        target_id=prior_listing.pk,
        reason=closing_context.reason,
        request_id=closing_context.request_id,
    )
    prior_audit_exists = any(
        record.before == prior_before and record.after == prior_after
        for record in prior_audit_candidates
    )
    if not prior_audit_exists:
        _raise_incomplete_transition_audit()

    successor_audit_candidates = AuditRecord.objects.filter(
        actor_user=successor_context.actor_user,
        sync_run=successor_context.sync_run,
        action=AuditRecord.Action.CREATE,
        target_type=AuditRecord.TargetType.SECURITY_LISTING,
        target_id=successor_listing.pk,
        reason=successor_context.reason,
        request_id=successor_context.request_id,
    )
    successor_audit_exists = any(
        record.before == {} and record.after == _listing_snapshot(successor_listing)
        for record in successor_audit_candidates
    )
    if not successor_audit_exists:
        _raise_incomplete_transition_audit()


def _data_change_origin_key(context: _WriteContext) -> str:
    if context.actor_user is not None:
        return context.request_id
    if context.source_evidence is not None:
        return f"source_evidence:{context.source_evidence.pk}"
    if context.sync_run is not None:
        return f"sync_run:{context.sync_run.pk}"
    raise CompanyServiceError("Automatic DataChange records require a SyncRun or SourceEvidence.")


def _raise_incomplete_transition_audit() -> None:
    raise ListingIdentityConflict(
        "SecurityListing transition data exists but its audit history is incomplete or "
        "inconsistent; manual review is required."
    )


def _record_action(
    *,
    context: _WriteContext,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> AuditRecord:
    if context.actor_user is not None:
        result = record_user_action(
            actor_user=context.actor_user,
            sync_run=context.sync_run,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before=dict(before),
            after=dict(after),
            reason=context.reason,
            request_id=context.request_id,
            ip_address=context.ip_address,
        )
    else:
        if context.sync_run is None:
            raise CompanyServiceError("Automatic audit records require a SyncRun.")
        result = record_system_action(
            sync_run=context.sync_run,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before=dict(before),
            after=dict(after),
            request_id=context.request_id,
        )
    return result.record


def _company_snapshot(company: Company) -> dict[str, object]:
    return {
        "legal_name": company.legal_name,
        "display_name": company.display_name,
        "cik": company.cik,
        "country_code": company.country_code or None,
        "issuer_type": company.issuer_type,
        "fiscal_year_end_month_day": company.fiscal_year_end_month_day or None,
        "investor_relations_url": company.investor_relations_url or None,
        "monitoring_status": company.monitoring_status,
    }


def _listing_snapshot(listing: SecurityListing) -> dict[str, object]:
    return {
        "ticker": listing.ticker,
        "exchange": listing.exchange,
        "security_name": listing.security_name or None,
        "security_type": listing.security_type,
        "share_class": listing.share_class or None,
        "is_primary": listing.is_primary,
        "effective_from": listing.effective_from.isoformat(),
        "effective_to": listing.effective_to.isoformat() if listing.effective_to else None,
        "source_evidence": str(listing.source_evidence_id) if listing.source_evidence_id else None,
    }


def _apply_changes(
    instance: Company | SecurityListing, changes: Mapping[str, object]
) -> tuple[str, ...]:
    changed: list[str] = []
    for field_name, value in changes.items():
        if getattr(instance, field_name) != value:
            setattr(instance, field_name, value)
            changed.append(field_name)
    return tuple(changed)


def _serialize_value(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, SourceEvidence):
        return str(value.pk)
    return value


def _serialize_company_value(field_name: str, value: object) -> object:
    if field_name in {"country_code", "fiscal_year_end_month_day", "investor_relations_url"}:
        return value or None
    return _serialize_value(value)


def _normalize_required_text(value: object, *, value_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise CompanyServiceError(f"{value_name} must be text.")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum_length:
        raise CompanyServiceError(f"{value_name} must contain 1 to {maximum_length} characters.")
    _ensure_safe_value(normalized, value_name=value_name)
    return normalized


def _normalize_optional_text(value: str | None, *, value_name: str, maximum_length: int) -> str:
    if value is None or value == "":
        return ""
    return _normalize_required_text(value, value_name=value_name, maximum_length=maximum_length)


def _normalize_country_code(value: str | None) -> str:
    if value is None or value == "":
        return ""
    normalized = _normalize_required_text(
        value, value_name="country_code", maximum_length=2
    ).upper()
    if not _COUNTRY_CODE_RE.fullmatch(normalized):
        raise CompanyServiceError("country_code must be a two-letter ISO code.")
    return normalized


def _normalize_month_day(value: str | None) -> str:
    if value is None or value == "":
        return ""
    normalized = _normalize_required_text(
        value, value_name="fiscal_year_end_month_day", maximum_length=5
    )
    if not _MONTH_DAY_RE.fullmatch(normalized):
        raise CompanyServiceError("fiscal_year_end_month_day must use MM-DD format.")
    try:
        date.fromisoformat(f"2000-{normalized}")
    except ValueError as error:
        raise CompanyServiceError("fiscal_year_end_month_day must be a calendar date.") from error
    return normalized


def _normalize_url(value: str | None) -> str:
    if value is None or value == "":
        return ""
    normalized = _normalize_required_text(
        value, value_name="investor_relations_url", maximum_length=2048
    )
    try:
        Company._meta.get_field("investor_relations_url").run_validators(normalized)
    except ValidationError as error:
        raise CompanyServiceError("investor_relations_url must be a safe HTTP(S) URL.") from error
    return normalized


def _normalize_choice(value: object, *, allowed: set[str], value_name: str) -> str:
    normalized = _normalize_required_text(value, value_name=value_name, maximum_length=32).lower()
    if normalized not in allowed:
        raise CompanyServiceError(f"{value_name} must be a supported value.")
    return normalized


def _normalize_date(value: object, *, value_name: str) -> date:
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    raise CompanyServiceError(f"{value_name} must be a date.")


def _normalize_optional_date(value: object, *, value_name: str) -> date | None:
    if value is None:
        return None
    return _normalize_date(value, value_name=value_name)


def _normalize_bool(value: object, *, value_name: str) -> bool:
    if not isinstance(value, bool):
        raise CompanyServiceError(f"{value_name} must be boolean.")
    return value


def _normalize_optional_uuid(value: uuid.UUID | None, *, value_name: str) -> uuid.UUID | None:
    if value is None:
        return None
    if not isinstance(value, uuid.UUID):
        raise CompanyServiceError(f"{value_name} must be a UUID.")
    return value


def _ensure_safe_value(value: str, *, value_name: str) -> None:
    try:
        normalize_json_without_credentials(value, value_name=value_name)
    except (InvalidAuditValue, SensitiveAuditData) as error:
        raise CompanyServiceError(str(error)) from None


def _require_allowed_change_fields(
    changes: Mapping[str, object],
    *,
    allowed: set[str],
    domain_name: str,
) -> None:
    if not changes:
        raise CompanyServiceError(f"{domain_name} changes must not be empty.")
    unexpected = set(changes) - allowed
    if unexpected:
        raise CompanyServiceError(f"{domain_name} changes include unsupported fields.")
