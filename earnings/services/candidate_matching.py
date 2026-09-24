"""Deterministic Company matching and candidate creation for earnings observations."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from django.db import IntegrityError, transaction
from django.db.models import Q

from audit.models import (
    AuditRecord,
    RawDataObservation,
    SourceEvidence,
    SyncRun,
)
from audit.services import record_source_evidence, record_system_action
from companies.models import Company, SecurityListing
from companies.services import CompanyServiceError, normalize_cik
from earnings.models import (
    EarningsCalendarObservation,
    EarningsDatePrecision,
    EarningsEvent,
    EarningsReconciliationDecision,
    FiscalCalendarType,
    IdentityStatus,
    MonitoringPoolMember,
    MonitoringPoolSnapshot,
    PeriodType,
    ReleaseSession,
)
from earnings.services.calendar_pagination import EARNINGS_CALENDAR_WINDOW_JOB_TYPE
from earnings.services.calendar_run_ownership import owned_calendar_run
from earnings.services.date_changes import update_earnings_schedule
from earnings.services.monitoring_pool import (
    MonitoringPoolSnapshotReference,
    resolve_monitoring_pool_snapshot,
)
from earnings.services.reconciliation import (
    build_earnings_reconciliation_decision_key,
    record_earnings_reconciliation_decision,
)

EARNINGS_COMPANY_MATCHER_VERSION = "earnings-company-match-v1"

_CANDIDATE_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://earnings-radar.example/company-match/v1",
)
_MATCH_FACTOR_NAMESPACE = "company_match"


class CompanyMatchOutcome(StrEnum):
    MATCHED = "MATCHED"
    UNMATCHED = "UNMATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    OUT_OF_POOL = "OUT_OF_POOL"


class CandidateMatchingError(ValueError):
    """Base class for invalid candidate matching input."""


class InvalidCandidateMatchingInput(CandidateMatchingError):
    """Raised when persistence context or matching input is not usable."""


class UnknownCompanyMatcherVersion(CandidateMatchingError):
    """Raised when the requested matcher contract version is not supported."""


class CandidateMatchingIntegrityError(RuntimeError):
    """Raised when persisted matching facts violate the ratified contract."""


@dataclass(frozen=True, slots=True)
class CandidateCreationResult:
    sync_run: SyncRun
    observation: EarningsCalendarObservation
    snapshot: MonitoringPoolSnapshot
    outcome: CompanyMatchOutcome
    matcher_version: str
    matching_input_revision: str
    match_execution_key: str
    match_result_key: str
    decision: EarningsReconciliationDecision
    candidate: EarningsEvent | None
    source_evidence: SourceEvidence | None
    decision_created: bool
    candidate_created: bool


@dataclass(frozen=True, slots=True)
class _NormalizedHints:
    cik: str
    ticker: str
    exchange: str
    provider_symbol: str

    def as_evidence(self) -> dict[str, str]:
        return {
            "cik": self.cik,
            "ticker": self.ticker,
            "exchange": self.exchange,
            "provider_symbol": self.provider_symbol,
        }


@dataclass(frozen=True, slots=True)
class _MatchFacts:
    snapshot: MonitoringPoolSnapshot
    hints: _NormalizedHints
    pool_companies: tuple[Company, ...]
    pool_listings: tuple[SecurityListing, ...]
    cik_companies: tuple[Company, ...]
    listing_matches: tuple[SecurityListing, ...]
    matching_input_revision: str


@dataclass(frozen=True, slots=True)
class _MatchEvaluation:
    outcome: CompanyMatchOutcome
    strategy: str
    reason_code: str
    matched_company: Company | None
    matched_listings: tuple[SecurityListing, ...]
    exact_companies: tuple[Company, ...]
    outside_pool_companies: tuple[Company, ...]
    outside_pool_listings: tuple[SecurityListing, ...]
    match_result_key: str
    candidate_identity_key: str | None


@owned_calendar_run
def create_earnings_candidate_for_observation(
    *,
    sync_run: SyncRun,
    observation: EarningsCalendarObservation,
    matcher_version: str = EARNINGS_COMPANY_MATCHER_VERSION,
) -> CandidateCreationResult:
    """Match one normalized observation and persist its candidate lineage."""

    current_run = _load_sync_run(sync_run)
    current_observation = _load_observation(observation)
    normalized_matcher_version = _normalize_matcher_version(matcher_version)
    _validate_observation_run_link(
        sync_run=current_run,
        observation=current_observation,
    )

    snapshot_reference = resolve_monitoring_pool_snapshot(current_run)
    facts = _build_match_facts(
        sync_run=current_run,
        observation=current_observation,
        snapshot_reference=snapshot_reference,
        matcher_version=normalized_matcher_version,
    )
    match_execution_key = _build_match_execution_key(
        sync_run=current_run,
        observation=current_observation,
        snapshot=facts.snapshot,
        matching_input_revision=facts.matching_input_revision,
        matcher_version=normalized_matcher_version,
    )
    evaluation = _evaluate_match(
        facts,
        match_execution_key=match_execution_key,
    )
    match_factors = _build_match_factors(
        sync_run=current_run,
        observation=current_observation,
        facts=facts,
        evaluation=evaluation,
        matcher_version=normalized_matcher_version,
        match_execution_key=match_execution_key,
    )
    decision_type, decision_status = _decision_values_for_outcome(evaluation.outcome)
    candidate_id = (
        _candidate_uuid(evaluation.candidate_identity_key)
        if evaluation.candidate_identity_key is not None
        else None
    )
    decision_key = build_earnings_reconciliation_decision_key(
        observation_id=current_observation.pk,
        decision_type=decision_type,
        status=decision_status,
        target_event_id=candidate_id,
        covered_fields=(),
        match_factors=match_factors,
        rule_version=normalized_matcher_version,
        supersedes_id=None,
        actor_user_id=None,
        request_id="",
    )
    existing = _find_decision(decision_key)
    if existing is not None:
        return _reuse_existing_result(
            sync_run=current_run,
            observation=current_observation,
            snapshot=facts.snapshot,
            outcome=evaluation.outcome,
            matcher_version=normalized_matcher_version,
            matching_input_revision=facts.matching_input_revision,
            match_execution_key=match_execution_key,
            match_result_key=evaluation.match_result_key,
            candidate_id=candidate_id,
            match_factors=match_factors,
            decision=existing,
        )

    if evaluation.outcome is CompanyMatchOutcome.MATCHED:
        assert candidate_id is not None
        assert evaluation.matched_company is not None
        return _create_matched_candidate(
            sync_run=current_run,
            observation=current_observation,
            snapshot=facts.snapshot,
            hints=facts.hints,
            evaluation=evaluation,
            matcher_version=normalized_matcher_version,
            matching_input_revision=facts.matching_input_revision,
            match_execution_key=match_execution_key,
            match_factors=match_factors,
            decision_key=decision_key,
            candidate_id=candidate_id,
        )

    return _create_non_match_decision(
        sync_run=current_run,
        observation=current_observation,
        snapshot=facts.snapshot,
        outcome=evaluation.outcome,
        matcher_version=normalized_matcher_version,
        matching_input_revision=facts.matching_input_revision,
        match_execution_key=match_execution_key,
        match_result_key=evaluation.match_result_key,
        match_factors=match_factors,
        decision_type=decision_type,
        decision_status=decision_status,
        decision_key=decision_key,
        reason_code=evaluation.reason_code,
    )


def _load_sync_run(sync_run: SyncRun) -> SyncRun:
    if not isinstance(sync_run, SyncRun) or sync_run._state.adding or sync_run.pk is None:
        raise InvalidCandidateMatchingInput("sync_run must be a persisted SyncRun.")
    current = SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    if current.job_type != EARNINGS_CALENDAR_WINDOW_JOB_TYPE:
        raise InvalidCandidateMatchingInput("sync_run has the wrong job type.")
    if current.status != SyncRun.Status.RUNNING:
        raise InvalidCandidateMatchingInput("sync_run must still be running.")
    return current


def _load_observation(
    observation: EarningsCalendarObservation,
) -> EarningsCalendarObservation:
    if (
        not isinstance(observation, EarningsCalendarObservation)
        or observation._state.adding
        or observation.pk is None
    ):
        raise InvalidCandidateMatchingInput("observation must be a persisted observation.")
    try:
        return EarningsCalendarObservation.objects.get(pk=observation.pk)
    except EarningsCalendarObservation.DoesNotExist as error:
        raise InvalidCandidateMatchingInput("observation no longer exists.") from error


def _normalize_matcher_version(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidCandidateMatchingInput("matcher_version must be a string.")
    normalized = value.strip()
    if normalized != EARNINGS_COMPANY_MATCHER_VERSION:
        raise UnknownCompanyMatcherVersion(f"Unsupported company matcher version: {value!r}.")
    return normalized


def _validate_observation_run_link(
    *,
    sync_run: SyncRun,
    observation: EarningsCalendarObservation,
) -> None:
    if observation.source_id != sync_run.source_id:
        raise InvalidCandidateMatchingInput(
            "observation and sync_run must belong to the same DataSource."
        )
    if not RawDataObservation.objects.filter(
        sync_run=sync_run,
        raw_data_record=observation.raw_data_record,
    ).exists():
        raise InvalidCandidateMatchingInput(
            "sync_run must have observed the observation raw record."
        )


def _build_match_facts(
    *,
    sync_run: SyncRun,
    observation: EarningsCalendarObservation,
    snapshot_reference: MonitoringPoolSnapshotReference,
    matcher_version: str,
) -> _MatchFacts:
    snapshot = snapshot_reference.snapshot
    hints = _normalize_hints(observation)
    pool_companies, pool_listings = _load_pool_facts(snapshot_reference)
    cik_companies = _load_exact_cik_companies(
        normalized_cik=hints.cik,
    )
    listing_matches = _load_exact_listing_matches(
        ticker=hints.ticker,
        exchange=hints.exchange,
        as_of=snapshot.as_of_date,
    )
    matching_input_revision = _build_matching_input_revision(
        sync_run=sync_run,
        observation=observation,
        snapshot=snapshot,
        hints=hints,
        pool_companies=pool_companies,
        pool_listings=pool_listings,
        cik_companies=cik_companies,
        listing_matches=listing_matches,
        matcher_version=matcher_version,
    )
    return _MatchFacts(
        snapshot=snapshot,
        hints=hints,
        pool_companies=pool_companies,
        pool_listings=pool_listings,
        cik_companies=cik_companies,
        listing_matches=listing_matches,
        matching_input_revision=matching_input_revision,
    )


def _normalize_hints(observation: EarningsCalendarObservation) -> _NormalizedHints:
    try:
        cik = normalize_cik(observation.cik) or ""
    except CompanyServiceError as error:
        raise InvalidCandidateMatchingInput("observation CIK is invalid.") from error
    return _NormalizedHints(
        cik=cik,
        ticker=observation.ticker.strip().upper(),
        exchange=observation.exchange.strip().upper(),
        provider_symbol=observation.provider_symbol.strip(),
    )


def _load_pool_facts(
    snapshot_reference: MonitoringPoolSnapshotReference,
) -> tuple[tuple[Company, ...], tuple[SecurityListing, ...]]:
    snapshot = snapshot_reference.snapshot
    member_company_ids = {member.company_id for member in snapshot_reference.members}
    companies = tuple(Company.objects.filter(id__in=member_company_ids).order_by("id"))
    companies_by_id = {company.pk: company for company in companies}
    if set(companies_by_id) != member_company_ids:
        raise CandidateMatchingIntegrityError(
            "Monitoring pool snapshot references a missing member Company."
        )

    basis_listing_ids = _basis_listing_ids(snapshot_reference.members)
    basis_listings = {
        listing.pk: listing for listing in SecurityListing.objects.filter(id__in=basis_listing_ids)
    }
    if set(basis_listings) != basis_listing_ids:
        raise CandidateMatchingIntegrityError(
            "Monitoring pool snapshot references a missing SecurityListing."
        )

    active_listings = tuple(
        SecurityListing.objects.filter(
            company_id__in=member_company_ids,
            effective_from__lte=snapshot.as_of_date,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=snapshot.as_of_date))
        .order_by("company_id", "id")
    )
    active_by_company: dict[uuid.UUID, list[SecurityListing]] = {
        company_id: [] for company_id in member_company_ids
    }
    for listing in active_listings:
        active_by_company[listing.company_id].append(listing)

    for member in snapshot_reference.members:
        if not active_by_company[member.company_id]:
            raise CandidateMatchingIntegrityError(
                "Monitoring pool member has no as-of active SecurityListing."
            )
        for basis in _validated_basis(member, snapshot.as_of_date):
            basis_listing = basis_listings.get(basis["security_listing_id"])
            if basis_listing is None or basis_listing.company_id != member.company_id:
                raise CandidateMatchingIntegrityError(
                    "Monitoring pool basis does not match its member Company."
                )
    return companies, active_listings


def _basis_listing_ids(members: tuple[MonitoringPoolMember, ...]) -> set[uuid.UUID]:
    listing_ids: set[uuid.UUID] = set()
    for member in members:
        for basis in _validated_basis(member, as_of=None):
            listing_ids.add(basis["security_listing_id"])
    return listing_ids


def _validated_basis(
    member: MonitoringPoolMember,
    as_of: date | None,
) -> list[dict[str, uuid.UUID]]:
    value = member.basis
    if not isinstance(value, list) or not value:
        raise CandidateMatchingIntegrityError("Monitoring pool basis is empty.")
    result: list[dict[str, uuid.UUID]] = []
    for row in value:
        if not isinstance(row, dict):
            raise CandidateMatchingIntegrityError("Monitoring pool basis row is invalid.")
        try:
            listing_id = uuid.UUID(str(row["security_listing_id"]))
            effective_from = date.fromisoformat(str(row["effective_from"]))
            effective_to_raw = row.get("effective_to")
            effective_to = (
                date.fromisoformat(str(effective_to_raw)) if effective_to_raw is not None else None
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CandidateMatchingIntegrityError(
                "Monitoring pool basis temporal identity is invalid."
            ) from error
        if effective_to is not None and effective_to <= effective_from:
            raise CandidateMatchingIntegrityError("Monitoring pool basis interval is invalid.")
        if as_of is not None and not (
            effective_from <= as_of and (effective_to is None or as_of < effective_to)
        ):
            raise CandidateMatchingIntegrityError(
                "Monitoring pool basis is not effective at snapshot as-of."
            )
        result.append(
            {
                "security_listing_id": listing_id,
            }
        )
    return result


def _load_exact_cik_companies(*, normalized_cik: str) -> tuple[Company, ...]:
    if not normalized_cik:
        return ()
    return tuple(Company.objects.filter(cik=normalized_cik).order_by("id"))


def _load_exact_listing_matches(
    *,
    ticker: str,
    exchange: str,
    as_of: date,
) -> tuple[SecurityListing, ...]:
    if not ticker or not exchange:
        return ()
    return tuple(
        SecurityListing.objects.select_related("company")
        .filter(
            ticker=ticker,
            exchange=exchange,
            effective_from__lte=as_of,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=as_of))
        .order_by("company_id", "id")
    )


def _build_matching_input_revision(
    *,
    sync_run: SyncRun,
    observation: EarningsCalendarObservation,
    snapshot: MonitoringPoolSnapshot,
    hints: _NormalizedHints,
    pool_companies: tuple[Company, ...],
    pool_listings: tuple[SecurityListing, ...],
    cik_companies: tuple[Company, ...],
    listing_matches: tuple[SecurityListing, ...],
    matcher_version: str,
) -> str:
    payload = {
        "matcher_version": matcher_version,
        "observation": {
            "id": str(observation.pk),
            "provider_key": observation.provider_key,
            "provider_event_id": observation.provider_event_id,
            "raw_data_record_id": str(observation.raw_data_record_id),
            "source_id": str(sync_run.source_id),
            "cik": hints.cik,
            "ticker": hints.ticker,
            "exchange": hints.exchange,
        },
        "snapshot": {
            "as_of": snapshot.as_of_date.isoformat(),
            "selector_version": snapshot.selector_version,
            "pool_hash": snapshot.pool_hash,
            "input_revision": snapshot.input_revision,
        },
        "pool_companies": sorted(
            (
                {"company_id": str(company.pk), "cik": company.cik or ""}
                for company in pool_companies
            ),
            key=lambda row: row["company_id"],
        ),
        "pool_listings": sorted(
            (_listing_fact(listing) for listing in pool_listings),
            key=lambda row: (row["company_id"], row["security_listing_id"]),
        ),
        "exact_external_cik_companies": sorted(
            (
                {"company_id": str(company.pk), "cik": company.cik or ""}
                for company in cik_companies
            ),
            key=lambda row: row["company_id"],
        ),
        "exact_external_listing_matches": sorted(
            (_listing_fact(listing) for listing in listing_matches),
            key=lambda row: (row["company_id"], row["security_listing_id"]),
        ),
    }
    return _sha256_json(payload)


def _listing_fact(listing: SecurityListing) -> dict[str, str | None]:
    return {
        "company_id": str(listing.company_id),
        "security_listing_id": str(listing.pk),
        "ticker": listing.ticker,
        "exchange": listing.exchange,
        "effective_from": listing.effective_from.isoformat(),
        "effective_to": listing.effective_to.isoformat() if listing.effective_to else None,
    }


def _evaluate_match(
    facts: _MatchFacts,
    *,
    match_execution_key: str,
) -> _MatchEvaluation:
    cik_companies = tuple(facts.cik_companies)
    listing_companies: dict[uuid.UUID, Company] = {
        listing.company.pk: listing.company for listing in facts.listing_matches
    }
    exact_companies = _dedupe_companies((*cik_companies, *listing_companies.values()))
    exact_company_ids = {company.pk for company in exact_companies}
    pool_company_ids = {company.pk for company in facts.pool_companies}

    if len(exact_company_ids) > 1:
        outcome = CompanyMatchOutcome.AMBIGUOUS
        strategy = _matching_strategy(
            cik_present=bool(cik_companies),
            listing_present=bool(facts.listing_matches),
            multiple=True,
        )
        reason_code = "multiple_exact_companies"
        matched_company = None
        matched_listings: tuple[SecurityListing, ...] = ()
    elif len(exact_company_ids) == 1:
        company = exact_companies[0]
        if company.pk not in pool_company_ids:
            outcome = CompanyMatchOutcome.OUT_OF_POOL
            strategy = _matching_strategy(
                cik_present=bool(cik_companies),
                listing_present=bool(facts.listing_matches),
                multiple=False,
            )
            reason_code = "exact_match_outside_pool"
            matched_company = None
            matched_listings = ()
        else:
            outcome = CompanyMatchOutcome.MATCHED
            strategy = _matching_strategy(
                cik_present=bool(cik_companies),
                listing_present=bool(facts.listing_matches),
                multiple=False,
            )
            reason_code = "exact_match_in_pool"
            matched_company = company
            matched_listings = tuple(
                listing for listing in facts.listing_matches if listing.company_id == company.pk
            )
    else:
        outcome = CompanyMatchOutcome.UNMATCHED
        strategy = "none"
        reason_code = "no_exact_match"
        matched_company = None
        matched_listings = ()

    outside_pool_companies = tuple(
        company for company in exact_companies if company.pk not in pool_company_ids
    )
    outside_pool_company_ids = {company.pk for company in outside_pool_companies}
    outside_pool_listings = tuple(
        listing
        for listing in facts.listing_matches
        if listing.company_id in outside_pool_company_ids
    )
    match_result_payload = {
        "match_execution_key": match_execution_key,
        "outcome": outcome.value,
        "exact_company_ids": sorted(str(company.pk) for company in exact_companies),
        "matched_company_id": str(matched_company.pk) if matched_company is not None else None,
        "matched_security_listing_ids": sorted(str(listing.pk) for listing in matched_listings),
        "outside_pool_company_ids": sorted(str(company.pk) for company in outside_pool_companies),
        "outside_pool_security_listing_ids": sorted(
            str(listing.pk) for listing in outside_pool_listings
        ),
    }
    match_result_key = _sha256_json(match_result_payload)
    candidate_identity_key = None
    if outcome is CompanyMatchOutcome.MATCHED:
        assert matched_company is not None
        candidate_identity_key = _sha256_json(
            {
                "match_execution_key": match_result_payload["match_execution_key"],
                "matched_company_id": str(matched_company.pk),
                "matched_security_listing_ids": match_result_payload[
                    "matched_security_listing_ids"
                ],
            }
        )
    return _MatchEvaluation(
        outcome=outcome,
        strategy=strategy,
        reason_code=reason_code,
        matched_company=matched_company,
        matched_listings=matched_listings,
        exact_companies=exact_companies,
        outside_pool_companies=outside_pool_companies,
        outside_pool_listings=outside_pool_listings,
        match_result_key=match_result_key,
        candidate_identity_key=candidate_identity_key,
    )


def _matching_strategy(
    *,
    cik_present: bool,
    listing_present: bool,
    multiple: bool,
) -> str:
    if multiple:
        return "multiple"
    if cik_present and listing_present:
        return "cik+exchange_ticker"
    if cik_present:
        return "cik"
    if listing_present:
        return "exchange_ticker"
    return "none"


def _dedupe_companies(companies: tuple[Company, ...]) -> tuple[Company, ...]:
    by_id: dict[uuid.UUID, Company] = {}
    for company in companies:
        by_id.setdefault(company.pk, company)
    return tuple(by_id[company_id] for company_id in sorted(by_id, key=str))


def _build_match_execution_key(
    *,
    sync_run: SyncRun,
    observation: EarningsCalendarObservation,
    snapshot: MonitoringPoolSnapshot,
    matching_input_revision: str,
    matcher_version: str,
) -> str:
    return _sha256_json(
        {
            "matcher_version": matcher_version,
            "observation": {
                "id": str(observation.pk),
                "provider_key": observation.provider_key,
                "provider_event_id": observation.provider_event_id,
                "raw_data_record_id": str(observation.raw_data_record_id),
                "source_id": str(sync_run.source_id),
            },
            "snapshot": {
                "as_of": snapshot.as_of_date.isoformat(),
                "selector_version": snapshot.selector_version,
                "pool_hash": snapshot.pool_hash,
            },
            "matching_input_revision": matching_input_revision,
        }
    )


def _build_match_factors(
    *,
    sync_run: SyncRun,
    observation: EarningsCalendarObservation,
    facts: _MatchFacts,
    evaluation: _MatchEvaluation,
    matcher_version: str,
    match_execution_key: str,
) -> dict[str, object]:
    return {
        _MATCH_FACTOR_NAMESPACE: {
            "matcher_version": matcher_version,
            "match_execution_key": match_execution_key,
            "matching_input_revision": facts.matching_input_revision,
            "match_result_key": evaluation.match_result_key,
            "match_status": evaluation.outcome.value,
            "match_strategy": evaluation.strategy,
            "monitoring_pool_as_of": facts.snapshot.as_of_date.isoformat(),
            "monitoring_pool_snapshot_id": str(facts.snapshot.pk),
            "monitoring_pool_hash": facts.snapshot.pool_hash,
            "selector_version": facts.snapshot.selector_version,
            "snapshot_input_revision": facts.snapshot.input_revision,
            "observation": {
                "id": str(observation.pk),
                "provider_key": observation.provider_key,
                "provider_event_id": observation.provider_event_id,
                "raw_data_record_id": str(observation.raw_data_record_id),
                "source_id": str(sync_run.source_id),
            },
            "normalized_cik": facts.hints.cik,
            "normalized_ticker": facts.hints.ticker,
            "normalized_exchange": facts.hints.exchange,
            "provider_symbol": facts.hints.provider_symbol,
            "company_name": observation.company_name,
            "exact_company_ids": sorted(str(company.pk) for company in evaluation.exact_companies),
            "matched_company_id": (
                str(evaluation.matched_company.pk)
                if evaluation.matched_company is not None
                else None
            ),
            "matched_security_listing_ids": sorted(
                str(listing.pk) for listing in evaluation.matched_listings
            ),
            "matched_outside_pool_company_ids": sorted(
                str(company.pk) for company in evaluation.outside_pool_companies
            ),
            "matched_outside_pool_security_listing_ids": sorted(
                str(listing.pk) for listing in evaluation.outside_pool_listings
            ),
            "reason_code": evaluation.reason_code,
        }
    }


def _decision_values_for_outcome(
    outcome: CompanyMatchOutcome,
) -> tuple[str, str]:
    if outcome is CompanyMatchOutcome.MATCHED:
        return "created_candidate", "resolved"
    if outcome is CompanyMatchOutcome.UNMATCHED:
        return "no_match", "rejected"
    if outcome is CompanyMatchOutcome.AMBIGUOUS:
        return "review_required", "open"
    if outcome is CompanyMatchOutcome.OUT_OF_POOL:
        return "ignored", "rejected"
    raise CandidateMatchingIntegrityError("Unknown company match outcome.")


def _create_matched_candidate(
    *,
    sync_run: SyncRun,
    observation: EarningsCalendarObservation,
    snapshot: MonitoringPoolSnapshot,
    hints: _NormalizedHints,
    evaluation: _MatchEvaluation,
    matcher_version: str,
    matching_input_revision: str,
    match_execution_key: str,
    match_factors: Mapping[str, object],
    decision_key: str,
    candidate_id: uuid.UUID,
) -> CandidateCreationResult:
    assert evaluation.matched_company is not None
    try:
        with transaction.atomic():
            evidence = record_source_evidence(
                raw_data_record=observation.raw_data_record,
                sync_run=sync_run,
                target_type=SourceEvidence.TargetType.EARNINGS_EVENT,
                target_id=candidate_id,
                field_name="company_match",
                raw_value=hints.as_evidence(),
                normalized_value=match_factors,
                confidence=_candidate_confidence(observation),
                normalizer_version=matcher_version,
            ).evidence
            candidate = EarningsEvent.objects.create(
                id=candidate_id,
                company=evaluation.matched_company,
                identity_status=IdentityStatus.CANDIDATE,
                identity_key=None,
                identity_rule_version=None,
                period_end_date=observation.period_end_date,
                period_type=observation.period_type,
                includes_q4=observation.period_type == PeriodType.FY,
                fiscal_year=observation.fiscal_year,
                **_candidate_fiscal_values(observation),
                status="scheduled_estimated",
                source_evidence=evidence,
            )
            candidate = _apply_observation_schedule(
                sync_run=sync_run,
                observation=observation,
                candidate=candidate,
            )
            decision_result = record_earnings_reconciliation_decision(
                observation=observation,
                decision_type="created_candidate",
                status="resolved",
                rule_version=matcher_version,
                target_event=candidate,
                match_factors=match_factors,
                reason="Exact company match inside frozen monitoring pool.",
                sync_run=sync_run,
            )
            _record_match_audit(
                sync_run=sync_run,
                target_type=AuditRecord.TargetType.EARNINGS_EVENT,
                target_id=candidate.pk,
                outcome=evaluation.outcome,
                match_execution_key=match_execution_key,
                matching_input_revision=matching_input_revision,
                decision_key=decision_result.decision.decision_key,
            )
            return CandidateCreationResult(
                sync_run=sync_run,
                observation=observation,
                snapshot=snapshot,
                outcome=evaluation.outcome,
                matcher_version=matcher_version,
                matching_input_revision=matching_input_revision,
                match_execution_key=match_execution_key,
                match_result_key=evaluation.match_result_key,
                decision=decision_result.decision,
                candidate=candidate,
                source_evidence=evidence,
                decision_created=decision_result.created,
                candidate_created=True,
            )
    except IntegrityError as error:
        existing = _find_decision(decision_key)
        if existing is None:
            raise CandidateMatchingIntegrityError(
                "Candidate creation violated an unexpected database constraint."
            ) from error
        return _reuse_existing_result(
            sync_run=sync_run,
            observation=observation,
            snapshot=snapshot,
            outcome=evaluation.outcome,
            matcher_version=matcher_version,
            matching_input_revision=matching_input_revision,
            match_execution_key=match_execution_key,
            match_result_key=evaluation.match_result_key,
            candidate_id=candidate_id,
            match_factors=match_factors,
            decision=existing,
        )


def _create_non_match_decision(
    *,
    sync_run: SyncRun,
    observation: EarningsCalendarObservation,
    snapshot: MonitoringPoolSnapshot,
    outcome: CompanyMatchOutcome,
    matcher_version: str,
    matching_input_revision: str,
    match_execution_key: str,
    match_result_key: str,
    match_factors: Mapping[str, object],
    decision_type: str,
    decision_status: str,
    decision_key: str,
    reason_code: str,
) -> CandidateCreationResult:
    try:
        with transaction.atomic():
            decision_result = record_earnings_reconciliation_decision(
                observation=observation,
                decision_type=decision_type,
                status=decision_status,
                rule_version=matcher_version,
                match_factors=match_factors,
                reason=f"Company matching outcome: {reason_code}.",
                sync_run=sync_run,
            )
            decision = decision_result.decision
            _record_match_audit(
                sync_run=sync_run,
                target_type=AuditRecord.TargetType.EARNINGS_RECONCILIATION_DECISION,
                target_id=decision.pk,
                outcome=outcome,
                match_execution_key=match_execution_key,
                matching_input_revision=matching_input_revision,
                decision_key=decision.decision_key,
            )
    except IntegrityError as error:
        existing = _find_decision(decision_key)
        if existing is None:
            raise CandidateMatchingIntegrityError(
                "Company match decision persistence violated a database constraint."
            ) from error
        return _reuse_existing_result(
            sync_run=sync_run,
            observation=observation,
            snapshot=snapshot,
            outcome=outcome,
            matcher_version=matcher_version,
            matching_input_revision=matching_input_revision,
            match_execution_key=match_execution_key,
            match_result_key=match_result_key,
            candidate_id=None,
            match_factors=match_factors,
            decision=existing,
        )

    return CandidateCreationResult(
        sync_run=sync_run,
        observation=observation,
        snapshot=snapshot,
        outcome=outcome,
        matcher_version=matcher_version,
        matching_input_revision=matching_input_revision,
        match_execution_key=match_execution_key,
        match_result_key=match_result_key,
        decision=decision,
        candidate=None,
        source_evidence=None,
        decision_created=decision_result.created,
        candidate_created=False,
    )


def _record_match_audit(
    *,
    sync_run: SyncRun,
    target_type: str,
    target_id: uuid.UUID,
    outcome: CompanyMatchOutcome,
    match_execution_key: str,
    matching_input_revision: str,
    decision_key: str,
) -> None:
    record_system_action(
        sync_run=sync_run,
        action=AuditRecord.Action.CREATE,
        target_type=target_type,
        target_id=target_id,
        before={},
        after={
            "company_match_outcome": outcome.value,
            "match_execution_key": match_execution_key,
            "matching_input_revision": matching_input_revision,
            "decision_key": decision_key,
        },
        reason=f"Company matching persisted outcome {outcome.value}.",
        request_id=f"company-match:{match_execution_key}",
    )


def _candidate_fiscal_values(
    observation: EarningsCalendarObservation,
) -> dict[str, object]:
    fiscal_calendar_type = observation.fiscal_calendar_type or FiscalCalendarType.MONTH_BASED
    period_length_weeks = observation.period_length_weeks
    if fiscal_calendar_type == FiscalCalendarType.WEEK_BASED_52_53:
        if period_length_weeks not in (52, 53):
            raise InvalidCandidateMatchingInput(
                "week-based candidate requires 52 or 53 period_length_weeks."
            )
    elif period_length_weeks is not None:
        raise InvalidCandidateMatchingInput(
            "period_length_weeks is only valid for week-based fiscal calendars."
        )
    return {
        "fiscal_calendar_type": fiscal_calendar_type,
        "period_length_weeks": period_length_weeks,
    }


def _candidate_confidence(observation: EarningsCalendarObservation) -> Decimal:
    if observation.confidence is None:
        return Decimal("1.0000")
    return observation.confidence


def _apply_observation_schedule(
    *,
    sync_run: SyncRun,
    observation: EarningsCalendarObservation,
    candidate: EarningsEvent,
) -> EarningsEvent:
    changes: dict[str, object] = {}
    if observation.estimated_release_precision == EarningsDatePrecision.DATE_ONLY:
        changes["estimated_release"] = observation.estimated_release_date
    elif observation.estimated_release_precision == EarningsDatePrecision.EXACT_DATETIME:
        changes["estimated_release"] = observation.estimated_release_at
    if observation.release_session != ReleaseSession.UNKNOWN:
        changes["release_session"] = observation.release_session
    if not changes:
        return candidate
    return update_earnings_schedule(
        earnings_event=candidate,
        changes=changes,
        sync_run=sync_run,
    ).earnings_event


def _find_decision(decision_key: str) -> EarningsReconciliationDecision | None:
    return (
        EarningsReconciliationDecision.objects.select_related("target_event")
        .filter(decision_key=decision_key)
        .first()
    )


def _reuse_existing_result(
    *,
    sync_run: SyncRun,
    observation: EarningsCalendarObservation,
    snapshot: MonitoringPoolSnapshot,
    outcome: CompanyMatchOutcome,
    matcher_version: str,
    matching_input_revision: str,
    match_execution_key: str,
    match_result_key: str,
    candidate_id: uuid.UUID | None,
    match_factors: Mapping[str, object],
    decision: EarningsReconciliationDecision,
) -> CandidateCreationResult:
    expected_type, expected_status = _decision_values_for_outcome(outcome)
    if (
        decision.observation_id != observation.pk
        or decision.decision_type != expected_type
        or decision.status != expected_status
        or decision.rule_version != matcher_version
        or decision.target_event_id != candidate_id
        or decision.match_factors != match_factors
    ):
        raise CandidateMatchingIntegrityError(
            "Existing company match decision does not match the requested execution."
        )
    company_match = match_factors[_MATCH_FACTOR_NAMESPACE]
    if not isinstance(company_match, dict):
        raise CandidateMatchingIntegrityError("Company match factors are invalid.")
    expected_company_id = company_match.get("matched_company_id")
    reason_code = company_match.get("reason_code")
    if not isinstance(reason_code, str):
        raise CandidateMatchingIntegrityError("Company match reason code is invalid.")
    expected_reason = (
        "Exact company match inside frozen monitoring pool."
        if outcome is CompanyMatchOutcome.MATCHED
        else f"Company matching outcome: {reason_code}."
    )
    if (
        decision.covered_fields != []
        or decision.actor_user_id is not None
        or decision.request_id != ""
        or decision.supersedes_id is not None
        or decision.sync_run_id is None
        or decision.reason != expected_reason
    ):
        raise CandidateMatchingIntegrityError(
            "Existing company match decision origin or reason is inconsistent."
        )

    candidate: EarningsEvent | None = None
    evidence: SourceEvidence | None = None
    if outcome is CompanyMatchOutcome.MATCHED:
        if decision.target_event is None:
            raise CandidateMatchingIntegrityError("Matched decision has no candidate.")
        candidate = decision.target_event
        expected_fiscal = _candidate_fiscal_values(observation)
        if (
            candidate.identity_status != IdentityStatus.CANDIDATE
            or candidate.identity_key is not None
            or candidate.identity_rule_version is not None
            or candidate.includes_q4 != (observation.period_type == PeriodType.FY)
            or (
                expected_company_id is not None and str(candidate.company_id) != expected_company_id
            )
            or candidate.period_end_date != observation.period_end_date
            or candidate.period_type != observation.period_type
            or candidate.fiscal_year != observation.fiscal_year
            or candidate.fiscal_calendar_type != expected_fiscal["fiscal_calendar_type"]
            or candidate.period_length_weeks != expected_fiscal["period_length_weeks"]
        ):
            raise CandidateMatchingIntegrityError(
                "Existing matched decision target is not an immutable candidate."
            )
        evidence = (
            SourceEvidence.objects.select_related("sync_run")
            .filter(
                target_type=SourceEvidence.TargetType.EARNINGS_EVENT,
                target_id=candidate.pk,
                field_name="company_match",
            )
            .first()
        )
        if (
            evidence is None
            or candidate.source_evidence_id != evidence.pk
            or evidence.raw_value != _normalize_hints(observation).as_evidence()
            or evidence.raw_data_record_id != observation.raw_data_record_id
            or evidence.sync_run.source_id != observation.source_id
            or evidence.confidence != _candidate_confidence(observation)
            or evidence.normalizer_version != matcher_version
            or evidence.normalized_value != match_factors
        ):
            raise CandidateMatchingIntegrityError(
                "Existing candidate has inconsistent company-match SourceEvidence."
            )
    elif decision.target_event_id is not None:
        raise CandidateMatchingIntegrityError(
            "Non-matched company match decision unexpectedly has a target candidate."
        )

    return CandidateCreationResult(
        sync_run=sync_run,
        observation=observation,
        snapshot=snapshot,
        outcome=outcome,
        matcher_version=matcher_version,
        matching_input_revision=matching_input_revision,
        match_execution_key=match_execution_key,
        match_result_key=match_result_key,
        decision=decision,
        candidate=candidate,
        source_evidence=evidence,
        decision_created=False,
        candidate_created=False,
    )


def _candidate_uuid(candidate_identity_key: str | None) -> uuid.UUID:
    if candidate_identity_key is None:
        raise CandidateMatchingIntegrityError("Candidate identity key is missing.")
    return uuid.uuid5(_CANDIDATE_NAMESPACE, candidate_identity_key)


def _sha256_json(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()
