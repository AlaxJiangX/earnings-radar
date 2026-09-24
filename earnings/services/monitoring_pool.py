"""Deterministic monitoring-pool selection and immutable snapshot persistence."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Q

from earnings.models import MonitoringPoolMember, MonitoringPoolSnapshot
from indexes.models import (
    ALLOWED_CODES,
    NORMATIVE_MEMBERSHIP_STATUSES,
    IndexMembership,
    MarketIndex,
)

EARNINGS_MONITORING_POOL_SELECTOR_VERSION = "earnings-monitoring-pool-v1"
MONITORING_POOL_HASH_CONTRACT_VERSION = "earnings-monitoring-pool-hash-v1"


class MonitoringPoolSelectorError(ValueError):
    """Base class for invalid selector input."""


class InvalidMonitoringPoolSelectorInput(MonitoringPoolSelectorError):
    """Raised when selector input cannot be canonicalized."""


class UnknownMonitoringPoolSelectorVersion(MonitoringPoolSelectorError):
    """Raised when the selector algorithm version is not supported."""


class MonitoringPoolIntegrityError(RuntimeError):
    """Raised when persisted selector data or source temporal facts are inconsistent."""


@dataclass(frozen=True, slots=True)
class MonitoringPoolSelectionResult:
    snapshot: MonitoringPoolSnapshot
    members: tuple[MonitoringPoolMember, ...]
    created: bool

    @property
    def as_of_date(self) -> date:
        return self.snapshot.as_of_date

    @property
    def selector_version(self) -> str:
        return self.snapshot.selector_version

    @property
    def enabled_index_codes(self) -> tuple[str, ...]:
        return tuple(self.snapshot.enabled_index_codes)

    @property
    def input_revision(self) -> str:
        return self.snapshot.input_revision

    @property
    def monitoring_pool_hash(self) -> str:
        return self.snapshot.pool_hash

    @property
    def member_count(self) -> int:
        return self.snapshot.member_count


def select_monitoring_pool(
    *,
    as_of: date,
    selector_version: str,
    enabled_index_codes: Iterable[str],
) -> MonitoringPoolSelectionResult:
    """Select the canonical Company monitoring pool and persist its snapshot."""

    normalized_as_of = _validate_as_of(as_of)
    normalized_selector_version = _validate_selector_version(selector_version)
    normalized_codes = _normalize_enabled_index_codes(enabled_index_codes)
    _validate_enabled_indexes_exist(normalized_codes)

    membership_rows = list(
        IndexMembership.objects.filter(
            index__code__in=normalized_codes,
            status__in=NORMATIVE_MEMBERSHIP_STATUSES,
            effective_from__lte=normalized_as_of,
        )
        .filter(
            Q(effective_to__isnull=True) | Q(effective_to__gt=normalized_as_of),
            security_listing__effective_from__lte=normalized_as_of,
        )
        .filter(
            Q(security_listing__effective_to__isnull=True)
            | Q(security_listing__effective_to__gt=normalized_as_of),
        )
        .select_related("index", "security_listing")
    )
    _validate_membership_rows(membership_rows)

    manifest_rows = _build_input_manifest_rows(membership_rows)
    input_revision = _sha256_json(
        {
            "enabled_index_codes": list(normalized_codes),
            "memberships": manifest_rows,
        }
    )
    members_payload = _build_members_payload(membership_rows)
    pool_hash = _build_pool_hash(
        as_of=normalized_as_of,
        selector_version=normalized_selector_version,
        enabled_index_codes=normalized_codes,
        input_revision=input_revision,
        members=members_payload,
    )

    return _persist_selection(
        as_of=normalized_as_of,
        selector_version=normalized_selector_version,
        enabled_index_codes=normalized_codes,
        input_revision=input_revision,
        pool_hash=pool_hash,
        members_payload=members_payload,
    )


def _validate_as_of(value: object) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise InvalidMonitoringPoolSelectorInput("as_of must be a date.")
    return value


def _validate_selector_version(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidMonitoringPoolSelectorInput("selector_version must be a string.")
    normalized = value.strip()
    if normalized != EARNINGS_MONITORING_POOL_SELECTOR_VERSION:
        raise UnknownMonitoringPoolSelectorVersion(
            f"Unsupported monitoring-pool selector version: {value!r}."
        )
    return normalized


def _normalize_enabled_index_codes(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InvalidMonitoringPoolSelectorInput(
            "enabled_index_codes must be a non-empty iterable of index codes."
        )
    normalized: set[str] = set()
    for code in value:
        if not isinstance(code, str):
            raise InvalidMonitoringPoolSelectorInput("enabled_index_codes entries must be strings.")
        normalized_code = code.strip().upper()
        if not normalized_code:
            raise InvalidMonitoringPoolSelectorInput(
                "enabled_index_codes entries must not be blank."
            )
        if normalized_code not in ALLOWED_CODES:
            raise InvalidMonitoringPoolSelectorInput(f"Unknown enabled index code: {code!r}.")
        normalized.add(normalized_code)
    if not normalized:
        raise InvalidMonitoringPoolSelectorInput(
            "enabled_index_codes must contain at least one index code."
        )
    return tuple(sorted(normalized))


def _validate_enabled_indexes_exist(codes: tuple[str, ...]) -> None:
    existing = set(MarketIndex.objects.filter(code__in=codes).values_list("code", flat=True))
    missing = sorted(set(codes) - existing)
    if missing:
        raise InvalidMonitoringPoolSelectorInput(
            f"Enabled index codes do not exist: {', '.join(missing)}."
        )


def _validate_membership_rows(rows: list[IndexMembership]) -> None:
    for membership in rows:
        listing = membership.security_listing
        if membership.effective_from < listing.effective_from:
            raise MonitoringPoolIntegrityError("IndexMembership starts before its SecurityListing.")
        if listing.effective_to is not None and (
            membership.effective_to is None or membership.effective_to > listing.effective_to
        ):
            raise MonitoringPoolIntegrityError(
                "IndexMembership extends beyond its SecurityListing."
            )


def _build_input_manifest_rows(rows: list[IndexMembership]) -> list[dict[str, object]]:
    manifest_rows: list[dict[str, object]] = [
        {
            "company_id": str(membership.security_listing.company_id),
            "index_code": membership.index.code,
            "security_listing_id": str(membership.security_listing_id),
            "membership_effective_from": membership.effective_from.isoformat(),
            "membership_effective_to": _iso_or_none(membership.effective_to),
            "listing_effective_from": membership.security_listing.effective_from.isoformat(),
            "listing_effective_to": _iso_or_none(membership.security_listing.effective_to),
        }
        for membership in rows
    ]
    manifest_rows.sort(key=_manifest_row_sort_key)
    return _dedupe_dicts(manifest_rows)


def _build_members_payload(rows: list[IndexMembership]) -> list[dict[str, object]]:
    grouped_basis: dict[UUID, set[tuple[str, str, str, str | None]]] = defaultdict(set)
    for membership in rows:
        grouped_basis[membership.security_listing.company_id].add(
            (
                membership.index.code,
                str(membership.security_listing_id),
                membership.effective_from.isoformat(),
                _iso_or_none(membership.effective_to),
            )
        )

    members: list[dict[str, object]] = []
    for company_id in sorted(grouped_basis, key=str):
        basis = [
            {
                "index_code": index_code,
                "security_listing_id": security_listing_id,
                "effective_from": effective_from,
                "effective_to": effective_to,
            }
            for index_code, security_listing_id, effective_from, effective_to in sorted(
                grouped_basis[company_id]
            )
        ]
        members.append({"company_id": str(company_id), "basis": basis})
    return members


def _build_pool_hash(
    *,
    as_of: date,
    selector_version: str,
    enabled_index_codes: tuple[str, ...],
    input_revision: str,
    members: list[dict[str, object]],
) -> str:
    return _sha256_json(
        {
            "contract": MONITORING_POOL_HASH_CONTRACT_VERSION,
            "selector_version": selector_version,
            "as_of": as_of.isoformat(),
            "enabled_index_codes": list(enabled_index_codes),
            "input_revision": input_revision,
            "members": members,
        }
    )


def _persist_selection(
    *,
    as_of: date,
    selector_version: str,
    enabled_index_codes: tuple[str, ...],
    input_revision: str,
    pool_hash: str,
    members_payload: list[dict[str, object]],
) -> MonitoringPoolSelectionResult:
    with transaction.atomic():
        existing = _find_snapshot_by_identity(
            as_of=as_of,
            selector_version=selector_version,
            input_revision=input_revision,
            pool_hash=pool_hash,
        )
        if existing is not None:
            return _load_validated_snapshot(
                existing,
                enabled_index_codes=enabled_index_codes,
                input_revision=input_revision,
                members_payload=members_payload,
            )

        try:
            with transaction.atomic():
                snapshot = MonitoringPoolSnapshot.objects.create(
                    as_of_date=as_of,
                    selector_version=selector_version,
                    enabled_index_codes=list(enabled_index_codes),
                    input_revision=input_revision,
                    pool_hash=pool_hash,
                    member_count=len(members_payload),
                )
                created_members = [
                    MonitoringPoolMember(
                        snapshot=snapshot,
                        company_id=UUID(str(member["company_id"])),
                        ordinal=ordinal,
                        basis=member["basis"],
                    )
                    for ordinal, member in enumerate(members_payload)
                ]
                if created_members:
                    MonitoringPoolMember.objects.bulk_create(created_members)
        except IntegrityError as error:
            existing = _find_snapshot_by_identity(
                as_of=as_of,
                selector_version=selector_version,
                input_revision=input_revision,
                pool_hash=pool_hash,
            )
            if existing is None:
                raise MonitoringPoolIntegrityError(
                    "Monitoring pool snapshot creation violated an unexpected constraint."
                ) from error
            return _load_validated_snapshot(
                existing,
                enabled_index_codes=enabled_index_codes,
                input_revision=input_revision,
                members_payload=members_payload,
            )

        members = _load_members(snapshot)
        return MonitoringPoolSelectionResult(snapshot=snapshot, members=members, created=True)


def _find_snapshot_by_identity(
    *,
    as_of: date,
    selector_version: str,
    input_revision: str,
    pool_hash: str,
) -> MonitoringPoolSnapshot | None:
    by_hash = MonitoringPoolSnapshot.objects.filter(
        as_of_date=as_of,
        selector_version=selector_version,
        pool_hash=pool_hash,
    ).first()
    by_revision = MonitoringPoolSnapshot.objects.filter(
        as_of_date=as_of,
        selector_version=selector_version,
        input_revision=input_revision,
    ).first()
    if by_hash is not None and by_revision is not None and by_hash.pk != by_revision.pk:
        raise MonitoringPoolIntegrityError(
            "Monitoring pool hash and input revision resolve to different snapshots."
        )
    return by_hash or by_revision


def _load_validated_snapshot(
    snapshot: MonitoringPoolSnapshot,
    *,
    enabled_index_codes: tuple[str, ...],
    input_revision: str,
    members_payload: list[dict[str, object]],
) -> MonitoringPoolSelectionResult:
    members = _load_members(snapshot)
    if snapshot.enabled_index_codes != list(enabled_index_codes):
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot index policy mismatch.")
    if snapshot.input_revision != input_revision:
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot input revision mismatch.")
    if snapshot.member_count != len(members):
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot member count mismatch.")

    persisted_members = _serialize_members(members)
    if persisted_members != members_payload:
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot members are not canonical.")

    recomputed_hash = _build_pool_hash(
        as_of=snapshot.as_of_date,
        selector_version=snapshot.selector_version,
        enabled_index_codes=_snapshot_codes(snapshot),
        input_revision=snapshot.input_revision,
        members=persisted_members,
    )
    if recomputed_hash != snapshot.pool_hash:
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot hash mismatch.")

    return MonitoringPoolSelectionResult(snapshot=snapshot, members=members, created=False)


def _load_members(snapshot: MonitoringPoolSnapshot) -> tuple[MonitoringPoolMember, ...]:
    members = tuple(MonitoringPoolMember.objects.filter(snapshot=snapshot).order_by("ordinal"))
    expected_ordinals = tuple(range(len(members)))
    actual_ordinals = tuple(member.ordinal for member in members)
    if actual_ordinals != expected_ordinals:
        raise MonitoringPoolIntegrityError("Monitoring pool member ordinals are not contiguous.")
    return members


def _serialize_members(members: tuple[MonitoringPoolMember, ...]) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for member in members:
        basis = _canonicalize_basis(member.basis)
        if basis != member.basis:
            raise MonitoringPoolIntegrityError(
                "Monitoring pool member basis is not canonically ordered."
            )
        serialized.append(
            {
                "company_id": str(member.company_id),
                "basis": basis,
            }
        )
    return serialized


def _canonicalize_basis(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise MonitoringPoolIntegrityError("Monitoring pool member basis must be a non-empty list.")
    canonical_rows: list[dict[str, object]] = []
    for row in value:
        if not isinstance(row, dict):
            raise MonitoringPoolIntegrityError("Monitoring pool basis rows must be objects.")
        expected_keys = {
            "index_code",
            "security_listing_id",
            "effective_from",
            "effective_to",
        }
        if set(row) != expected_keys:
            raise MonitoringPoolIntegrityError("Monitoring pool basis row shape mismatch.")
        index_code = row["index_code"]
        security_listing_id = row["security_listing_id"]
        effective_from = row["effective_from"]
        effective_to = row["effective_to"]
        if not isinstance(index_code, str) or index_code not in ALLOWED_CODES:
            raise MonitoringPoolIntegrityError(
                "Monitoring pool basis contains an invalid index code."
            )
        if not isinstance(security_listing_id, str):
            raise MonitoringPoolIntegrityError("Monitoring pool basis listing identity is invalid.")
        if not isinstance(effective_from, str):
            raise MonitoringPoolIntegrityError(
                "Monitoring pool basis contains an invalid temporal identity."
            )
        try:
            UUID(security_listing_id)
            start_date = date.fromisoformat(effective_from)
            end_date = date.fromisoformat(effective_to) if isinstance(effective_to, str) else None
            if effective_to is not None and not isinstance(effective_to, str):
                raise ValueError
            if str(UUID(security_listing_id)) != security_listing_id:
                raise ValueError
            if start_date.isoformat() != effective_from:
                raise ValueError
            if end_date is not None and end_date.isoformat() != effective_to:
                raise ValueError
            if end_date is not None and end_date <= start_date:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise MonitoringPoolIntegrityError(
                "Monitoring pool basis contains an invalid temporal identity."
            ) from error
        canonical_rows.append(
            {
                "index_code": index_code,
                "security_listing_id": security_listing_id,
                "effective_from": effective_from,
                "effective_to": effective_to,
            }
        )
    canonical_rows.sort(
        key=lambda row: (
            str(row["index_code"]),
            str(row["security_listing_id"]),
            str(row["effective_from"]),
            str(row["effective_to"] or ""),
        )
    )
    if len({_json_key(row) for row in canonical_rows}) != len(canonical_rows):
        raise MonitoringPoolIntegrityError("Monitoring pool basis contains duplicate rows.")
    return canonical_rows


def _snapshot_codes(snapshot: MonitoringPoolSnapshot) -> tuple[str, ...]:
    value = snapshot.enabled_index_codes
    if not isinstance(value, list) or not value:
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot index policy is invalid.")
    if any(not isinstance(code, str) or code not in ALLOWED_CODES for code in value):
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot index policy is invalid.")
    if value != sorted(set(value)):
        raise MonitoringPoolIntegrityError(
            "Monitoring pool snapshot index policy is not canonical."
        )
    return tuple(value)


def _sha256_json(value: object) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


def _iso_or_none(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _manifest_row_sort_key(row: dict[str, object]) -> tuple[str, str, str, str, str, str, str]:
    return (
        str(row["company_id"]),
        str(row["index_code"]),
        str(row["security_listing_id"]),
        str(row["membership_effective_from"]),
        str(row["membership_effective_to"] or ""),
        str(row["listing_effective_from"]),
        str(row["listing_effective_to"] or ""),
    )


def _dedupe_dicts(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for row in rows:
        key = _json_key(row)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def _json_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
