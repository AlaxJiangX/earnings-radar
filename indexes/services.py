from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.db.models import Q

from audit.models import AuditRecord, DataChange, SourceEvidence, SyncRun
from audit.services import (
    DataChangeWriteResult,
    record_data_change,
    record_system_action,
    record_user_action,
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


@dataclass(frozen=True, slots=True)
class MembershipWriteResult:
    membership: IndexMembership
    created: bool
    data_changes: tuple[DataChangeWriteResult, ...]
    audit_record: AuditRecord | None


@dataclass(frozen=True, slots=True)
class MembershipCloseResult:
    membership: IndexMembership
    action: str  # "cancelled", "ended", "corrected", "skipped"
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
) -> MembershipWriteResult:
    _validate_membership_status(status)
    _validate_membership_dates(effective_from, effective_to)
    _validate_membership_source(source_evidence, sync_run, actor_user, reason, request_id)

    with transaction.atomic():
        try:
            with transaction.atomic():
                membership = IndexMembership.objects.create(
                    index=index,
                    security_listing=security_listing,
                    status=status,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    announcement_date=announcement_date,
                    source_evidence=source_evidence,
                )
        except IntegrityError:
            # Savepoint rolled back; try to find existing
            existing = IndexMembership.objects.filter(
                index=index,
                security_listing=security_listing,
                effective_from=effective_from,
                status__in=NORMATIVE_MEMBERSHIP_STATUSES,
            ).first()

            if existing is not None:
                _verify_membership_equivalent(
                    existing,
                    status,
                    effective_to,
                    announcement_date,
                    source_evidence,
                )
                return MembershipWriteResult(
                    membership=existing,
                    created=False,
                    data_changes=(),
                    audit_record=None,
                )

            # Check for overlap conflict
            overlapping = (
                IndexMembership.objects.filter(
                    security_listing=security_listing,
                    index=index,
                    status__in=NORMATIVE_MEMBERSHIP_STATUSES,
                    effective_from__lt=(effective_to or date(9999, 12, 31)),
                )
                .filter(
                    Q(effective_to__isnull=True) | Q(effective_to__gt=effective_from),
                )
                .first()
            )

            if overlapping is not None:
                raise MembershipOverlapConflict(
                    f"New membership [{effective_from}, {effective_to}) "
                    f"overlaps existing membership {overlapping.pk} "
                    f"[{overlapping.effective_from}, {overlapping.effective_to})."
                ) from None

            raise MembershipIntegrityConflict(
                "Membership creation failed with an unexpected IntegrityError."
            ) from None

        # Created successfully
        context = _resolve_membership_write_context(
            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
            target_id=membership.pk,
            source_evidence=source_evidence,
            sync_run=sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        audit_result = _write_membership_audit(
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
            audit_record=audit_result,
        )


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
    if effective_to <= membership.effective_from:
        raise MembershipServiceError("effective_to must be later than effective_from.")

    with transaction.atomic():
        obj = IndexMembership.objects.select_for_update().get(pk=membership.pk)

        if obj.status not in ("announced", "active"):
            raise InvalidMembershipState(f"Membership {obj.pk} is {obj.status}, cannot be ended.")

        if obj.effective_to == effective_to:
            return MembershipEndResult(
                membership=obj,
                data_changes=(),
                audit_record=None,
            )

        old_effective_to = obj.effective_to
        old_status = obj.status
        new_status = derive_status(obj.effective_from, effective_to, effective_to)

        obj.effective_to = effective_to
        obj.status = new_status
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
                source_evidence=source_evidence,
                sync_run=sync_run,
                actor_user=actor_user,
                reason=reason,
                origin_key=request_id,
            )
            data_changes.append(dc)

        if old_status != new_status:
            dc = record_data_change(
                target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
                target_id=obj.pk,
                field_name="status",
                old_value=old_status,
                new_value=new_status,
                rule_version=MEMBERSHIP_RULE_VERSION,
                source_evidence=source_evidence,
                sync_run=sync_run,
                actor_user=actor_user,
                reason=reason,
                origin_key=request_id,
            )
            data_changes.append(dc)

        context = _resolve_membership_write_context(
            target_type=DataChange.TargetType.INDEX_MEMBERSHIP,
            target_id=obj.pk,
            source_evidence=source_evidence,
            sync_run=sync_run,
            actor_user=actor_user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        audit = _write_membership_audit(
            membership=obj,
            action=AuditRecord.Action.UPDATE,
            before={
                "effective_to": old_effective_to.isoformat() if old_effective_to else None,
                "status": old_status,
            },
            after={"effective_to": effective_to.isoformat(), "status": new_status},
            context=context,
        )

        return MembershipEndResult(
            membership=obj,
            data_changes=tuple(data_changes),
            audit_record=audit,
        )


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


def _cast_mapping_value(mapping: Mapping[str, object], key: str, default: object) -> object:
    return mapping.get(key, default)


def correct_membership(
    *,
    membership: IndexMembership,
    replacement_values: Mapping[str, object],
    actor_user: User,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
) -> MembershipCorrectionResult:
    with transaction.atomic():
        old = IndexMembership.objects.select_for_update().get(pk=membership.pk)

        if IndexMembership.objects.filter(supersedes=old).exists():
            raise AlreadyCorrected(f"Membership {old.pk} already has a replacement.")

        if old.status not in NORMATIVE_MEMBERSHIP_STATUSES:
            raise InvalidMembershipState(
                f"Membership {old.pk} is {old.status}, cannot be corrected."
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
        memberships = list(
            IndexMembership.objects.select_for_update()
            .filter(security_listing=listing, status__in=NORMATIVE_MEMBERSHIP_STATUSES)
            .order_by("effective_from")
        )

        for obj in memberships:
            if obj.effective_from >= new_effective_to:
                if obj.effective_from > as_of_date:
                    # Future announced, never took effect
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
                    raise MembershipServiceError(
                        f"Membership {obj.pk} effective_from={obj.effective_from} "
                        f">= new_effective_to={new_effective_to} but is not in the future "
                        f"(as_of_date={as_of_date}). Manual review required."
                    )

            elif obj.status == "ended":
                if obj.effective_to is None or obj.effective_to > new_effective_to:
                    # Old effective_to exceeds new boundary → correct
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
                            old_value=old_effective_to.isoformat() if old_effective_to else None,
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
                        "effective_to": old_effective_to.isoformat() if old_effective_to else None,
                        "status": old_status,
                    },
                    after={"effective_to": new_effective_to.isoformat(), "status": new_status},
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


# --- Internal helpers ---


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
    if not has_auto and not has_human:
        raise MembershipServiceError("Must provide either source_evidence/sync_run or actor_user.")
    if has_human and (not reason or not request_id):
        raise MembershipServiceError("Manual operations require non-empty reason and request_id.")
    if has_auto and source_evidence is None and sync_run is None:
        raise MembershipServiceError(
            "Automatic operations require at least one of source_evidence or sync_run."
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


class _MembershipWriteContext:
    def __init__(
        self,
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


def _resolve_membership_write_context(
    *,
    target_type: str,
    target_id: object,
    source_evidence: SourceEvidence | None,
    sync_run: SyncRun | None,
    actor_user: User | None,
    reason: str,
    request_id: str,
    ip_address: str | None,
) -> _MembershipWriteContext:
    del target_type, target_id
    is_auto = actor_user is None
    return _MembershipWriteContext(
        is_auto=is_auto,
        actor_user=actor_user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
        source_evidence=source_evidence,
        sync_run=sync_run,
    )


def _write_membership_audit(
    *,
    membership: IndexMembership,
    action: str,
    before: object,
    after: object,
    context: _MembershipWriteContext,
) -> AuditRecord:
    if context.is_auto:
        assert context.sync_run is not None, "Automatic audit requires a SyncRun."
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
            actor_user=context.actor_user,  # type: ignore[arg-type]
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
        "effective_to": membership.effective_to.isoformat() if membership.effective_to else None,
        "announcement_date": membership.announcement_date.isoformat()
        if membership.announcement_date
        else None,
    }
