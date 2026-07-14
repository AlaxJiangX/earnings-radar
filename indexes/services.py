from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, cast
from uuid import UUID as uuid_UUID
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.db.models import Q

from audit.models import AuditRecord, DataChange, SourceEvidence, SyncRun
from audit.services import (
    DataChangeWriteResult,
    record_data_change,
    record_system_action,
    record_user_action,
)
from audit.services.source_evidence import (
    resolve_source_evidence_reference,
)
from companies.models import SecurityListing
from indexes.models import NORMATIVE_MEMBERSHIP_STATUSES, IndexMembership, MarketIndex

if TYPE_CHECKING:
    from accounts.models import User

INDEX_ENABLED_RULE_VERSION = "market-index-enabled-v1"


class MarketIndexServiceError(ValueError):
    pass


class MarketIndexNotFound(MarketIndexServiceError):
    pass


class InvalidMarketIndexCode(MarketIndexServiceError):
    pass


@dataclass(frozen=True, slots=True)
class IndexToggleResult:
    index: MarketIndex
    enabled: bool
    changed: bool
    data_changes: tuple[DataChangeWriteResult, ...]
    audit_record: AuditRecord | None


def set_index_enabled(
    *,
    code: str,
    enabled: bool,
    actor_user: User,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
) -> IndexToggleResult:
    normalized_code = _normalize_code(code)

    with transaction.atomic():
        try:
            market_index = MarketIndex.objects.select_for_update().get(code=normalized_code)
        except MarketIndex.DoesNotExist as exc:
            raise MarketIndexNotFound(
                f"MarketIndex with code {normalized_code!r} does not exist."
            ) from exc

        if market_index.is_enabled == enabled:
            return IndexToggleResult(
                index=market_index,
                enabled=enabled,
                changed=False,
                data_changes=(),
                audit_record=None,
            )

        old_value = market_index.is_enabled

        market_index.is_enabled = enabled
        market_index.save(update_fields={"is_enabled", "updated_at"})

        data_change_result = record_data_change(
            target_type=DataChange.TargetType.MARKET_INDEX,
            target_id=market_index.pk,
            field_name="is_enabled",
            old_value=old_value,
            new_value=enabled,
            rule_version=INDEX_ENABLED_RULE_VERSION,
            actor_user=actor_user,
            reason=reason,
            origin_key=request_id,
        )

        audit_result = record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.UPDATE,
            target_type=AuditRecord.TargetType.MARKET_INDEX,
            target_id=market_index.pk,
            before={"is_enabled": old_value},
            after={"is_enabled": enabled},
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )

        return IndexToggleResult(
            index=market_index,
            enabled=enabled,
            changed=True,
            data_changes=(data_change_result,),
            audit_record=audit_result.record,
        )


def get_index_by_code(*, code: str) -> MarketIndex:
    normalized_code = _normalize_code(code)
    try:
        return MarketIndex.objects.get(code=normalized_code)
    except MarketIndex.DoesNotExist as exc:
        raise MarketIndexNotFound(
            f"MarketIndex with code {normalized_code!r} does not exist."
        ) from exc


def _normalize_code(code: str) -> str:
    if not isinstance(code, str):
        raise InvalidMarketIndexCode("code must be a string.")
    normalized = code.strip().upper()
    if normalized not in MarketIndex.Code.values:
        raise InvalidMarketIndexCode(f"Unknown index code {code!r}.")
    return normalized


# ---- IndexMembership services ----

MEMBERSHIP_RULE_VERSION = "index-membership-v1"


class MembershipServiceError(ValueError):
    pass


class MembershipIdentityConflict(MembershipServiceError):
    pass


class MembershipOverlapConflict(MembershipServiceError):
    pass


class MembershipIntegrityConflict(MembershipServiceError):
    pass


class InvalidMembershipState(MembershipServiceError):
    pass


class AlreadyCorrected(MembershipServiceError):
    pass


class CannotCancelPastEffective(MembershipServiceError):
    pass


class MembershipListingHistoryConflict(MembershipServiceError):
    pass


@dataclass(frozen=True, slots=True)
class MembershipWriteResult:
    membership: IndexMembership
    created: bool
    data_changes: tuple[DataChangeWriteResult, ...]
    audit_record: AuditRecord | None


@dataclass(frozen=True, slots=True)
class MembershipCloseResult:
    membership: IndexMembership
    action: str
    replacement: IndexMembership | None
    data_changes: tuple[DataChangeWriteResult, ...]
    audit_records: tuple[AuditRecord | None, ...]


@dataclass(frozen=True, slots=True)
class MembershipCorrectionResult:
    old_membership: IndexMembership
    replacement: IndexMembership
    data_changes: tuple[DataChangeWriteResult, ...]
    audit_records: tuple[AuditRecord | None, ...]


@dataclass(frozen=True, slots=True)
class MembershipEndResult:
    membership: IndexMembership
    data_changes: tuple[DataChangeWriteResult, ...]
    audit_record: AuditRecord | None


