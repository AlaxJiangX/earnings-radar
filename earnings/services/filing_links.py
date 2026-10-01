"""Filing ↔ Earnings matching, classification, review and replay.

The module owns the 4.5A contract from ADR-021: deterministic periodic and
release matching over persisted Stage 4.4 SEC metadata, metadata-only release
classification, append-only decisions, the current link projection, and manual
review authority.  It never downloads or parses a filing body.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from django.db import transaction

from audit.models import (
    AuditRecord,
    DomainTargetType,
    RawDataObservation,
    SyncRun,
)
from audit.services import (
    record_data_change,
    record_system_action,
    record_user_action,
    resolve_source_evidence_reference,
)
from earnings.models import (
    EarningsDatePrecision,
    EarningsEvent,
    EventStatus,
    FilingEarningsConfidence,
    FilingEarningsDecision,
    FilingEarningsDecisionSource,
    FilingEarningsLink,
    FilingEarningsRelationType,
    FilingEarningsReviewStatus,
    FilingReleaseClassification,
)
from earnings.services.filing_decision import (
    build_filing_earnings_decision_key,
    record_filing_earnings_decision,
    verify_reused_decision,
)
from filings.models import Filing, FilingDocument
from filings.parsing import normalize_reported_items

if TYPE_CHECKING:
    from accounts.models import User
    from audit.models import SourceEvidence

MATCH_RULE_VERSION = "filing-earnings-match-v1"
CLASSIFICATION_RULE_VERSION = "filing-release-classification-v1"

_MATCH_FACTORS_NAMESPACE = "filing_earnings"
_EASTERN = ZoneInfo("America/New_York")

_FORM_RELATION_TYPES = {
    "8-K": FilingEarningsRelationType.RELEASE_FILING,
    "6-K": FilingEarningsRelationType.RELEASE_FILING,
    "10-Q": FilingEarningsRelationType.PERIODIC_FILING,
    "10-K": FilingEarningsRelationType.PERIODIC_FILING,
    "20-F": FilingEarningsRelationType.PERIODIC_FILING,
    "40-F": FilingEarningsRelationType.PERIODIC_FILING,
}
_RELEASE_FORMS = frozenset({"8-K", "6-K"})
_ANNUAL_PERIODIC_FORMS = frozenset({"10-K", "20-F", "40-F"})
_QUARTER_PERIOD_TYPES = ("Q1", "Q2", "Q3")
_SUPPORTED_EXHIBITS = frozenset({"EX-99.1", "EX-99"})

REASON_ITEM_202_WITH_EARNINGS_EXHIBIT = "ITEM_202_WITH_EARNINGS_EXHIBIT"
REASON_NO_ITEM_202 = "NO_ITEM_202"
REASON_ITEMS_METADATA_MISSING = "ITEMS_METADATA_MISSING"
REASON_ITEM_202_WITHOUT_SUPPORTED_EXHIBIT = "ITEM_202_WITHOUT_SUPPORTED_EXHIBIT"
REASON_UNSUPPORTED_EXHIBIT_ONLY = "UNSUPPORTED_EXHIBIT_ONLY"
REASON_SIX_K_REQUIRES_REVIEW = "SIX_K_REQUIRES_REVIEW"
REASON_RELEASE_FACT_MISSING = "RELEASE_FACT_MISSING"
REASON_NO_MATCHING_RELEASE_WINDOW = "NO_MATCHING_RELEASE_WINDOW"
REASON_NO_MATCHING_PERIODIC_EVENT = "NO_MATCHING_PERIODIC_EVENT"
REASON_MULTIPLE_CANONICAL_EVENTS = "MULTIPLE_CANONICAL_EVENTS"
REASON_CANDIDATE_ONLY_EVENT = "CANDIDATE_ONLY_EVENT"
REASON_CANCELLED_CANONICAL_EVENT = "CANCELLED_CANONICAL_EVENT"
REASON_EXISTING_LINK_REVIEW_REQUIRED = "EXISTING_LINK_REVIEW_REQUIRED"
REASON_MANUAL_CONFIRMED = "MANUAL_CONFIRMED"
REASON_MANUAL_REJECTED = "MANUAL_REJECTED"
REASON_MATCHED_RELEASE_FILING = "MATCHED_RELEASE_FILING"
REASON_MATCHED_PERIODIC_FILING = "MATCHED_PERIODIC_FILING"

_REASON_TEXT = {
    REASON_ITEM_202_WITH_EARNINGS_EXHIBIT: (
        "8-K reports Item 2.02 with a supported earnings exhibit."
    ),
    REASON_NO_ITEM_202: "8-K does not report Item 2.02.",
    REASON_ITEMS_METADATA_MISSING: ("SEC reported items metadata is missing or unparseable."),
    REASON_ITEM_202_WITHOUT_SUPPORTED_EXHIBIT: (
        "8-K reports Item 2.02 without a supported EX-99.1 or EX-99 exhibit."
    ),
    REASON_UNSUPPORTED_EXHIBIT_ONLY: (
        "Only an unsupported EX-99.x exhibit is present for a non-2.02 8-K."
    ),
    REASON_SIX_K_REQUIRES_REVIEW: "6-K release classification always requires review.",
    REASON_RELEASE_FACT_MISSING: "No canonical event has a release reference fact.",
    REASON_NO_MATCHING_RELEASE_WINDOW: (
        "No canonical event reference date falls inside the bounded release window."
    ),
    REASON_NO_MATCHING_PERIODIC_EVENT: "No EarningsEvent matches the reported period.",
    REASON_MULTIPLE_CANONICAL_EVENTS: (
        "Multiple canonical EarningsEvents match the Filing; review is required."
    ),
    REASON_CANDIDATE_ONLY_EVENT: (
        "Only candidate EarningsEvents match the Filing; promotion is not automatic."
    ),
    REASON_CANCELLED_CANONICAL_EVENT: ("The only canonical EarningsEvent match is cancelled."),
    REASON_EXISTING_LINK_REVIEW_REQUIRED: (
        "An existing automatic link conflicts with the new evaluation; review is required."
    ),
    REASON_MANUAL_CONFIRMED: "A reviewer confirmed the Filing/Earnings relation.",
    REASON_MANUAL_REJECTED: "A reviewer rejected the Filing/Earnings relation.",
    REASON_MATCHED_RELEASE_FILING: (
        "A unique canonical EarningsEvent matched inside the bounded release window."
    ),
    REASON_MATCHED_PERIODIC_FILING: (
        "A unique canonical EarningsEvent matched the reported fiscal period."
    ),
}

_BINDING_DECISION_TYPES = frozenset(
    {"matched_release_filing", "matched_periodic_filing", "manual_confirmed"}
)


class FilingEarningsServiceError(ValueError):
    """Base error for the 4.5A filing/earnings services."""


class InvalidFilingEarningsInput(FilingEarningsServiceError):
    pass


class InvalidFilingEarningsReview(InvalidFilingEarningsInput):
    pass


class FilingEarningsIntegrityError(RuntimeError):
    pass


class FilingEarningsReviewIntegrityError(FilingEarningsIntegrityError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseClassification:
    classification: str
    reason_code: str
    reported_items_parseable: bool
    contains_item_202: bool | None
    supported_exhibits: tuple[str, ...]
    unsupported_exhibits: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FilingEarningsEvaluationResult:
    filing: Filing
    relation_type: str
    outcome: str
    decision: FilingEarningsDecision | None
    link: FilingEarningsLink | None
    decision_created: bool
    link_created: bool
    link_updated: bool
    blocked_by_manual_authority: bool
    match_rule_version: str
    classification_rule_version: str


@dataclass(frozen=True, slots=True)
class FilingEarningsReviewResult:
    filing: Filing
    relation_type: str
    decision: FilingEarningsDecision
    link: FilingEarningsLink | None
    decision_created: bool
    link_created: bool
    link_updated: bool


@dataclass(frozen=True, slots=True)
class _ReferenceFact:
    reference_date: date
    field_name: str


@dataclass(frozen=True, slots=True)
class _MatchingOutcome:
    outcome: str
    target_event: EarningsEvent | None
    confidence: str | None
    classification: ReleaseClassification | None
    reason_code: str
    details: dict[str, object]


def filing_relation_type_for_form(form_type: str) -> str:
    normalized = form_type.strip().upper() if isinstance(form_type, str) else ""
    try:
        return _FORM_RELATION_TYPES[normalized]
    except KeyError:
        raise InvalidFilingEarningsInput(
            "Filing form type is outside the Stage 4.4 target set."
        ) from None


def classify_release_filing(
    *,
    form_type: str,
    reported_items: str,
    document_types: Sequence[str],
) -> ReleaseClassification:
    """Apply the ADR-021 metadata-only 8-K/6-K decision table."""

    normalized_form = form_type.strip().upper()
    if normalized_form not in _RELEASE_FORMS:
        raise InvalidFilingEarningsInput(
            "Release classification only applies to 8-K and 6-K filings."
        )
    documents = tuple(sorted({item.strip().upper() for item in document_types if item.strip()}))
    supported = tuple(item for item in documents if item in _SUPPORTED_EXHIBITS)
    unsupported = tuple(
        item for item in documents if item.startswith("EX-99.") and item not in _SUPPORTED_EXHIBITS
    )
    normalized_items = normalize_reported_items(reported_items)
    items_parseable = bool(normalized_items)
    item_codes = set(normalized_items.split(",")) if items_parseable else set()
    contains_item_202: bool | None = "2.02" in item_codes if items_parseable else None

    if normalized_form == "6-K":
        return ReleaseClassification(
            classification=FilingReleaseClassification.REVIEW_REQUIRED,
            reason_code=REASON_SIX_K_REQUIRES_REVIEW,
            reported_items_parseable=items_parseable,
            contains_item_202=contains_item_202,
            supported_exhibits=supported,
            unsupported_exhibits=unsupported,
        )
    if not items_parseable:
        return ReleaseClassification(
            classification=FilingReleaseClassification.REVIEW_REQUIRED,
            reason_code=REASON_ITEMS_METADATA_MISSING,
            reported_items_parseable=False,
            contains_item_202=None,
            supported_exhibits=supported,
            unsupported_exhibits=unsupported,
        )
    if contains_item_202:
        if supported:
            return ReleaseClassification(
                classification=FilingReleaseClassification.YES,
                reason_code=REASON_ITEM_202_WITH_EARNINGS_EXHIBIT,
                reported_items_parseable=True,
                contains_item_202=True,
                supported_exhibits=supported,
                unsupported_exhibits=unsupported,
            )
        return ReleaseClassification(
            classification=FilingReleaseClassification.REVIEW_REQUIRED,
            reason_code=REASON_ITEM_202_WITHOUT_SUPPORTED_EXHIBIT,
            reported_items_parseable=True,
            contains_item_202=True,
            supported_exhibits=supported,
            unsupported_exhibits=unsupported,
        )
    if unsupported and not supported:
        return ReleaseClassification(
            classification=FilingReleaseClassification.REVIEW_REQUIRED,
            reason_code=REASON_UNSUPPORTED_EXHIBIT_ONLY,
            reported_items_parseable=True,
            contains_item_202=False,
            supported_exhibits=supported,
            unsupported_exhibits=unsupported,
        )
    return ReleaseClassification(
        classification=FilingReleaseClassification.NO,
        reason_code=REASON_NO_ITEM_202,
        reported_items_parseable=True,
        contains_item_202=False,
        supported_exhibits=supported,
        unsupported_exhibits=unsupported,
    )


def evaluate_filing_earnings_link(
    *,
    filing: Filing,
    sync_run: SyncRun,
) -> FilingEarningsEvaluationResult:
    """Deterministically evaluate one persisted Filing and update its projection."""

    filing_id = _persisted_id(filing, "filing")
    if sync_run._state.adding or sync_run.pk is None:
        raise InvalidFilingEarningsInput("sync_run must be saved before use.")
    with transaction.atomic():
        current = Filing.objects.select_for_update().select_related("company").get(pk=filing_id)
        current_run = _load_persisted_run(sync_run)
        relation_type = filing_relation_type_for_form(current.form_type)
        evidence, evidence_run = _load_filing_evidence(current)
        current_run = _validate_run_context(
            run=current_run,
            evidence=evidence,
            evidence_run=evidence_run,
        )
        documents = _document_types(current)
        evidence_digest = _build_evidence_digest(filing=current, documents=documents)
        leaf = _effective_decision(current, relation_type)
        links = _load_relation_links(current, relation_type, lock=True)
        if len(links) > 1:
            raise FilingEarningsIntegrityError(
                "A Filing relation must have at most one current link."
            )
        current_link = links[0] if links else None

        if leaf is not None and leaf.decision_source == FilingEarningsDecisionSource.MANUAL:
            _validate_manual_authority_projection(leaf=leaf, link=current_link)
            return FilingEarningsEvaluationResult(
                filing=current,
                relation_type=relation_type,
                outcome="manual_authority",
                decision=leaf,
                link=current_link,
                decision_created=False,
                link_created=False,
                link_updated=False,
                blocked_by_manual_authority=True,
                match_rule_version=MATCH_RULE_VERSION,
                classification_rule_version=CLASSIFICATION_RULE_VERSION,
            )

        outcome = _evaluate(current, relation_type, documents)
        outcome = _apply_existing_link_rule(outcome, current_link)
        classification_rule_version = (
            CLASSIFICATION_RULE_VERSION if outcome.classification is not None else ""
        )
        match_factors = _build_match_factors(
            filing=current,
            relation_type=relation_type,
            outcome=outcome,
            evidence_digest=evidence_digest,
            classification_rule_version=classification_rule_version,
        )
        decision_type = outcome.outcome
        if decision_type in _BINDING_DECISION_TYPES:
            status = "resolved"
        elif decision_type == "review_required":
            status = "open"
        else:
            status = "rejected"
        classification = (
            outcome.classification.classification if outcome.classification is not None else None
        )
        confidence = outcome.confidence
        target_event = outcome.target_event
        reason = _REASON_TEXT.get(outcome.reason_code, outcome.reason_code)
        decision_key = build_filing_earnings_decision_key(
            filing_id=current.pk,
            relation_type=relation_type,
            decision_type=decision_type,
            status=status,
            match_rule_version=MATCH_RULE_VERSION,
            classification_rule_version=classification_rule_version,
            decision_source=FilingEarningsDecisionSource.AUTOMATIC,
            classification=classification,
            confidence=confidence,
            match_factors=match_factors,
            target_event_id=target_event.pk if target_event is not None else None,
            actor_user_id=None,
            request_id="",
        )
        existing = FilingEarningsDecision.objects.filter(decision_key=decision_key).first()
        if existing is not None:
            verify_reused_decision(
                decision=existing,
                filing_id=current.pk,
                relation_type=relation_type,
                target_event_id=target_event.pk if target_event is not None else None,
                decision_type=decision_type,
                status=status,
                classification=classification,
                confidence=confidence,
                match_rule_version=MATCH_RULE_VERSION,
                classification_rule_version=classification_rule_version,
                decision_source=FilingEarningsDecisionSource.AUTOMATIC,
                match_factors=match_factors,
                actor_user_id=None,
                request_id="",
            )
            _validate_reused_projection(
                decision=existing,
                link=current_link,
                reason_code=outcome.reason_code,
            )
            return FilingEarningsEvaluationResult(
                filing=current,
                relation_type=relation_type,
                outcome=outcome.outcome,
                decision=existing,
                link=current_link,
                decision_created=False,
                link_created=False,
                link_updated=False,
                blocked_by_manual_authority=False,
                match_rule_version=MATCH_RULE_VERSION,
                classification_rule_version=classification_rule_version,
            )

        write = record_filing_earnings_decision(
            filing=current,
            relation_type=relation_type,
            decision_type=decision_type,
            status=status,
            match_rule_version=MATCH_RULE_VERSION,
            classification_rule_version=classification_rule_version,
            decision_source=FilingEarningsDecisionSource.AUTOMATIC,
            classification=classification,
            confidence=confidence,
            match_factors=match_factors,
            reason=reason,
            target_event=target_event,
            source_raw_data_record=evidence.raw_data_record,
            source_evidence=evidence,
            sync_run=current_run,
            supersedes=leaf,
        )
        decision = write.decision
        if not write.created:
            _validate_reused_projection(
                decision=decision,
                link=current_link,
                reason_code=outcome.reason_code,
            )
            return FilingEarningsEvaluationResult(
                filing=current,
                relation_type=relation_type,
                outcome=outcome.outcome,
                decision=decision,
                link=current_link,
                decision_created=False,
                link_created=False,
                link_updated=False,
                blocked_by_manual_authority=False,
                match_rule_version=MATCH_RULE_VERSION,
                classification_rule_version=classification_rule_version,
            )
        record_system_action(
            sync_run=current_run,
            action=AuditRecord.Action.CREATE,
            target_type=AuditRecord.TargetType.FILING_EARNINGS_DECISION,
            target_id=decision.pk,
            before={"predecessor_decision_id": str(leaf.pk) if leaf is not None else None},
            after={
                "decision_type": decision.decision_type,
                "status": decision.status,
                "target_event_id": (
                    str(decision.target_event_id) if decision.target_event_id else None
                ),
                "classification": decision.classification,
                "reason_code": outcome.reason_code,
                "decision_key": decision.decision_key,
            },
            reason=reason,
            request_id=f"filing-earnings-decision:{decision.decision_key}",
        )

        if decision_type not in _BINDING_DECISION_TYPES:
            return FilingEarningsEvaluationResult(
                filing=current,
                relation_type=relation_type,
                outcome=outcome.outcome,
                decision=decision,
                link=current_link,
                decision_created=True,
                link_created=False,
                link_updated=False,
                blocked_by_manual_authority=False,
                match_rule_version=MATCH_RULE_VERSION,
                classification_rule_version=classification_rule_version,
            )

        projection_reason_code = (
            outcome.classification.reason_code
            if outcome.classification is not None
            else outcome.reason_code
        )
        link, link_created, link_updated = _upsert_automatic_link(
            filing=current,
            decision=decision,
            reason_code=projection_reason_code,
            run=current_run,
            existing_link=current_link,
        )
        return FilingEarningsEvaluationResult(
            filing=current,
            relation_type=relation_type,
            outcome=outcome.outcome,
            decision=decision,
            link=link,
            decision_created=True,
            link_created=link_created,
            link_updated=link_updated,
            blocked_by_manual_authority=False,
            match_rule_version=MATCH_RULE_VERSION,
            classification_rule_version=classification_rule_version,
        )


def confirm_filing_earnings_link(
    *,
    filing: Filing,
    relation_type: str,
    target_event: EarningsEvent,
    actor_user: User,
    reason: str,
    request_id: str,
    sync_run: SyncRun | None = None,
    ip_address: str | None = None,
) -> FilingEarningsReviewResult:
    """Append a manual confirmation and project it onto the current link."""

    return _record_manual_resolution(
        action="confirm",
        filing=filing,
        relation_type=relation_type,
        target_event=target_event,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        sync_run=sync_run,
        ip_address=ip_address,
    )


def reject_filing_earnings_link(
    *,
    filing: Filing,
    relation_type: str,
    actor_user: User,
    reason: str,
    request_id: str,
    sync_run: SyncRun | None = None,
    ip_address: str | None = None,
) -> FilingEarningsReviewResult:
    """Append a manual rejection for ``(filing, relation_type)``."""

    return _record_manual_resolution(
        action="reject",
        filing=filing,
        relation_type=relation_type,
        target_event=None,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        sync_run=sync_run,
        ip_address=ip_address,
    )


def _record_manual_resolution(
    *,
    action: str,
    filing: Filing,
    relation_type: str,
    target_event: EarningsEvent | None,
    actor_user: User,
    reason: str,
    request_id: str,
    sync_run: SyncRun | None,
    ip_address: str | None,
) -> FilingEarningsReviewResult:
    filing_id = _persisted_id(filing, "filing")
    actor_id = _persisted_id(actor_user, "actor_user")
    relation = _normalize_review_relation(relation_type)
    normalized_reason = _required_text(reason, "reason", 2000)
    normalized_request = _required_text(request_id, "request_id", 255)
    with transaction.atomic():
        current = Filing.objects.select_for_update().get(pk=filing_id)
        if filing_relation_type_for_form(current.form_type) != relation:
            raise InvalidFilingEarningsReview("relation_type does not match the Filing form type.")
        evidence, evidence_run = _load_filing_evidence(current)
        current_run: SyncRun | None = None
        if sync_run is not None:
            current_run = _load_persisted_run(sync_run)
            current_run = _validate_run_context(
                run=current_run,
                evidence=evidence,
                evidence_run=evidence_run,
            )
        current_target: EarningsEvent | None = None
        if action == "confirm":
            if target_event is None:
                raise InvalidFilingEarningsReview("manual confirmation requires a target event.")
            current_target = _load_canonical_target(target_event)
        if current_target is not None and current_target.company_id != current.company_id:
            raise InvalidFilingEarningsReview(
                "manual confirmation requires an EarningsEvent of the same Company."
            )
        _validate_review_target(
            action=action,
            relation_type=relation,
            filing=current,
            target_event=current_target,
        )

        existing_request = list(
            FilingEarningsDecision.objects.filter(
                filing=current,
                relation_type=relation,
                actor_user_id=actor_id,
                request_id=normalized_request,
            ).order_by("decided_at", "id")
        )
        if len(existing_request) > 1:
            raise FilingEarningsReviewIntegrityError(
                "A manual request_id must not resolve to multiple decisions."
            )
        links = _load_relation_links(current, relation, lock=True)
        if len(links) > 1:
            raise FilingEarningsReviewIntegrityError(
                "A Filing relation must have at most one current link."
            )
        current_link = links[0] if links else None
        if existing_request:
            decision = existing_request[0]
            _verify_manual_replay(
                decision=decision,
                action=action,
                target_event=current_target,
                reason=normalized_reason,
            )
            return FilingEarningsReviewResult(
                filing=current,
                relation_type=relation,
                decision=decision,
                link=current_link,
                decision_created=False,
                link_created=False,
                link_updated=False,
            )

        leaf = _effective_decision(current, relation)
        match_factors = _build_manual_match_factors(
            filing=current,
            relation_type=relation,
            action=action,
            target_event=current_target,
            predecessor=leaf,
            request_id=normalized_request,
        )
        if action == "confirm":
            assert current_target is not None
            classification = (
                FilingReleaseClassification.YES
                if relation == FilingEarningsRelationType.RELEASE_FILING
                else None
            )
            write = record_filing_earnings_decision(
                filing=current,
                relation_type=relation,
                decision_type="manual_confirmed",
                status="resolved",
                match_rule_version=MATCH_RULE_VERSION,
                classification_rule_version=(
                    CLASSIFICATION_RULE_VERSION
                    if relation == FilingEarningsRelationType.RELEASE_FILING
                    else ""
                ),
                decision_source=FilingEarningsDecisionSource.MANUAL,
                classification=classification,
                confidence=FilingEarningsConfidence.MANUAL,
                match_factors=match_factors,
                reason=normalized_reason,
                target_event=current_target,
                source_raw_data_record=evidence.raw_data_record,
                source_evidence=evidence,
                actor_user=actor_user,
                sync_run=current_run,
                request_id=normalized_request,
                supersedes=leaf,
            )
        else:
            write = record_filing_earnings_decision(
                filing=current,
                relation_type=relation,
                decision_type="manual_rejected",
                status="rejected",
                match_rule_version=MATCH_RULE_VERSION,
                classification_rule_version="",
                decision_source=FilingEarningsDecisionSource.MANUAL,
                classification=None,
                confidence=None,
                match_factors=match_factors,
                reason=normalized_reason,
                target_event=None,
                source_raw_data_record=evidence.raw_data_record,
                source_evidence=evidence,
                actor_user=actor_user,
                sync_run=current_run,
                request_id=normalized_request,
                supersedes=leaf,
            )
        decision = write.decision
        if not write.created:
            return FilingEarningsReviewResult(
                filing=current,
                relation_type=relation,
                decision=decision,
                link=current_link,
                decision_created=False,
                link_created=False,
                link_updated=False,
            )
        record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.MANUAL_CORRECTION,
            target_type=AuditRecord.TargetType.FILING_EARNINGS_DECISION,
            target_id=decision.pk,
            before={"predecessor_decision_id": str(leaf.pk) if leaf is not None else None},
            after={
                "decision_type": decision.decision_type,
                "status": decision.status,
                "target_event_id": (
                    str(decision.target_event_id) if decision.target_event_id else None
                ),
                "classification": decision.classification,
                "request_id": normalized_request,
                "decision_key": decision.decision_key,
            },
            reason=normalized_reason,
            request_id=normalized_request,
            ip_address=ip_address,
            sync_run=current_run,
        )
        link: FilingEarningsLink | None
        if action == "confirm":
            assert current_target is not None
            confirmed_link, link_created, link_updated = _upsert_manual_link(
                filing=current,
                decision=decision,
                target_event=current_target,
                actor_user=actor_user,
                reason=normalized_reason,
                request_id=normalized_request,
                evidence=evidence,
                run=current_run,
                existing_link=current_link,
            )
            link = confirmed_link
        else:
            rejected_link, link_updated = _reject_current_link(
                decision=decision,
                actor_user=actor_user,
                reason=normalized_reason,
                request_id=normalized_request,
                existing_link=current_link,
                run=current_run,
            )
            link = rejected_link
            link_created = False
        return FilingEarningsReviewResult(
            filing=current,
            relation_type=relation,
            decision=decision,
            link=link,
            decision_created=True,
            link_created=link_created,
            link_updated=link_updated,
        )


def _evaluate(
    filing: Filing,
    relation_type: str,
    documents: tuple[str, ...],
) -> _MatchingOutcome:
    if relation_type == FilingEarningsRelationType.RELEASE_FILING:
        return _evaluate_release(filing, documents)
    return _evaluate_periodic(filing)


def _evaluate_periodic(filing: Filing) -> _MatchingOutcome:
    canonical = _periodic_events(filing=filing, identity_status="canonical")
    candidates = _periodic_events(filing=filing, identity_status="candidate")
    details: dict[str, object] = {
        "strategy": "periodic_exact",
        "period_of_report": _iso_or_none(filing.period_of_report),
        "canonical_event_ids": _sorted_ids(canonical),
        "candidate_event_ids": _sorted_ids(candidates),
    }
    if len(canonical) == 1:
        event = canonical[0]
        details["matched_event_id"] = str(event.pk)
        if event.status == EventStatus.CANCELLED:
            return _MatchingOutcome(
                outcome="review_required",
                target_event=None,
                confidence=None,
                classification=None,
                reason_code=REASON_CANCELLED_CANONICAL_EVENT,
                details=details,
            )
        details["matched_event_status"] = event.status
        return _MatchingOutcome(
            outcome="matched_periodic_filing",
            target_event=event,
            confidence=FilingEarningsConfidence.EXACT,
            classification=None,
            reason_code=REASON_MATCHED_PERIODIC_FILING,
            details=details,
        )
    if len(canonical) > 1:
        return _MatchingOutcome(
            outcome="review_required",
            target_event=None,
            confidence=None,
            classification=None,
            reason_code=REASON_MULTIPLE_CANONICAL_EVENTS,
            details=details,
        )
    if candidates:
        return _MatchingOutcome(
            outcome="review_required",
            target_event=None,
            confidence=None,
            classification=None,
            reason_code=REASON_CANDIDATE_ONLY_EVENT,
            details=details,
        )
    return _MatchingOutcome(
        outcome="no_match",
        target_event=None,
        confidence=None,
        classification=None,
        reason_code=REASON_NO_MATCHING_PERIODIC_EVENT,
        details=details,
    )


def _evaluate_release(filing: Filing, documents: tuple[str, ...]) -> _MatchingOutcome:
    accepted = filing.accepted_at.astimezone(_EASTERN).date()
    window_start = accepted.fromordinal(accepted.toordinal() - 1)
    window_end = accepted.fromordinal(accepted.toordinal() + 1)
    canonical = _release_events(filing=filing, identity_status="canonical")
    candidates = _release_events(filing=filing, identity_status="candidate")
    facts_exist = bool(canonical or candidates)
    canonical_in_window = [
        item for item in canonical if window_start <= item[1].reference_date <= window_end
    ]
    candidates_in_window = [
        item for item in candidates if window_start <= item[1].reference_date <= window_end
    ]
    classification = classify_release_filing(
        form_type=filing.form_type,
        reported_items=filing.reported_items,
        document_types=documents,
    )
    details: dict[str, object] = {
        "strategy": "release_bounded_window",
        "filing_accepted_date_et": accepted.isoformat(),
        "window_start_et": window_start.isoformat(),
        "window_end_et": window_end.isoformat(),
        "canonical_event_ids": _sorted_ids([item[0] for item in canonical]),
        "candidate_event_ids": _sorted_ids([item[0] for item in candidates]),
        "canonical_window_event_ids": _sorted_ids([item[0] for item in canonical_in_window]),
        "candidate_window_event_ids": _sorted_ids([item[0] for item in candidates_in_window]),
    }
    if not facts_exist:
        return _MatchingOutcome(
            outcome="no_match",
            target_event=None,
            confidence=None,
            classification=None,
            reason_code=REASON_RELEASE_FACT_MISSING,
            details=details,
        )
    if len(canonical_in_window) > 1:
        return _MatchingOutcome(
            outcome="review_required",
            target_event=None,
            confidence=None,
            classification=None,
            reason_code=REASON_MULTIPLE_CANONICAL_EVENTS,
            details=details,
        )
    if len(canonical_in_window) == 1:
        event, fact = canonical_in_window[0]
        details["matched_event_id"] = str(event.pk)
        details["matched_reference_field"] = fact.field_name
        details["matched_reference_date"] = fact.reference_date.isoformat()
        if event.status == EventStatus.CANCELLED:
            return _MatchingOutcome(
                outcome="review_required",
                target_event=None,
                confidence=None,
                classification=None,
                reason_code=REASON_CANCELLED_CANONICAL_EVENT,
                details=details,
            )
        return _MatchingOutcome(
            outcome="matched_release_filing",
            target_event=event,
            confidence=FilingEarningsConfidence.BOUNDED_WINDOW,
            classification=classification,
            reason_code=REASON_MATCHED_RELEASE_FILING,
            details=details,
        )
    if candidates_in_window:
        return _MatchingOutcome(
            outcome="review_required",
            target_event=None,
            confidence=None,
            classification=None,
            reason_code=REASON_CANDIDATE_ONLY_EVENT,
            details=details,
        )
    return _MatchingOutcome(
        outcome="no_match",
        target_event=None,
        confidence=None,
        classification=None,
        reason_code=REASON_NO_MATCHING_RELEASE_WINDOW,
        details=details,
    )


def _periodic_events(*, filing: Filing, identity_status: str) -> list[EarningsEvent]:
    if filing.period_of_report is None:
        return []
    queryset = EarningsEvent.objects.filter(
        company_id=filing.company_id,
        identity_status=identity_status,
        period_end_date=filing.period_of_report,
    )
    if filing.form_type == "10-Q":
        queryset = queryset.filter(period_type__in=_QUARTER_PERIOD_TYPES)
    elif filing.form_type in _ANNUAL_PERIODIC_FORMS:
        queryset = queryset.filter(period_type="FY", includes_q4=True)
    else:
        return []
    return list(queryset.order_by("id"))


def _release_events(
    *, filing: Filing, identity_status: str
) -> list[tuple[EarningsEvent, _ReferenceFact]]:
    events = EarningsEvent.objects.filter(
        company_id=filing.company_id,
        identity_status=identity_status,
    ).order_by("id")
    result: list[tuple[EarningsEvent, _ReferenceFact]] = []
    for event in events:
        fact = _reference_fact(event)
        if fact is not None:
            result.append((event, fact))
    return result


def _reference_fact(event: EarningsEvent) -> _ReferenceFact | None:
    # ADR-021 precedence: earnings_release > confirmed_release > estimated_release.
    return (
        _fact_for(
            field_name="earnings_release",
            precision=event.earnings_release_precision,
            value_at=event.earnings_release_at,
            value_date=event.earnings_release_date,
        )
        or _fact_for(
            field_name="confirmed_release",
            precision=event.confirmed_release_precision,
            value_at=event.confirmed_release_at,
            value_date=event.confirmed_release_date,
        )
        or _fact_for(
            field_name="estimated_release",
            precision=event.estimated_release_precision,
            value_at=event.estimated_release_at,
            value_date=event.estimated_release_date,
        )
    )


def _fact_for(
    *,
    field_name: str,
    precision: str,
    value_at: datetime | None,
    value_date: date | None,
) -> _ReferenceFact | None:
    if precision == EarningsDatePrecision.EXACT_DATETIME and value_at is not None:
        return _ReferenceFact(
            reference_date=value_at.astimezone(_EASTERN).date(),
            field_name=field_name,
        )
    if precision == EarningsDatePrecision.DATE_ONLY and value_date is not None:
        return _ReferenceFact(reference_date=value_date, field_name=field_name)
    return None


def _apply_existing_link_rule(
    outcome: _MatchingOutcome,
    existing_link: FilingEarningsLink | None,
) -> _MatchingOutcome:
    """Never let automation silently move or remove an existing projection."""

    if existing_link is None:
        return outcome
    if outcome.outcome in _BINDING_DECISION_TYPES:
        if (
            outcome.target_event is not None
            and outcome.target_event.pk == existing_link.earnings_event_id
        ):
            return outcome
    details = dict(outcome.details)
    details["computed_outcome"] = outcome.outcome
    details["computed_reason_code"] = outcome.reason_code
    details["existing_link_decision_id"] = str(existing_link.current_decision_id)
    details["existing_link_event_id"] = str(existing_link.earnings_event_id)
    return _MatchingOutcome(
        outcome="review_required",
        target_event=None,
        confidence=None,
        classification=None,
        reason_code=REASON_EXISTING_LINK_REVIEW_REQUIRED,
        details=details,
    )


def _build_match_factors(
    *,
    filing: Filing,
    relation_type: str,
    outcome: _MatchingOutcome,
    evidence_digest: str,
    classification_rule_version: str,
) -> dict[str, object]:
    return {
        _MATCH_FACTORS_NAMESPACE: {
            "match_rule_version": MATCH_RULE_VERSION,
            "classification_rule_version": classification_rule_version or None,
            "evidence_digest": evidence_digest,
            "relation_type": relation_type,
            "form_type": filing.form_type,
            "reason_code": outcome.reason_code,
            "details": outcome.details,
            "classification": _classification_factors(outcome.classification),
        }
    }


def _build_manual_match_factors(
    *,
    filing: Filing,
    relation_type: str,
    action: str,
    target_event: EarningsEvent | None,
    predecessor: FilingEarningsDecision | None,
    request_id: str,
) -> dict[str, object]:
    reason_code = REASON_MANUAL_CONFIRMED if action == "confirm" else REASON_MANUAL_REJECTED
    return {
        _MATCH_FACTORS_NAMESPACE: {
            "match_rule_version": MATCH_RULE_VERSION,
            "classification_rule_version": (
                CLASSIFICATION_RULE_VERSION
                if relation_type == FilingEarningsRelationType.RELEASE_FILING
                else None
            ),
            "relation_type": relation_type,
            "form_type": filing.form_type,
            "reason_code": reason_code,
            "manual": {
                "action": action,
                "request_id": request_id,
                "predecessor_decision_id": (
                    str(predecessor.pk) if predecessor is not None else None
                ),
                "target_event_id": str(target_event.pk) if target_event is not None else None,
            },
        }
    }


def _classification_factors(
    classification: ReleaseClassification | None,
) -> dict[str, object] | None:
    if classification is None:
        return None
    return {
        "classification": classification.classification,
        "reason_code": classification.reason_code,
        "reported_items_parseable": classification.reported_items_parseable,
        "contains_item_202": classification.contains_item_202,
        "supported_exhibits": list(classification.supported_exhibits),
        "unsupported_exhibits": list(classification.unsupported_exhibits),
    }


def _upsert_automatic_link(
    *,
    filing: Filing,
    decision: FilingEarningsDecision,
    reason_code: str,
    run: SyncRun,
    existing_link: FilingEarningsLink | None,
) -> tuple[FilingEarningsLink, bool, bool]:
    target_event = decision.target_event
    if target_event is None or decision.confidence is None:
        raise FilingEarningsIntegrityError(
            "A binding automatic decision requires a target event and confidence."
        )
    classification_reason = (
        reason_code if decision.relation_type == FilingEarningsRelationType.RELEASE_FILING else ""
    )
    desired: dict[str, object] = {
        "release_filing_classification": decision.classification,
        "classification_reason": classification_reason,
        "classification_rule_version": decision.classification_rule_version,
        "match_rule_version": decision.match_rule_version,
        "confidence": decision.confidence,
        "review_status": FilingEarningsReviewStatus.AUTO,
        "review_reason": "",
        "reviewed_by": None,
        "reviewed_at": None,
        "source_evidence": decision.source_evidence,
        "current_decision": decision,
    }
    if existing_link is None:
        link = FilingEarningsLink.objects.create(
            filing=filing,
            earnings_event=target_event,
            relation_type=decision.relation_type,
            **desired,
        )
        record_system_action(
            sync_run=run,
            action=AuditRecord.Action.CREATE,
            target_type=AuditRecord.TargetType.FILING_EARNINGS_LINK,
            target_id=link.pk,
            before=None,
            after=_link_snapshot(link),
            reason=decision.reason,
            request_id=f"filing-earnings-link:{decision.decision_key}",
        )
        return link, True, False
    if existing_link.earnings_event_id != target_event.pk:
        raise FilingEarningsIntegrityError(
            "Automatic evaluation must not re-point an existing link."
        )
    changes = _link_changes(existing_link, desired)
    if not changes:
        return existing_link, False, False
    before = _link_snapshot(existing_link)
    for field_name, old_value, new_value in changes:
        record_data_change(
            target_type=DomainTargetType.FILING_EARNINGS_LINK,
            target_id=existing_link.pk,
            field_name=field_name,
            old_value=old_value,
            new_value=new_value,
            rule_version=_rule_version_for_field(field_name),
            sync_run=run,
        )
        _apply_link_change(existing_link, field_name, desired[field_name])
    _save_link(existing_link)
    record_system_action(
        sync_run=run,
        action=AuditRecord.Action.UPDATE,
        target_type=AuditRecord.TargetType.FILING_EARNINGS_LINK,
        target_id=existing_link.pk,
        before=before,
        after=_link_snapshot(existing_link),
        reason=decision.reason,
        request_id=f"filing-earnings-link:{decision.decision_key}",
    )
    return existing_link, False, True


def _upsert_manual_link(
    *,
    filing: Filing,
    decision: FilingEarningsDecision,
    target_event: EarningsEvent,
    actor_user: User,
    reason: str,
    request_id: str,
    evidence: SourceEvidence,
    run: SyncRun | None,
    existing_link: FilingEarningsLink | None,
) -> tuple[FilingEarningsLink, bool, bool]:
    if existing_link is not None and existing_link.earnings_event_id != target_event.pk:
        raise FilingEarningsReviewIntegrityError(
            "Manual confirmation cannot re-point an existing current link."
        )
    desired: dict[str, object] = {
        "release_filing_classification": decision.classification,
        "classification_reason": (
            REASON_MANUAL_CONFIRMED
            if decision.relation_type == FilingEarningsRelationType.RELEASE_FILING
            else ""
        ),
        "classification_rule_version": decision.classification_rule_version,
        "match_rule_version": decision.match_rule_version,
        "confidence": decision.confidence,
        "review_status": FilingEarningsReviewStatus.CONFIRMED,
        "review_reason": reason,
        "reviewed_by": actor_user,
        "reviewed_at": decision.decided_at,
        "source_evidence": evidence,
        "current_decision": decision,
    }
    if existing_link is None:
        link = FilingEarningsLink.objects.create(
            filing=filing,
            earnings_event=target_event,
            relation_type=decision.relation_type,
            **desired,
        )
        record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.CREATE,
            target_type=AuditRecord.TargetType.FILING_EARNINGS_LINK,
            target_id=link.pk,
            before=None,
            after=_link_snapshot(link),
            reason=reason,
            request_id=request_id,
            sync_run=run,
        )
        return link, True, False
    changes = _link_changes(existing_link, desired)
    if not changes:
        return existing_link, False, False
    before = _link_snapshot(existing_link)
    for field_name, old_value, new_value in changes:
        record_data_change(
            target_type=DomainTargetType.FILING_EARNINGS_LINK,
            target_id=existing_link.pk,
            field_name=field_name,
            old_value=old_value,
            new_value=new_value,
            rule_version=_rule_version_for_field(field_name),
            actor_user=actor_user,
            reason=reason,
            origin_key=request_id,
        )
        _apply_link_change(existing_link, field_name, desired[field_name])
    _save_link(existing_link)
    record_user_action(
        actor_user=actor_user,
        action=AuditRecord.Action.MANUAL_CORRECTION,
        target_type=AuditRecord.TargetType.FILING_EARNINGS_LINK,
        target_id=existing_link.pk,
        before=before,
        after=_link_snapshot(existing_link),
        reason=reason,
        request_id=request_id,
        sync_run=run,
    )
    return existing_link, False, True


def _reject_current_link(
    *,
    decision: FilingEarningsDecision,
    actor_user: User,
    reason: str,
    request_id: str,
    existing_link: FilingEarningsLink | None,
    run: SyncRun | None,
) -> tuple[FilingEarningsLink | None, bool]:
    if existing_link is None:
        return None, False
    desired: dict[str, object] = {
        "review_status": FilingEarningsReviewStatus.REJECTED,
        "review_reason": reason,
        "reviewed_by": actor_user,
        "reviewed_at": decision.decided_at,
        "current_decision": decision,
    }
    changes = _link_changes(existing_link, desired)
    if not changes:
        return existing_link, False
    before = _link_snapshot(existing_link)
    for field_name, old_value, new_value in changes:
        record_data_change(
            target_type=DomainTargetType.FILING_EARNINGS_LINK,
            target_id=existing_link.pk,
            field_name=field_name,
            old_value=old_value,
            new_value=new_value,
            rule_version=_rule_version_for_field(field_name),
            actor_user=actor_user,
            reason=reason,
            origin_key=request_id,
        )
        _apply_link_change(existing_link, field_name, desired[field_name])
    _save_link(existing_link)
    record_user_action(
        actor_user=actor_user,
        action=AuditRecord.Action.MANUAL_CORRECTION,
        target_type=AuditRecord.TargetType.FILING_EARNINGS_LINK,
        target_id=existing_link.pk,
        before=before,
        after=_link_snapshot(existing_link),
        reason=reason,
        request_id=request_id,
        sync_run=run,
    )
    return existing_link, True


def _link_changes(
    link: FilingEarningsLink,
    desired: Mapping[str, object],
) -> list[tuple[str, object, object]]:
    changes: list[tuple[str, object, object]] = []
    for field_name, new_value in desired.items():
        field = FilingEarningsLink._meta.get_field(field_name)
        current_value: object
        if field.many_to_one or field.one_to_one:
            current_value = getattr(link, f"{field_name}_id")
            new_identity = getattr(new_value, "pk", None) if new_value is not None else None
            if current_value != new_identity:
                changes.append(
                    (
                        field_name,
                        _json_identity(current_value),
                        _json_identity(new_identity),
                    )
                )
            continue
        current_value = getattr(link, field_name)
        if isinstance(current_value, datetime) or isinstance(new_value, datetime):
            comparable_current = _iso_or_none(current_value) if current_value else None
            comparable_new = _iso_or_none(new_value) if new_value else None
            if comparable_current != comparable_new:
                changes.append((field_name, comparable_current, comparable_new))
            continue
        if current_value != new_value:
            changes.append((field_name, current_value, new_value))
    return changes


def _apply_link_change(
    link: FilingEarningsLink,
    field_name: str,
    value: object,
) -> None:
    setattr(link, field_name, value)


def _save_link(link: FilingEarningsLink) -> None:
    link.save(update_fields=(*_LINK_MUTABLE_FIELDS, "updated_at"))


_LINK_MUTABLE_FIELDS = (
    "release_filing_classification",
    "classification_reason",
    "classification_rule_version",
    "match_rule_version",
    "confidence",
    "review_status",
    "review_reason",
    "reviewed_by",
    "reviewed_at",
    "source_evidence",
    "current_decision",
)


def _link_snapshot(link: FilingEarningsLink) -> dict[str, object]:
    return {
        "earnings_event_id": str(link.earnings_event_id),
        "relation_type": link.relation_type,
        "release_filing_classification": link.release_filing_classification,
        "classification_reason": link.classification_reason,
        "classification_rule_version": link.classification_rule_version,
        "match_rule_version": link.match_rule_version,
        "confidence": link.confidence,
        "review_status": link.review_status,
        "review_reason": link.review_reason,
        "reviewed_by_id": (str(link.reviewed_by_id) if link.reviewed_by_id is not None else None),
        "reviewed_at": link.reviewed_at.isoformat() if link.reviewed_at else None,
        "source_evidence_id": (
            str(link.source_evidence_id) if link.source_evidence_id is not None else None
        ),
        "current_decision_id": str(link.current_decision_id),
    }


def _rule_version_for_field(field_name: str) -> str:
    if field_name in {
        "release_filing_classification",
        "classification_reason",
        "classification_rule_version",
    }:
        return CLASSIFICATION_RULE_VERSION
    return MATCH_RULE_VERSION


def _json_identity(value: object) -> object:
    return str(value) if value is not None else None


def _validate_manual_authority_projection(
    *,
    leaf: FilingEarningsDecision,
    link: FilingEarningsLink | None,
) -> None:
    if leaf.decision_type == "manual_confirmed":
        if link is None or link.current_decision_id != leaf.pk:
            raise FilingEarningsIntegrityError(
                "A manual confirmation must project onto its current link."
            )
        if (
            link.review_status != FilingEarningsReviewStatus.CONFIRMED
            or link.reviewed_by_id != leaf.actor_user_id
            or link.reviewed_at is None
        ):
            raise FilingEarningsIntegrityError(
                "The confirmed link does not match its manual decision."
            )
        return
    if leaf.decision_type == "manual_rejected":
        if link is not None and (
            link.review_status != FilingEarningsReviewStatus.REJECTED
            or link.current_decision_id != leaf.pk
        ):
            raise FilingEarningsIntegrityError(
                "The rejected link does not match its manual decision."
            )


def _validate_reused_projection(
    *,
    decision: FilingEarningsDecision,
    link: FilingEarningsLink | None,
    reason_code: str,
) -> None:
    if decision.decision_type in _BINDING_DECISION_TYPES:
        if link is None or link.current_decision_id != decision.pk:
            raise FilingEarningsIntegrityError(
                "A binding decision must project onto its current link."
            )
        if (
            link.earnings_event_id != decision.target_event_id
            or link.release_filing_classification != decision.classification
            or link.confidence != decision.confidence
            or link.match_rule_version != decision.match_rule_version
            or link.classification_rule_version != decision.classification_rule_version
        ):
            raise FilingEarningsIntegrityError(
                "The current link does not match its binding decision."
            )
        return
    if reason_code == REASON_EXISTING_LINK_REVIEW_REQUIRED:
        if link is None:
            raise FilingEarningsIntegrityError(
                "The existing-link review decision lost its current link."
            )
        return
    if link is not None:
        raise FilingEarningsIntegrityError("A non-binding decision must not have a current link.")


def _load_filing_evidence(filing: Filing) -> tuple[SourceEvidence, SyncRun]:
    evidence = filing.source_evidence
    if evidence is None:
        raise FilingEarningsIntegrityError("Filing is missing its SEC SourceEvidence.")
    try:
        reference = resolve_source_evidence_reference(
            source_evidence=evidence,
            sync_run=None,
            target_type=DomainTargetType.FILING,
            target_id=filing.pk,
        )
    except ValueError as error:
        raise FilingEarningsIntegrityError(str(error)) from None
    return reference.evidence, reference.sync_run


def _validate_run_context(
    *,
    run: SyncRun,
    evidence: SourceEvidence,
    evidence_run: SyncRun,
) -> SyncRun:
    if run.source_id != evidence_run.source_id:
        raise InvalidFilingEarningsInput(
            "sync_run and Filing SourceEvidence must share one DataSource."
        )
    if (
        run.pk != evidence_run.pk
        and not RawDataObservation.objects.filter(
            sync_run_id=run.pk,
            raw_data_record_id=evidence.raw_data_record_id,
        ).exists()
    ):
        raise InvalidFilingEarningsInput("sync_run must have observed the Filing raw evidence.")
    return run


def _load_persisted_run(sync_run: SyncRun) -> SyncRun:
    if sync_run._state.adding or sync_run.pk is None:
        raise InvalidFilingEarningsInput("sync_run must be saved before use.")
    try:
        return SyncRun.objects.select_related("source").get(pk=sync_run.pk)
    except SyncRun.DoesNotExist as error:
        raise InvalidFilingEarningsInput("sync_run no longer exists.") from error


def _effective_decision(
    filing: Filing,
    relation_type: str,
) -> FilingEarningsDecision | None:
    decisions = list(
        FilingEarningsDecision.objects.filter(
            filing=filing,
            relation_type=relation_type,
        ).order_by("decided_at", "decision_key")
    )
    if not decisions:
        return None
    superseded_ids = {item.supersedes_id for item in decisions if item.supersedes_id is not None}
    leaves = [item for item in decisions if item.pk not in superseded_ids]
    if len(leaves) != 1:
        raise FilingEarningsIntegrityError(
            "Filing decision history must have exactly one effective leaf."
        )
    return cast(FilingEarningsDecision, leaves[0])


def _load_relation_links(
    filing: Filing,
    relation_type: str,
    *,
    lock: bool,
) -> list[FilingEarningsLink]:
    queryset = FilingEarningsLink.objects.filter(
        filing=filing,
        relation_type=relation_type,
    ).order_by("created_at", "id")
    if lock:
        queryset = queryset.select_for_update()
    return list(queryset)


def _load_canonical_target(target_event: EarningsEvent) -> EarningsEvent:
    _persisted_id(target_event, "target_event")
    try:
        event = EarningsEvent.objects.get(pk=target_event.pk)
    except EarningsEvent.DoesNotExist as error:
        raise InvalidFilingEarningsReview("target_event no longer exists.") from error
    if event.identity_status != "canonical":
        raise InvalidFilingEarningsReview("manual confirmation requires a canonical EarningsEvent.")
    return event


def _validate_review_target(
    *,
    action: str,
    relation_type: str,
    filing: Filing,
    target_event: EarningsEvent | None,
) -> None:
    if action == "reject":
        if target_event is not None:
            raise InvalidFilingEarningsReview("manual rejection does not take a target event.")
        return
    if target_event is None:
        raise InvalidFilingEarningsReview("manual confirmation requires a target event.")
    if relation_type == FilingEarningsRelationType.PERIODIC_FILING:
        if filing.period_of_report is None:
            raise InvalidFilingEarningsReview(
                "Periodic confirmation requires Filing.period_of_report."
            )
        # Manual authority may override an automatic miss, but the persisted
        # fiscal period identity must still be internally consistent.
        if target_event.period_end_date != filing.period_of_report:
            raise InvalidFilingEarningsReview(
                "Periodic confirmation target does not match the Filing period."
            )


def _verify_manual_replay(
    *,
    decision: FilingEarningsDecision,
    action: str,
    target_event: EarningsEvent | None,
    reason: str,
) -> None:
    expected_type = "manual_confirmed" if action == "confirm" else "manual_rejected"
    expected_status = "resolved" if action == "confirm" else "rejected"
    if decision.decision_type != expected_type or decision.status != expected_status:
        raise InvalidFilingEarningsReview(
            "request_id was already used for a different manual action."
        )
    if decision.target_event_id != (target_event.pk if target_event is not None else None):
        raise InvalidFilingEarningsReview(
            "request_id was already used with a different target event."
        )
    if decision.reason != reason:
        raise InvalidFilingEarningsReview("request_id was already used with a different reason.")


def _normalize_review_relation(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidFilingEarningsReview("relation_type must be a string.")
    normalized = value.strip().upper()
    if normalized not in {
        FilingEarningsRelationType.RELEASE_FILING,
        FilingEarningsRelationType.PERIODIC_FILING,
    }:
        raise InvalidFilingEarningsReview(
            "relation_type must be RELEASE_FILING or PERIODIC_FILING."
        )
    return normalized


def _required_text(value: str, value_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidFilingEarningsReview(f"{value_name} must be text.")
    normalized = value.strip()
    if not normalized:
        raise InvalidFilingEarningsReview(f"{value_name} must not be empty.")
    if len(normalized) > maximum_length:
        raise InvalidFilingEarningsReview(
            f"{value_name} must contain at most {maximum_length} characters."
        )
    return normalized


def _persisted_id(instance: object, value_name: str) -> uuid.UUID:
    state = getattr(instance, "_state", None)
    pk = getattr(instance, "pk", None)
    if state is None or getattr(state, "adding", True) or not isinstance(pk, uuid.UUID):
        raise InvalidFilingEarningsInput(f"{value_name} must be a saved model instance.")
    return pk


def _document_types(filing: Filing) -> tuple[str, ...]:
    values = (
        FilingDocument.objects.filter(filing=filing)
        .order_by("filename")
        .values_list("document_type", flat=True)
    )
    return tuple(sorted({value.strip().upper() for value in values if value.strip()}))


def _build_evidence_digest(*, filing: Filing, documents: tuple[str, ...]) -> str:
    payload = {
        "accepted_at": filing.accepted_at.isoformat(),
        "accession_number": filing.accession_number,
        "company_id": str(filing.company_id),
        "document_types": list(documents),
        "filing_id": str(filing.pk),
        "filing_url": filing.filing_url,
        "form_type": filing.form_type,
        "period_of_report": _iso_or_none(filing.period_of_report),
        "primary_document": filing.primary_document,
        "reported_items": filing.reported_items,
    }
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def _sorted_ids(events: Sequence[EarningsEvent]) -> list[str]:
    return sorted(str(event.pk) for event in events)


def _iso_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError("Canonical JSON timestamps must be date or datetime values.")
