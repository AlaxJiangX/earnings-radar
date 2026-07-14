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

        if effective_to <= obj.effective_from:
            raise MembershipServiceError(
                f"effective_to {effective_to} must be later than "
                f"effective_from {obj.effective_from}."
            )

        if obj.status not in ("announced", "active"):
            raise InvalidMembershipState(f"Membership {obj.pk} is {obj.status}, cannot be ended.")

        # True no-op: effective_to already matches AND status is already ended
        if obj.effective_to == effective_to and obj.status == IndexMembership.Status.ENDED:
            return MembershipEndResult(
                membership=obj,
                data_changes=(),
                audit_record=None,
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
    actor_user: User,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
) -> MembershipCorrectionResult:
    # actor_user context is always manual for corrections (no automatic self-correction)
    _resolve_membership_write_context(
        source_evidence=None,
        sync_run=None,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
        target_id=membership.pk,
    )

    with transaction.atomic():
        old = IndexMembership.objects.select_for_update().get(pk=membership.pk)

        if IndexMembership.objects.filter(supersedes=old).exists():
            raise AlreadyCorrected(f"Membership {old.pk} already has a replacement.")

        if old.status not in NORMATIVE_MEMBERSHIP_STATUSES:
            raise InvalidMembershipState(
                f"Membership {old.pk} is {old.status}, cannot be corrected."
            )

        # Verify listing still exists and is consistent with historical membership
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
            before={"status": old_status_before},
            after={"status": IndexMembership.Status.CORRECTED},
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )

        new_index = _cast_mapping_value(replacement_values, "index", old.index)
        new_listing = _cast_mapping_value(
            replacement_values, "security_listing", old.security_listing
        )
        new_status = _cast_mapping_value(replacement_values, "status", old_status_before)
        new_eff_from = _cast_mapping_value(replacement_values, "effective_from", old.effective_from)
        new_eff_to = _cast_mapping_value(replacement_values, "effective_to", old.effective_to)
        new_ann_date = _cast_mapping_value(
            replacement_values, "announcement_date", old.announcement_date
        )

        replacement = IndexMembership.objects.create(  # type: ignore[misc]
            supersedes=old,
            index=new_index,
            security_listing=new_listing,
            status=new_status,
            effective_from=new_eff_from,
            effective_to=new_eff_to,
            announcement_date=new_ann_date,
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
    results: list[MembershipCloseResult] = []

    with transaction.atomic():
        locked_listing = SecurityListing.objects.select_for_update().get(pk=listing.pk)

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
                if obj.effective_from > as_of_date:
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
                        f">= new_effective_to={new_effective_to} but "
                        f"as_of_date={as_of_date} is not in the future. "
                        f"Manual review required."
                    )

            elif obj.status == "ended":
                if obj.effective_to is None or obj.effective_to > new_effective_to:
                    corr_result = correct_membership(
                        membership=obj,
                        replacement_values={
                            "effective_to": new_effective_to,
                            "status": derive_status(
                                obj.effective_from, new_effective_to, as_of_date
                            ),
                        },
                        actor_user=actor_user,
                        reason=reason,
                        request_id=request_id,
                        ip_address=ip_address,
                    )
                    results.append(
                        MembershipCloseResult(
                            membership=obj,
                            action="corrected",
                            replacement=corr_result.replacement,
                            data_changes=corr_result.data_changes,
                            audit_records=corr_result.audit_records,
                        )
                    )
                else:
                    results.append(
                        MembershipCloseResult(
                            membership=obj,
                            action="skipped",
                            replacement=None,
                            data_changes=(),
                            audit_records=(),
                        )
                    )
            else:
                # announced or active, effective_from < new_effective_to
                old_effective_to = obj.effective_to
                old_status = obj.status
                new_status = derive_status(obj.effective_from, new_effective_to, as_of_date)

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

                dcs: list[DataChangeWriteResult] = []
                if old_effective_to != new_effective_to:
                    dcs.append(
                        record_data_change(
                            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
                            target_id=obj.pk,
                            field_name="effective_to",
                            old_value=(old_effective_to.isoformat() if old_effective_to else None),
                            new_value=new_effective_to.isoformat(),
                            rule_version=MEMBERSHIP_RULE_VERSION,
                            actor_user=actor_user,
                            reason=reason,
                            origin_key=request_id,
                        )
                    )
                if old_status != new_status:
                    dcs.append(
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

                audit = record_user_action(
                    actor_user=actor_user,
                    action=AuditRecord.Action.UPDATE,
                    target_type=AuditRecord.TargetType.INDEX_MEMBERSHIP,
                    target_id=obj.pk,
                    before={
                        "effective_to": (
                            old_effective_to.isoformat() if old_effective_to else None
                        ),
                        "status": old_status,
                    },
                    after={
                        "effective_to": new_effective_to.isoformat(),
                        "status": new_status,
                    },
                    reason=reason,
                    request_id=request_id,
                    ip_address=ip_address,
                )

                results.append(
                    MembershipCloseResult(
                        membership=obj,
                        action="ended",
                        replacement=None,
                        data_changes=tuple(dcs),
                        audit_records=(audit.record,),
                    )
                )

    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _cast_mapping_value(mapping: Mapping[str, object], key: str, default: object) -> object:
    return mapping.get(key, default)


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
    has_auto = source_evidence is not None or sync_run is not None
    has_human = actor_user is not None

    if has_auto and has_human:
        raise MembershipServiceError(
            "Cannot provide both automatic (source_evidence/sync_run) "
            "and manual (actor_user) provenance."
        )
    if has_auto:
        if source_evidence is None:
            raise MembershipServiceError("Automatic provenance requires source_evidence.")
        if sync_run is None:
            raise MembershipServiceError("Automatic provenance requires sync_run.")
    elif has_human:
        if not reason or not reason.strip():
            raise MembershipServiceError("Manual operations require non-empty reason.")
        if not request_id or not request_id.strip():
            raise MembershipServiceError("Manual operations require non-empty request_id.")
    else:
        raise MembershipServiceError("Must provide either source_evidence+sync_run or actor_user.")


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
    }