class _MembershipWriteContext:
    """Resolved provenance context for membership write operations."""

    __slots__ = (
        "is_auto",
        "actor_user",
        "reason",
        "request_id",
        "ip_address",
        "source_evidence",
        "sync_run",
    )

    def __init__(
        self,
        *,
        is_auto: bool,
        actor_user: User | None,
        reason: str,
        request_id: str,
        ip_address: str | None,
        source_evidence: SourceEvidence | None,
        sync_run: SyncRun | None,
    ):
        self.is_auto = is_auto
        self.actor_user = actor_user
        self.reason = reason
        self.request_id = request_id
        self.ip_address = ip_address
        self.source_evidence = source_evidence
        self.sync_run = sync_run


def derive_status(
    effective_from: date,
    effective_to: date | None,
    as_of_date: date,
) -> str:
    if as_of_date < effective_from:
        return IndexMembership.Status.ANNOUNCED
    if effective_to is not None and effective_to <= as_of_date:
        return IndexMembership.Status.ENDED
    return IndexMembership.Status.ACTIVE


# ---------------------------------------------------------------------------
# create_index_membership
# ---------------------------------------------------------------------------


def create_index_membership(
    *,
    index: MarketIndex,
    security_listing: SecurityListing,
    status: str,
    effective_from: date,
    effective_to: date | None = None,
    announcement_date: date | None = None,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
    membership_id: uuid_UUID | None = None,
) -> MembershipWriteResult:
    _validate_membership_status(status)
    _validate_membership_dates(effective_from, effective_to)
    _validate_membership_source(source_evidence, sync_run, actor_user, reason, request_id)

    target_id = membership_id if membership_id is not None else uuid4()

    # Resolve provenance *before* entering the main transaction so that
    # resolve_source_evidence_reference validates evidence/sync_run linkage
    # using persisted stable identifiers.
    context = _resolve_membership_write_context(
        source_evidence=source_evidence,
        sync_run=sync_run,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
        target_id=target_id,
    )
    evidence = context.source_evidence

    with transaction.atomic():
        # Re-read + lock domain parents
        index_obj = _locked_market_index(index)
        listing_obj = _locked_security_listing(security_listing)
        _validate_membership_within_listing(
            effective_from=effective_from,
            effective_to=effective_to,
            listing_effective_from=listing_obj.effective_from,
            listing_effective_to=listing_obj.effective_to,
        )

        try:
            with transaction.atomic():
                membership = IndexMembership.objects.create(
                    id=target_id,
                    index=index_obj,
                    security_listing=listing_obj,
                    status=status,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    announcement_date=announcement_date,
                    source_evidence=evidence,
                )
        except IntegrityError as error:
            # Savepoint rolled back; classify the conflict
            existing = IndexMembership.objects.filter(
                index=index_obj,
                security_listing=listing_obj,
                effective_from=effective_from,
                status__in=NORMATIVE_MEMBERSHIP_STATUSES,
            ).first()

            if existing is not None:
                _verify_membership_equivalent(
                    existing,
                    status,
                    effective_to,
                    announcement_date,
                    evidence,
                )
                return MembershipWriteResult(
                    membership=existing,
                    created=False,
                    data_changes=(),
                    audit_record=None,
                )

            # Check for date-range overlap
            overlap_q = IndexMembership.objects.filter(
                security_listing=listing_obj,
                index=index_obj,
                status__in=NORMATIVE_MEMBERSHIP_STATUSES,
                effective_from__lt=effective_to if effective_to is not None else date(9999, 12, 31),
            )
            if effective_to is None:
                overlap_q = overlap_q.filter(
                    Q(effective_to__isnull=True) | Q(effective_to__gt=effective_from),
                )
            else:
                overlap_q = overlap_q.filter(
                    Q(effective_to__isnull=True) | Q(effective_to__gt=effective_from),
                )
            overlapping = overlap_q.first()

            if overlapping is not None:
                raise MembershipOverlapConflict(
                    f"New membership [{effective_from}, {effective_to}) "
                    f"overlaps existing membership {overlapping.pk} "
                    f"[{overlapping.effective_from}, {overlapping.effective_to})."
                ) from None

            raise MembershipIntegrityConflict(
                "Membership creation failed with an unexpected IntegrityError."
            ) from error

        # Created successfully – record only CREATE AuditRecord, no initial DataChange
        audit = _write_membership_audit(
            membership=membership,
            action=AuditRecord.Action.CREATE,
            before=None,
            after=_serialize_membership(membership),
            context=context,
        )

        return MembershipWriteResult(
            membership=membership,
            created=True,
            data_changes=(),
            audit_record=audit,
        )


# ---------------------------------------------------------------------------
# end_membership
# ---------------------------------------------------------------------------


