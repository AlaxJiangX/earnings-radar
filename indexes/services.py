from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction

from audit.models import AuditRecord, DataChange
from audit.services import (
    DataChangeWriteResult,
    record_data_change,
    record_user_action,
)
from indexes.models import MarketIndex

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
