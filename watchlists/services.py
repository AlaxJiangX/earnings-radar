from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, cast

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from audit.models import AuditRecord
from audit.services import record_user_action
from companies.models import Company, SecurityListing
from companies.services import (
    CompanyMonitoringStatusResult,
    recalculate_company_monitoring_status,
)
from indexes.selectors import company_indexes_as_of
from watchlists.models import WatchlistItem

if TYPE_CHECKING:
    from accounts.models import User


class WatchlistServiceError(ValueError):
    pass


class WatchlistItemNotFound(WatchlistServiceError):
    pass


class WatchlistCompanyNotEligible(WatchlistServiceError):
    pass


@dataclass(frozen=True, slots=True)
class WatchlistMutationResult:
    item: WatchlistItem
    created: bool
    changed: bool
    audit_record: AuditRecord | None
    monitoring_result: CompanyMonitoringStatusResult | None


def add_watchlist_item(
    *,
    user: User,
    company_id: uuid.UUID,
    as_of: date,
    identity_pending: bool,
    reason: str,
    request_id: str,
    priority_level: str = WatchlistItem.PriorityLevel.NORMAL,
    ip_address: str | None = None,
) -> WatchlistMutationResult:
    _validate_user(user)
    normalized_company_id = _require_uuid(company_id, value_name="company_id")
    normalized_as_of = _require_date(as_of)
    normalized_identity_pending = _require_bool(
        identity_pending,
        value_name="identity_pending",
    )
    normalized_priority = _normalize_priority(priority_level)

    with transaction.atomic():
        company = _lock_company_for_watchlist_mutation(normalized_company_id)
        existing = (
            WatchlistItem.objects.select_for_update()
            .filter(user=user, company_id=normalized_company_id)
            .first()
        )
        if existing is not None and existing.is_active:
            return WatchlistMutationResult(existing, False, False, None, None)

        _require_effective_listing(company, as_of=normalized_as_of)
        created = False
        reactivated = existing is not None
        before: dict[str, object]
        item: WatchlistItem
        if existing is None:
            before = {}
            try:
                with transaction.atomic():
                    item = WatchlistItem.objects.create(
                        user=user,
                        company=company,
                        priority_level=normalized_priority,
                    )
                created = True
            except IntegrityError as error:
                raced_item = (
                    WatchlistItem.objects.select_for_update()
                    .filter(user=user, company_id=normalized_company_id)
                    .first()
                )
                if raced_item is None:
                    raise error from None
                item = raced_item
                if item.is_active:
                    return WatchlistMutationResult(item, False, False, None, None)
                reactivated = True
                before = _snapshot(item)
        else:
            item = existing
            before = _snapshot(item)

        if reactivated:
            item.is_active = True
            item.deactivated_at = None
            item.save(update_fields=("is_active", "deactivated_at", "updated_at"))

        after = _snapshot(item)
        audit_record = _record_watchlist_action(
            user=user,
            action=(AuditRecord.Action.CREATE if created else AuditRecord.Action.UPDATE),
            item=item,
            before=before,
            after=after,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        monitoring_result = _recompute_company(
            company_id=item.company_id,
            as_of=normalized_as_of,
            identity_pending=normalized_identity_pending,
            user=user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        return WatchlistMutationResult(item, created, True, audit_record, monitoring_result)


def remove_watchlist_item(
    *,
    user: User,
    item_id: uuid.UUID,
    as_of: date,
    identity_pending: bool,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
) -> WatchlistMutationResult:
    _validate_user(user)
    normalized_item_id = _require_uuid(item_id, value_name="item_id")
    normalized_as_of = _require_date(as_of)
    normalized_identity_pending = _require_bool(
        identity_pending,
        value_name="identity_pending",
    )
    with transaction.atomic():
        company_id = _company_id_for_item(normalized_item_id)
        company = _lock_company_for_watchlist_mutation(company_id)
        item = _locked_user_item(user=user, item_id=normalized_item_id)
        if item.company_id != company.pk:
            raise WatchlistItemNotFound("Watchlist item was not found.")
        if not item.is_active:
            return WatchlistMutationResult(item, False, False, None, None)
        before = _snapshot(item)
        item.is_active = False
        item.deactivated_at = timezone.now()
        item.save(update_fields=("is_active", "deactivated_at", "updated_at"))
        after = _snapshot(item)
        audit_record = _record_watchlist_action(
            user=user,
            action=AuditRecord.Action.DEACTIVATE,
            item=item,
            before=before,
            after=after,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        monitoring_result = _recompute_company(
            company_id=item.company_id,
            as_of=normalized_as_of,
            identity_pending=normalized_identity_pending,
            user=user,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        return WatchlistMutationResult(item, False, True, audit_record, monitoring_result)


def set_watchlist_priority(
    *,
    user: User,
    item_id: uuid.UUID,
    priority_level: str,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
) -> WatchlistMutationResult:
    normalized_priority = _normalize_priority(priority_level)
    return _set_watchlist_attribute(
        user=user,
        item_id=item_id,
        field_name="priority_level",
        value=normalized_priority,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
    )


def set_watchlist_alerts(
    *,
    user: User,
    item_id: uuid.UUID,
    alerts_enabled: bool,
    reason: str,
    request_id: str,
    ip_address: str | None = None,
) -> WatchlistMutationResult:
    normalized_alerts = _require_bool(alerts_enabled, value_name="alerts_enabled")
    return _set_watchlist_attribute(
        user=user,
        item_id=item_id,
        field_name="alerts_enabled",
        value=normalized_alerts,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
    )


def _set_watchlist_attribute(
    *,
    user: User,
    item_id: uuid.UUID,
    field_name: str,
    value: object,
    reason: str,
    request_id: str,
    ip_address: str | None,
) -> WatchlistMutationResult:
    _validate_user(user)
    normalized_item_id = _require_uuid(item_id, value_name="item_id")
    with transaction.atomic():
        item = _locked_user_item(user=user, item_id=normalized_item_id)
        if getattr(item, field_name) == value:
            return WatchlistMutationResult(item, False, False, None, None)
        before = _snapshot(item)
        setattr(item, field_name, value)
        item.save(update_fields=(field_name, "updated_at"))
        after = _snapshot(item)
        audit_record = _record_watchlist_action(
            user=user,
            action=AuditRecord.Action.UPDATE,
            item=item,
            before=before,
            after=after,
            reason=reason,
            request_id=request_id,
            ip_address=ip_address,
        )
        return WatchlistMutationResult(item, False, True, audit_record, None)


def _lock_company_for_watchlist_mutation(company_id: uuid.UUID) -> Company:
    try:
        return Company.objects.select_for_update().get(pk=company_id)
    except Company.DoesNotExist as error:
        raise WatchlistCompanyNotEligible("Company does not exist.") from error


def _company_id_for_item(item_id: uuid.UUID) -> uuid.UUID:
    company_id = (
        WatchlistItem.objects.filter(pk=item_id).values_list("company_id", flat=True).first()
    )
    if company_id is None:
        raise WatchlistItemNotFound("Watchlist item was not found.")
    return cast(uuid.UUID, company_id)


def _require_effective_listing(company: Company, *, as_of: date) -> None:
    has_listing = (
        SecurityListing.objects.filter(company=company, effective_from__lte=as_of)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=as_of))
        .exists()
    )
    if not has_listing:
        raise WatchlistCompanyNotEligible(
            "Company must have an effective SecurityListing on the requested as_of date."
        )


def _locked_user_item(*, user: User, item_id: uuid.UUID) -> WatchlistItem:
    try:
        return cast(
            WatchlistItem,
            WatchlistItem.objects.select_for_update().get(pk=item_id, user=user),
        )
    except WatchlistItem.DoesNotExist as error:
        raise WatchlistItemNotFound("Watchlist item was not found.") from error


def _recompute_company(
    *,
    company_id: uuid.UUID,
    as_of: date,
    identity_pending: bool,
    user: User,
    reason: str,
    request_id: str,
    ip_address: str | None,
) -> CompanyMonitoringStatusResult:
    has_active_watchlist = WatchlistItem.objects.filter(
        company_id=company_id,
        is_active=True,
    ).exists()
    has_enabled_index_membership = company_indexes_as_of(
        company_id=company_id,
        as_of_date=as_of,
        is_enabled=True,
    ).exists()
    return recalculate_company_monitoring_status(
        company_id=company_id,
        as_of=as_of,
        has_enabled_index_membership=has_enabled_index_membership,
        has_active_watchlist=has_active_watchlist,
        identity_pending=identity_pending,
        actor_user=user,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
    )


def _record_watchlist_action(
    *,
    user: User,
    action: str,
    item: WatchlistItem,
    before: dict[str, object],
    after: dict[str, object],
    reason: str,
    request_id: str,
    ip_address: str | None,
) -> AuditRecord:
    return record_user_action(
        actor_user=user,
        action=action,
        target_type=AuditRecord.TargetType.WATCHLIST_ITEM,
        target_id=item.pk,
        before=before,
        after=after,
        reason=reason,
        request_id=request_id,
        ip_address=ip_address,
    ).record


def _snapshot(item: WatchlistItem) -> dict[str, object]:
    return {
        "company_id": str(item.company_id),
        "priority_level": item.priority_level,
        "alerts_enabled": item.alerts_enabled,
        "is_active": item.is_active,
        "deactivated_at": _iso_datetime_or_none(item.deactivated_at),
    }


def _iso_datetime_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _validate_user(user: User) -> None:
    if user._state.adding or user.pk is None:
        raise WatchlistServiceError("user must be saved before use.")


def _require_uuid(value: uuid.UUID, *, value_name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise WatchlistServiceError(f"{value_name} must be a UUID.")
    return value


def _require_date(value: date) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise WatchlistServiceError("as_of must be a date.")
    return value


def _require_bool(value: bool, *, value_name: str) -> bool:
    if not isinstance(value, bool):
        raise WatchlistServiceError(f"{value_name} must be a boolean.")
    return value


def _normalize_priority(value: str) -> str:
    if not isinstance(value, str):
        raise WatchlistServiceError("priority_level must be text.")
    normalized = value.strip().lower()
    allowed = {choice for choice, _ in WatchlistItem.PriorityLevel.choices}
    if normalized not in allowed:
        raise WatchlistServiceError("priority_level must be normal or important.")
    return normalized