def end_membership(
    *,
    membership: IndexMembership,
    effective_to: date,
    source_evidence: SourceEvidence | None = None,
    sync_run: SyncRun | None = None,
    actor_user: User | None = None,
    reason: str = "",
    request_id: str = "",
    ip_address: str | None = None,
) -> MembershipEndResult:
    _validate_membership_source(source_evidence, sync_run, actor_user, reason, request_id)

    context = _resolve_membership_write_context(
        source_evidence=source_evidence,
        sync_run=sync_run,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
        target_id=membership.pk,
    )

    with transaction.atomic():
        obj = IndexMembership.objects.select_for_update().get(pk=membership.pk)

        # --- Idempotency ordering (lock first, then classify) ---

        # Already ended with same effective_to → pure no-op
        if obj.status == IndexMembership.Status.ENDED and obj.effective_to == effective_to:
            return MembershipEndResult(
                membership=obj,
                data_changes=(),
                audit_record=None,
            )

        # Already ended but with different effective_to → reject
        if obj.status == IndexMembership.Status.ENDED:
            raise InvalidMembershipState(
                f"Membership {obj.pk} is already ended with effective_to={obj.effective_to}. "
                f"Use correct_membership to change the historical end date, "
                f"not end_membership."
            )

        # Corrected and cancelled are invalid starting states for end
        if obj.status not in ("announced", "active"):
            raise InvalidMembershipState(f"Membership {obj.pk} is {obj.status}, cannot be ended.")

        # Requested effective_to must be later than the database effective_from
        if effective_to <= obj.effective_from:
            raise MembershipServiceError(
                f"effective_to {effective_to} must be later than "
                f"effective_from {obj.effective_from}."
            )

        old_effective_to = obj.effective_to
        old_status = obj.status

        obj.effective_to = effective_to
        obj.status = IndexMembership.Status.ENDED
        obj.save(update_fields=["effective_to", "status", "updated_at"])

        data_changes: list[DataChangeWriteResult] = []

        if old_effective_to != effective_to:
            dc = record_data_change(
                target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
                target_id=obj.pk,
                field_name="effective_to",
                old_value=old_effective_to.isoformat() if old_effective_to else None,
                new_value=effective_to.isoformat(),
                rule_version=MEMBERSHIP_RULE_VERSION,
                source_evidence=context.source_evidence,
                sync_run=context.sync_run,
                actor_user=context.actor_user,
                reason=context.reason,
                origin_key=context.request_id,
            )
            data_changes.append(dc)

        if old_status != IndexMembership.Status.ENDED:
            dc = record_data_change(
                target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
                target_id=obj.pk,
                field_name="status",
                old_value=old_status,
                new_value=IndexMembership.Status.ENDED,
                rule_version=MEMBERSHIP_RULE_VERSION,
                source_evidence=context.source_evidence,
                sync_run=context.sync_run,
                actor_user=context.actor_user,
                reason=context.reason,
                origin_key=context.request_id,
            )
            data_changes.append(dc)

        audit = _write_membership_audit(
            membership=obj,
            action=AuditRecord.Action.UPDATE,
            before={
                "effective_to": old_effective_to.isoformat() if old_effective_to else None,
                "status": old_status,
            },
            after={
                "effective_to": effective_to.isoformat(),
                "status": IndexMembership.Status.ENDED,
            },
            context=context,
        )

        return MembershipEndResult(
            membership=obj,
            data_changes=tuple(data_changes),
            audit_record=audit,
        )


# ---------------------------------------------------------------------------
# cancel_membership
# ---------------------------------------------------------------------------


def cancel_membership(
    *,
    membership: IndexMembership,
    as_of_date: date,
    actor_user: User,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
) -> MembershipWriteResult:
    with transaction.atomic():
        obj = IndexMembership.objects.select_for_update().get(pk=membership.pk)

        if obj.status != "announced":
            raise InvalidMembershipState("Only announced memberships can be cancelled.")

        if obj.effective_from <= as_of_date:
            raise CannotCancelPastEffective(
                f"Membership {obj.pk} effective_from={obj.effective_from} "
                f"is on or before {as_of_date}; it has taken effect and cannot be cancelled."
            )

        old_status = obj.status
        obj.status = IndexMembership.Status.CANCELLED
        obj.save(update_fields=["status", "updated_at"])

        dc = record_data_change(
            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
            target_id=obj.pk,
            field_name="status",
            old_value=old_status,
            new_value=IndexMembership.Status.CANCELLED,
            rule_version=MEMBERSHIP_RULE_VERSION,
            actor_user=actor_user,
            reason=reason,
            origin_key=request_id,
        )

        audit = record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.DEACTIVATE,
            target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
            target_id=obj.pk,
            before={"status": old_status},
            after={"status": IndexMembership.Status.CANCELLED},
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )

        return MembershipWriteResult(
            membership=obj,
            created=False,
            data_changes=(dc,),
            audit_record=audit.record,
        )


# ---------------------------------------------------------------------------
# correct_membership
# ---------------------------------------------------------------------------


def correct_membership(
    *,
    membership: IndexMembership,
    replacement_values: Mapping[str, object],
    source_evidence: SourceEvidence | None = None,
    actor_user: User,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
) -> MembershipCorrectionResult:
    """Atomically correct a membership.

    The original record is set to ``corrected`` and a *replacement* record is
    created in the same transaction.  Only specific fields are allowed in
    *replacement_values* and the replacement must pass all validation before
    any mutation occurs.
    """
    # Validate replacement field whitelist *before* any mutation
    _validate_replacement_fields(replacement_values)

    replacement_id = uuid4()

    # Resolve write context (manual correction)
    _validate_membership_source(
        source_evidence=source_evidence,
        sync_run=None,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
    )
    context = _resolve_membership_write_context(
        source_evidence=source_evidence,
        sync_run=None,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
        target_id=replacement_id,
    )
    resolved_evidence = context.source_evidence

    with transaction.atomic():
        old = IndexMembership.objects.select_for_update().get(pk=membership.pk)

        # Prevent double-correction
        if IndexMembership.objects.filter(supersedes=old).exists():
            raise AlreadyCorrected(f"Membership {old.pk} already has a replacement.")

        if old.status not in NORMATIVE_MEMBERSHIP_STATUSES:
            raise InvalidMembershipState(
                f"Membership {old.pk} is {old.status}, cannot be corrected."
            )

        # --- Extract and validate replacement values ---

        new_index = _extract_replacement(replacement_values, "index", old.index, MarketIndex)
        new_listing = _extract_replacement(
            replacement_values, "security_listing", old.security_listing, SecurityListing
        )
        new_status = _extract_replacement(replacement_values, "status", old.status, str)
        new_eff_from = _extract_replacement(
            replacement_values, "effective_from", old.effective_from, date
        )
        new_eff_to = _extract_replacement(
            replacement_values, "effective_to", old.effective_to, (date, type(None))
        )
        new_ann_date = _extract_replacement(
            replacement_values, "announcement_date", old.announcement_date, (date, type(None))
        )

        # last_verified_at: distinguish "not provided" from "explicitly None"
        if "last_verified_at" in replacement_values:
            new_last_verified = replacement_values["last_verified_at"]
        else:
            # Preserve exact old value, including None
            new_last_verified = old.last_verified_at

        # source_evidence: ONLY accepted via the top-level parameter, NOT replacement_values.
        # _validate_replacement_fields already rejects it in replacement_values.

        # Validate replacement invariants
        _validate_replacement_membership(
            status=new_status,  # type: ignore[arg-type]
            effective_from=new_eff_from,  # type: ignore[arg-type]
            effective_to=new_eff_to,  # type: ignore[arg-type]
            announcement_date=new_ann_date,  # type: ignore[arg-type]
            source_evidence=resolved_evidence,
        )

        # Re-read + lock replacement index and listing
        locked_index = _locked_market_index(new_index)  # type: ignore[arg-type]
        locked_listing = _locked_security_listing(new_listing)  # type: ignore[arg-type]

        # Validate replacement within listing boundaries
        _validate_membership_within_listing(
            effective_from=new_eff_from,  # type: ignore[arg-type]
            effective_to=new_eff_to,  # type: ignore[arg-type]
            listing_effective_from=locked_listing.effective_from,
            listing_effective_to=locked_listing.effective_to,
        )

        # Prevent exact self-supersede: replacement must differ from original
        _prevent_self_supersede(
            old,
            locked_index,
            locked_listing,
            new_eff_from,  # type: ignore[arg-type]
            new_eff_to,  # type: ignore[arg-type]
            new_status,  # type: ignore[arg-type]
            new_ann_date,  # type: ignore[arg-type]
            new_last_verified,
            new_source_evidence=resolved_evidence,
        )

        # --- Verify old listing still exists and is consistent ---
        try:
            listing = SecurityListing.objects.select_for_update().get(pk=old.security_listing_id)
        except SecurityListing.DoesNotExist as exc:
            raise MembershipListingHistoryConflict(
                f"SecurityListing {old.security_listing_id} no longer exists – "
                f"cannot correct membership {old.pk}."
            ) from exc

        if old.effective_from < listing.effective_from:
            raise MembershipListingHistoryConflict(
                f"Membership {old.pk} effective_from={old.effective_from} "
                f"is before listing {listing.pk} effective_from={listing.effective_from}."
            )
        if listing.effective_to is not None and (
            old.effective_to is None or old.effective_to > listing.effective_to
        ):
            raise MembershipListingHistoryConflict(
                f"Membership {old.pk} effective_to={old.effective_to} "
                f"exceeds listing {listing.pk} effective_to={listing.effective_to}."
            )

        # --- Mutate: set old to corrected, create replacement ---
        old_status_before = old.status
        old.status = IndexMembership.Status.CORRECTED
        old.save(update_fields=["status", "updated_at"])

        status_dc = record_data_change(
            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
            target_id=old.pk,
            field_name="status",
            old_value=old_status_before,
            new_value=IndexMembership.Status.CORRECTED,
            rule_version=MEMBERSHIP_RULE_VERSION,
            actor_user=actor_user,
            reason=reason,
            origin_key=request_id,
        )

        old_audit = record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.MANUAL_CORRECTION,
            target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
            target_id=old.pk,
            before=_serialize_membership(old),
            after={
                "status": IndexMembership.Status.CORRECTED,
                "effective_to": old.effective_to.isoformat() if old.effective_to else None,
            },
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )

        replacement = IndexMembership.objects.create(  # type: ignore[misc]
            id=replacement_id,
            supersedes=old,
            index=locked_index,
            security_listing=locked_listing,
            status=new_status,
            effective_from=new_eff_from,
            effective_to=new_eff_to,
            announcement_date=new_ann_date,
            last_verified_at=new_last_verified,
            source_evidence=resolved_evidence,
        )

        replacement_audit = record_user_action(
            actor_user=actor_user,
            action=AuditRecord.Action.CREATE,
            target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
            target_id=replacement.pk,
            before=None,
            after=_serialize_membership(replacement),
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )

        return MembershipCorrectionResult(
            old_membership=old,
            replacement=replacement,
            data_changes=(status_dc,),
            audit_records=(old_audit.record, replacement_audit.record),
        )


# ---------------------------------------------------------------------------
# close_memberships_for_listing
# ---------------------------------------------------------------------------


def close_memberships_for_listing(
    *,
    listing: SecurityListing,
    new_effective_to: date,
    as_of_date: date,
    actor_user: User,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
) -> list[MembershipCloseResult]:
    """Close (end/cancel) all normative memberships for a listing.

    - Memberships whose ``effective_from < new_effective_to`` are ended at
      *new_effective_to* (status derived via ``derive_status``).
    - Memberships whose ``effective_from >= new_effective_to`` are cancelled
      **only** if they are ``announced`` AND ``effective_from > as_of_date``.
      All other states (future active, future ended, past effective, etc.)
      raise ``MembershipListingHistoryConflict``.
    - *new_effective_to* must be strictly greater than the listing's current
      ``effective_from``.
    - The entire batch is atomic: a conflict rolls back all prior mutations,
      DataChanges and AuditRecords.
    """
    results: list[MembershipCloseResult] = []

    _validate_membership_source(
        source_evidence=None,
        sync_run=None,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
    )

    with transaction.atomic():
        locked_listing = SecurityListing.objects.select_for_update().get(pk=listing.pk)

        if new_effective_to <= locked_listing.effective_from:
            raise MembershipServiceError(
                f"new_effective_to {new_effective_to} must be later than "
                f"listing effective_from {locked_listing.effective_from}."
            )

        memberships = list(
            IndexMembership.objects.select_for_update()
            .filter(
                security_listing=locked_listing,
                status__in=NORMATIVE_MEMBERSHIP_STATUSES,
            )
            .order_by("effective_from")
        )

        for obj in memberships:
            if obj.effective_from >= new_effective_to:
                # Membership starts on or after listing ends.
                # Only future announced memberships can be cancelled.
                if (
                    obj.status == IndexMembership.Status.ANNOUNCED
                    and obj.effective_from > as_of_date
                ):
                    old_status = obj.status
                    obj.status = IndexMembership.Status.CANCELLED
                    obj.save(update_fields=["status", "updated_at"])

                    dc = record_data_change(
                        target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
                        target_id=obj.pk,
                        field_name="status",
                        old_value=old_status,
                        new_value=IndexMembership.Status.CANCELLED,
                        rule_version=MEMBERSHIP_RULE_VERSION,
                        actor_user=actor_user,
                        reason=reason,
                        origin_key=request_id,
                    )
                    audit = record_user_action(
                        actor_user=actor_user,
                        action=AuditRecord.Action.DEACTIVATE,
                        target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
                        target_id=obj.pk,
                        before=_serialize_membership(obj),
                        after={
                            "status": IndexMembership.Status.CANCELLED,
                            "effective_to": (
                                obj.effective_to.isoformat() if obj.effective_to else None
                            ),
                        },
                        reason=reason,
                        request_id=request_id,
                        ip_address=ip_address,
                    )
                    results.append(
                        MembershipCloseResult(
                            membership=obj,
                            action="cancelled",
                            replacement=None,
                            data_changes=(dc,),
                            audit_records=(audit.record,),
                        )
                    )
                else:
                    raise MembershipListingHistoryConflict(
                        f"Membership {obj.pk} effective_from={obj.effective_from} "
                        f">= new_effective_to={new_effective_to}, "
                        f"status={obj.status}, as_of_date={as_of_date}. "
                        f"Only future announced memberships can be cancelled. "
                        f"Manual review required."
                    )
            else:
                # effective_from < new_effective_to → close the membership
                old_effective_to = obj.effective_to
                old_status = obj.status
                new_status = derive_status(obj.effective_from, new_effective_to, as_of_date)

                # Ended memberships with old.effective_to > new_effective_to
                # must go through corrected+replacement, not direct modification.
                if (
                    old_status == IndexMembership.Status.ENDED
                    and old_effective_to is not None
                    and old_effective_to > new_effective_to
                ):
                    # --- ended → corrected + replacement ---
                    replacement_id = uuid4()

                    old_status_before = old_status
                    old_effective_to_before = old_effective_to

                    # Mark old as corrected
                    obj.status = IndexMembership.Status.CORRECTED
                    obj.save(update_fields=["status", "updated_at"])

                    status_dc = record_data_change(
                        target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
                        target_id=obj.pk,
                        field_name="status",
                        old_value=old_status_before,
                        new_value=IndexMembership.Status.CORRECTED,
                        rule_version=MEMBERSHIP_RULE_VERSION,
                        actor_user=actor_user,
                        reason=reason,
                        origin_key=request_id,
                    )

                    old_audit = record_user_action(
                        actor_user=actor_user,
                        action=AuditRecord.Action.MANUAL_CORRECTION,
                        target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
                        target_id=obj.pk,
                        before=_serialize_membership(obj),
                        after={
                            "status": IndexMembership.Status.CORRECTED,
                            "effective_to": old_effective_to_before.isoformat(),
                        },
                        reason=reason,
                        request_id=request_id,
                        ip_address=ip_address,
                    )

                    # Create replacement with shortened effective_to
                    replacement = IndexMembership.objects.create(
                        id=replacement_id,
                        supersedes=obj,
                        index_id=obj.index_id,
                        security_listing_id=obj.security_listing_id,
                        status=new_status,
                        effective_from=obj.effective_from,
                        effective_to=new_effective_to,
                        announcement_date=obj.announcement_date,
                        last_verified_at=obj.last_verified_at,
                        source_evidence=obj.source_evidence,
                    )

                    replacement_audit = record_user_action(
                        actor_user=actor_user,
                        action=AuditRecord.Action.CREATE,
                        target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
                        target_id=replacement.pk,
                        before=None,
                        after=_serialize_membership(replacement),
                        reason=reason,
                        request_id=request_id,
                        ip_address=ip_address,
                    )

                    results.append(
                        MembershipCloseResult(
                            membership=replacement,
                            action="corrected",
                            replacement=replacement,
                            data_changes=(status_dc,),
                            audit_records=(old_audit.record, replacement_audit.record),
                        )
                    )
                    continue

                # Not ended, or ended but old.effective_to <= new_effective_to
                if old_effective_to == new_effective_to and old_status == new_status:
                    results.append(
                        MembershipCloseResult(
                            membership=obj,
                            action="skipped",
                            replacement=None,
                            data_changes=(),
                            audit_records=(),
                        )
                    )
                    continue

                obj.effective_to = new_effective_to
                obj.status = new_status
                obj.save(update_fields=["effective_to", "status", "updated_at"])

                dc_list: list[DataChangeWriteResult] = []
                if old_effective_to != new_effective_to:
                    dc_list.append(
                        record_data_change(
                            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
                            target_id=obj.pk,
                            field_name="effective_to",
                            old_value=old_effective_to.isoformat() if old_effective_to else None,
                            new_value=new_effective_to.isoformat(),
                            rule_version=MEMBERSHIP_RULE_VERSION,
                            actor_user=actor_user,
                            reason=reason,
                            origin_key=request_id,
                        )
                    )
                if old_status != new_status:
                    dc_list.append(
                        record_data_change(
                            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
                            target_id=obj.pk,
                            field_name="status",
                            old_value=old_status,
                            new_value=new_status,
                            rule_version=MEMBERSHIP_RULE_VERSION,
                            actor_user=actor_user,
                            reason=reason,
                            origin_key=request_id,
                        )
                    )

                # Fix 2: action label reflects final status
                action_label = _close_action_label(
                    old_status, new_status, as_of_date, obj.effective_from
                )

                audit = record_user_action(
                    actor_user=actor_user,
                    action=AuditRecord.Action.UPDATE,
                    target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
                    target_id=obj.pk,
                    before=_serialize_membership_from_fields(
                        index_id=str(obj.index_id),
                        security_listing_id=str(obj.security_listing_id),
                        status=old_status,
                        effective_from=obj.effective_from.isoformat(),
                        effective_to=old_effective_to.isoformat() if old_effective_to else None,
                        announcement_date=(
                            obj.announcement_date.isoformat() if obj.announcement_date else None
                        ),
                        last_verified_at=(
                            obj.last_verified_at.isoformat() if obj.last_verified_at else None
                        ),
                        source_evidence_id=(
                            str(obj.source_evidence_id) if obj.source_evidence_id else None
                        ),
                        supersedes_id=str(obj.supersedes_id) if obj.supersedes_id else None,
                    ),
                    after=_serialize_membership(obj),
                    reason=reason,
                    request_id=request_id,
                    ip_address=ip_address,
                )

                results.append(
                    MembershipCloseResult(
                        membership=obj,
                        action=action_label,
                        replacement=None,
                        data_changes=tuple(dc_list),
                        audit_records=(audit.record,),
                    )
                )

    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _prevent_self_supersede(
    old: IndexMembership,
    new_index: MarketIndex,
    new_listing: SecurityListing,
    new_eff_from: date,
    new_eff_to: date | None,
    new_status: str,
    new_ann_date: date | None,
    new_last_verified: object,
    new_source_evidence: SourceEvidence | None = None,
) -> None:
    """Reject replacement that is identical to the original on all fields.

    A new source_evidence that differs from old.source_evidence_id is treated
    as a meaningful change, allowing evidence-only corrections.
    """
    new_ev_id = new_source_evidence.pk if new_source_evidence else None
    if (
        new_index.pk == old.index_id
        and new_listing.pk == old.security_listing_id
        and new_eff_from == old.effective_from
        and new_eff_to == old.effective_to
        and new_status == old.status
        and new_ann_date == old.announcement_date
        and new_last_verified == old.last_verified_at
        and new_ev_id == old.source_evidence_id
    ):
        raise MembershipServiceError(
            "Replacement is identical to the original membership. At least one field must differ."
        )


# ---- Replacement validation helpers ----

_ALLOWED_REPLACEMENT_FIELDS = frozenset(
    {
        "index",
        "security_listing",
        "status",
        "effective_from",
        "effective_to",
        "announcement_date",
        "last_verified_at",
    }
)

_REPLACEMENT_STATUSES = frozenset(
    {IndexMembership.Status.ANNOUNCED, IndexMembership.Status.ACTIVE, IndexMembership.Status.ENDED}
)


def _validate_replacement_fields(replacement_values: Mapping[str, object]) -> None:
    forbidden = set(replacement_values.keys()) - _ALLOWED_REPLACEMENT_FIELDS
    if forbidden:
        raise MembershipServiceError(
            f"Replacement contains forbidden fields: {', '.join(sorted(forbidden))}."
        )


def _extract_replacement(
    mapping: Mapping[str, object],
    key: str,
    default: object,
    expected_type: type | tuple[type, ...],
) -> object:
    value = mapping.get(key, default)
    if not isinstance(value, expected_type):
        raise MembershipServiceError(
            f"Replacement field {key!r} must be of type {expected_type!r}, "
            f"got {type(value).__name__!r}."
        )
    return value


def _validate_replacement_membership(
    *,
    status: str,
    effective_from: date,
    effective_to: date | None,
    announcement_date: date | None,
    source_evidence: SourceEvidence | None,
) -> None:
    if status not in _REPLACEMENT_STATUSES:
        raise MembershipServiceError(
            f"Replacement status {status!r} is not allowed. "
            f"Only announced, active, and ended are valid."
        )
    if effective_to is not None and effective_to <= effective_from:
        raise MembershipServiceError("Replacement effective_to must be later than effective_from.")
    if status == IndexMembership.Status.ENDED and effective_to is None:
        raise MembershipServiceError("Replacement with ended status requires effective_to.")
    if announcement_date is not None and announcement_date > effective_from:
        raise MembershipServiceError(
            "Replacement announcement_date must not be after effective_from."
        )


def _validate_membership_status(status: str) -> None:
    if status not in IndexMembership.Status.values:
        raise MembershipServiceError(f"Invalid status {status!r}.")


def _validate_membership_dates(effective_from: date, effective_to: date | None) -> None:
    if effective_to is not None and effective_to <= effective_from:
        raise MembershipServiceError("effective_to must be later than effective_from.")


def _validate_membership_source(
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    request_id: str,
) -> None:
    """Enforce frozen provenance classification (auto vs manual).

    Auto:  sync_run is not None AND actor_user is None AND source_evidence is not None
    Manual: actor_user is not None AND sync_run is None AND reason/request_id non-empty

    Reject: SyncRun without SourceEvidence, SourceEvidence without actor_user or
    SyncRun, actor_user + SyncRun together, both sources empty, blank reason, blank request_id.
    """
    has_sync_run = sync_run is not None
    has_actor = actor_user is not None
    has_evidence = source_evidence is not None

    # Reject: actor_user + SyncRun mixed
    if has_actor and has_sync_run:
        raise MembershipServiceError("Cannot provide both actor_user and sync_run provenance.")

    # Reject: SyncRun without SourceEvidence
    if has_sync_run and not has_evidence:
        raise MembershipServiceError("Automatic provenance requires source_evidence.")

    # Reject: SourceEvidence alone without actor_user or SyncRun
    if has_evidence and not has_actor and not has_sync_run:
        raise MembershipServiceError(
            "SourceEvidence requires either actor_user or sync_run provenance."
        )

    # Reject: neither source
    if not has_actor and not has_sync_run:
        raise MembershipServiceError("Must provide either source_evidence+sync_run or actor_user.")

    # Manual validation
    if has_actor:
        if not reason or not reason.strip():
            raise MembershipServiceError("Manual operations require non-empty reason.")
        if not request_id or not request_id.strip():
            raise MembershipServiceError("Manual operations require non-empty request_id.")


def _validate_membership_within_listing(
    *,
    effective_from: date,
    effective_to: date | None,
    listing_effective_from: date,
    listing_effective_to: date | None,
) -> None:
    if effective_from < listing_effective_from:
        raise MembershipServiceError(
            f"Membership effective_from {effective_from} is before "
            f"listing effective_from {listing_effective_from}."
        )
    if listing_effective_to is not None:
        if effective_to is None:
            raise MembershipServiceError(
                f"Membership with no effective_to cannot be created under "
                f"a listing that ends at {listing_effective_to}."
            )
        if effective_to > listing_effective_to:
            raise MembershipServiceError(
                f"Membership effective_to {effective_to} exceeds "
                f"listing effective_to {listing_effective_to}."
            )


def _verify_membership_equivalent(
    existing: IndexMembership,
    status: str,
    effective_to: date | None,
    announcement_date: date | None,
    source_evidence: SourceEvidence | None,
) -> None:
    if (
        existing.status != status
        or existing.effective_to != effective_to
        or existing.announcement_date != announcement_date
        or existing.source_evidence_id != (source_evidence.pk if source_evidence else None)
    ):
        raise MembershipIdentityConflict(
            f"An existing membership {existing.pk} with the same identity "
            f"(index, security_listing, effective_from) has different field values. "
            f"Use end + re-create or correct to update."
        )


def _resolve_membership_write_context(
    *,
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    request_id: str,
    ip_address: str | None,
    target_type: str,
    target_id: uuid_UUID,
) -> _MembershipWriteContext:
    is_auto = actor_user is None

    if is_auto:
        if source_evidence is None or sync_run is None:
            raise MembershipServiceError(
                "Automatic write context requires both source_evidence and sync_run."
            )
        evidence_ref = resolve_source_evidence_reference(
            source_evidence=source_evidence,
            sync_run=sync_run,
            target_type=target_type,
            target_id=target_id,
            field_names=(),
        )
        resolved_evidence = evidence_ref.evidence
        resolved_sync_run = evidence_ref.sync_run
        resolved_request_id = (
            request_id.strip() if request_id.strip() else f"sync-run:{resolved_sync_run.pk}"
        )
        return _MembershipWriteContext(
            is_auto=True,
            actor_user=None,
            reason=reason,
            request_id=resolved_request_id,
            ip_address=None,
            source_evidence=resolved_evidence,
            sync_run=resolved_sync_run,
        )

    # Manual
    if not reason.strip():
        raise MembershipServiceError("Manual operations require non-empty reason.")
    if not request_id.strip():
        raise MembershipServiceError("Manual operations require non-empty request_id.")

    resolved_evidence_manual: SourceEvidence | None = None
    if source_evidence is not None:
        evidence_ref = resolve_source_evidence_reference(
            source_evidence=source_evidence,
            sync_run=None,
            target_type=target_type,
            target_id=target_id,
            field_names=(),
        )
        resolved_evidence_manual = evidence_ref.evidence

    return _MembershipWriteContext(
        is_auto=False,
        actor_user=actor_user,
        reason=reason.strip(),
        request_id=request_id.strip(),
        ip_address=ip_address,
        source_evidence=resolved_evidence_manual,
        sync_run=None,
    )


def _locked_market_index(index: MarketIndex) -> MarketIndex:
    try:
        return MarketIndex.objects.select_for_update().get(pk=index.pk)
    except MarketIndex.DoesNotExist as exc:
        raise MembershipServiceError(f"MarketIndex {index.pk} does not exist.") from exc


def _locked_security_listing(listing: SecurityListing) -> SecurityListing:
    try:
        return SecurityListing.objects.select_for_update().get(pk=listing.pk)
    except SecurityListing.DoesNotExist as exc:
        raise MembershipServiceError(f"SecurityListing {listing.pk} does not exist.") from exc


def _write_membership_audit(
    *,
    membership: IndexMembership,
    action: str,
    before: object,
    after: object,
    context: _MembershipWriteContext,
) -> AuditRecord:
    if context.is_auto:
        assert context.sync_run is not None
        result = record_system_action(
            sync_run=context.sync_run,
            action=action,
            target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
            target_id=membership.pk,
            before=before,
            after=after,
            reason=context.reason,
            request_id=context.request_id,
        )
    else:
        result = record_user_action(
            actor_user=cast("User", context.actor_user),
            action=action,
            target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
            target_id=membership.pk,
            before=before,
            after=after,
            reason=context.reason,
            request_id=context.request_id,
            ip_address=context.ip_address,
        )
    return result.record


def _close_action_label(
    old_status: str,
    new_status: str,
    as_of_date: date,
    effective_from: date,
) -> str:
    """Return the semantically accurate action label for close_memberships_for_listing."""
    if old_status == IndexMembership.Status.ANNOUNCED and effective_from > as_of_date:
        if new_status == IndexMembership.Status.CANCELLED:
            return "cancelled"
        return "announced"
    if new_status == IndexMembership.Status.ENDED:
        if old_status == IndexMembership.Status.ENDED:
            # ended with old.effective_to <= new_effective_to
            return "ended"
        return "ended"
    if new_status == IndexMembership.Status.ACTIVE:
        return "active"
    return new_status


def _serialize_membership_from_fields(
    *,
    index_id: str,
    security_listing_id: str,
    status: str,
    effective_from: str,
    effective_to: str | None,
    announcement_date: str | None,
    last_verified_at: str | None,
    source_evidence_id: str | None,
    supersedes_id: str | None,
) -> dict[str, object]:
    """Serialize a membership snapshot from pre-extracted field values."""
    return {
        "index_id": index_id,
        "security_listing_id": security_listing_id,
        "status": status,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "announcement_date": announcement_date,
        "last_verified_at": last_verified_at,
        "source_evidence_id": source_evidence_id,
        "supersedes_id": supersedes_id,
    }


def _serialize_membership(membership: IndexMembership) -> dict[str, object]:
    return {
        "index_id": str(membership.index_id),
        "security_listing_id": str(membership.security_listing_id),
        "status": membership.status,
        "effective_from": membership.effective_from.isoformat(),
        "effective_to": (membership.effective_to.isoformat() if membership.effective_to else None),
        "announcement_date": (
            membership.announcement_date.isoformat() if membership.announcement_date else None
        ),
        "last_verified_at": (
            membership.last_verified_at.isoformat() if membership.last_verified_at else None
        ),
        "source_evidence_id": (
            str(membership.source_evidence_id) if membership.source_evidence_id else None
        ),
        "supersedes_id": str(membership.supersedes_id) if membership.supersedes_id else None,
    }
