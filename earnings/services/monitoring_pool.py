"""Deterministic monitoring-pool selection and immutable snapshot persistence."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Q

from audit.models import SyncRun
from companies.models import Company, SecurityListing
from earnings.models import MonitoringPoolMember, MonitoringPoolSnapshot
from earnings.services.calendar_sync_identity import (
    EARNINGS_CALENDAR_SCOPE_FIELDS,
    EarningsCalendarWindowKind,
    InvalidEarningsCalendarSyncIdentity,
    build_earnings_calendar_sync_scope,
)
from indexes.models import (
    ALLOWED_CODES,
    NORMATIVE_MEMBERSHIP_STATUSES,
    IndexMembership,
    MarketIndex,
)

EARNINGS_MONITORING_POOL_SELECTOR_VERSION = "earnings-monitoring-pool-v1"
EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2 = "earnings-monitoring-pool-v2"
MONITORING_POOL_HASH_CONTRACT_VERSION = "earnings-monitoring-pool-hash-v1"
MONITORING_POOL_HASH_CONTRACT_VERSION_V2 = "earnings-monitoring-pool-hash-v2"
MONITORING_POOL_INPUT_CONTRACT_VERSION_V2 = "earnings-monitoring-pool-input-v2"


class MonitoringPoolSelectorError(ValueError):
    """Base class for invalid selector input."""


class InvalidMonitoringPoolSelectorInput(MonitoringPoolSelectorError):
    """Raised when selector input cannot be canonicalized."""


class UnknownMonitoringPoolSelectorVersion(MonitoringPoolSelectorError):
    """Raised when the selector algorithm version is not supported."""


class MonitoringPoolIntegrityError(RuntimeError):
    """Raised when persisted selector data or source temporal facts are inconsistent."""


@dataclass(frozen=True, slots=True)
class MonitoringPoolSnapshotReference:
    snapshot: MonitoringPoolSnapshot
    members: tuple[MonitoringPoolMember, ...]


@dataclass(frozen=True, slots=True)
class MonitoringPoolSelectionResult:
    snapshot: MonitoringPoolSnapshot
    members: tuple[MonitoringPoolMember, ...]
    created: bool
    watchlist_company_ids: tuple[UUID, ...] = ()

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


def resolve_monitoring_pool_snapshot(sync_run: SyncRun) -> MonitoringPoolSnapshotReference:
    """Resolve and validate the frozen snapshot referenced by a persisted SyncRun."""

    if not isinstance(sync_run, SyncRun) or sync_run._state.adding or sync_run.pk is None:
        raise InvalidMonitoringPoolSelectorInput("sync_run must be a persisted SyncRun.")
    current_run = SyncRun.objects.get(pk=sync_run.pk)
    scope = _validate_scope(current_run.scope)
    return resolve_monitoring_pool_snapshot_contract(
        as_of=date.fromisoformat(cast(str, scope["monitoring_pool_as_of"])),
        selector_version=cast(str, scope["selector_version"]),
        pool_hash=cast(str, scope["monitoring_pool_hash"]),
    )


def resolve_monitoring_pool_snapshot_contract(
    *, as_of: date, selector_version: str, pool_hash: str
) -> MonitoringPoolSnapshotReference:
    """Validate a persisted frozen pool by its immutable scope contract."""

    if isinstance(as_of, datetime) or not isinstance(as_of, date):
        raise MonitoringPoolIntegrityError("Monitoring pool as-of date is invalid.")
    if not isinstance(selector_version, str) or not selector_version:
        raise MonitoringPoolIntegrityError("Monitoring pool selector version is invalid.")
    if selector_version not in {
        EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2,
    }:
        raise MonitoringPoolIntegrityError("Monitoring pool selector version is unsupported.")
    if not isinstance(pool_hash, str) or len(pool_hash) != 64:
        raise MonitoringPoolIntegrityError("Monitoring pool hash is invalid.")
    snapshot_matches = list(
        MonitoringPoolSnapshot.objects.filter(
            as_of_date=as_of,
            selector_version=selector_version,
            pool_hash=pool_hash,
        )
    )
    if len(snapshot_matches) != 1:
        raise MonitoringPoolIntegrityError(
            "SyncRun monitoring-pool contract does not resolve to exactly one snapshot."
        )
    snapshot = snapshot_matches[0]
    members = _load_members(snapshot)
    validated = _load_validated_snapshot(
        snapshot,
        enabled_index_codes=_snapshot_codes(snapshot),
        input_revision=snapshot.input_revision,
        members_payload=_serialize_members(
            members,
            selector_version=snapshot.selector_version,
        ),
    )
    return MonitoringPoolSnapshotReference(
        snapshot=validated.snapshot,
        members=validated.members,
    )


def _validate_scope(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(EARNINGS_CALENDAR_SCOPE_FIELDS):
        raise MonitoringPoolIntegrityError("SyncRun scope is not canonical.")
    try:
        canonical = build_earnings_calendar_sync_scope(
            provider_key=cast(str, value["provider_key"]),
            window_kind=EarningsCalendarWindowKind(cast(str, value["window_kind"])),
            window_start=date.fromisoformat(cast(str, value["window_start"])),
            window_end=date.fromisoformat(cast(str, value["window_end"])),
            monitoring_pool_as_of=date.fromisoformat(cast(str, value["monitoring_pool_as_of"])),
            monitoring_pool_hash=cast(str, value["monitoring_pool_hash"]),
            selector_version=cast(str, value["selector_version"]),
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        InvalidEarningsCalendarSyncIdentity,
    ) as error:
        raise MonitoringPoolIntegrityError("SyncRun scope is invalid.") from error
    if canonical != value:
        raise MonitoringPoolIntegrityError("SyncRun scope is not canonical.")
    return canonical


def select_monitoring_pool(
    *,
    as_of: date,
    selector_version: str,
    enabled_index_codes: Iterable[str],
    watchlist_company_ids: Iterable[UUID] | None = None,
) -> MonitoringPoolSelectionResult:
    """Select the canonical Company monitoring pool and persist its snapshot."""

    normalized_as_of = _validate_as_of(as_of)
    normalized_selector_version = _validate_selector_version(selector_version)
    normalized_codes = _normalize_enabled_index_codes(enabled_index_codes)
    normalized_watchlist_company_ids = _normalize_watchlist_company_ids(
        watchlist_company_ids,
        selector_version=normalized_selector_version,
    )
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

    if normalized_selector_version == EARNINGS_MONITORING_POOL_SELECTOR_VERSION:
        manifest_rows = _build_input_manifest_rows(membership_rows)
        input_revision = _sha256_json(
            {
                "enabled_index_codes": list(normalized_codes),
                "memberships": manifest_rows,
            }
        )
        members_payload = _build_members_payload(membership_rows)
    else:
        watchlist_listings = _watchlist_listings_as_of(
            company_ids=normalized_watchlist_company_ids,
            as_of=normalized_as_of,
        )
        index_manifest_rows = _build_input_manifest_rows(membership_rows)
        watchlist_manifest_rows = _build_watchlist_manifest_rows(watchlist_listings)
        input_revision = _sha256_json(
            {
                "contract": MONITORING_POOL_INPUT_CONTRACT_VERSION_V2,
                "enabled_index_codes": list(normalized_codes),
                "index_memberships": index_manifest_rows,
                "watchlist_company_ids": [
                    str(company_id) for company_id in normalized_watchlist_company_ids
                ],
                "watchlist_listings": watchlist_manifest_rows,
            }
        )
        members_payload = _build_members_payload_v2(
            membership_rows=membership_rows,
            watchlist_company_ids=normalized_watchlist_company_ids,
            watchlist_listings=watchlist_listings,
        )
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
        watchlist_company_ids=normalized_watchlist_company_ids,
    )


def _validate_as_of(value: object) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise InvalidMonitoringPoolSelectorInput("as_of must be a date.")
    return value


def _validate_selector_version(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidMonitoringPoolSelectorInput("selector_version must be a string.")
    normalized = value.strip()
    if normalized not in {
        EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2,
    }:
        raise UnknownMonitoringPoolSelectorVersion(
            f"Unsupported monitoring-pool selector version: {value!r}."
        )
    return normalized


def _normalize_watchlist_company_ids(
    value: object,
    *,
    selector_version: str,
) -> tuple[UUID, ...]:
    if selector_version == EARNINGS_MONITORING_POOL_SELECTOR_VERSION:
        if value is not None:
            raise InvalidMonitoringPoolSelectorInput(
                "watchlist_company_ids must be None for the v1 selector."
            )
        return ()
    if value is None:
        raise InvalidMonitoringPoolSelectorInput(
            "watchlist_company_ids must be provided for the v2 selector."
        )
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InvalidMonitoringPoolSelectorInput(
            "watchlist_company_ids must be an iterable of UUID values."
        )
    normalized: set[UUID] = set()
    for company_id in value:
        if not isinstance(company_id, UUID):
            raise InvalidMonitoringPoolSelectorInput(
                "watchlist_company_ids entries must be UUID values."
            )
        normalized.add(company_id)
    ordered = tuple(sorted(normalized, key=str))
    existing = set(Company.objects.filter(pk__in=ordered).values_list("pk", flat=True))
    missing = tuple(company_id for company_id in ordered if company_id not in existing)
    if missing:
        raise InvalidMonitoringPoolSelectorInput(
            "watchlist_company_ids contains an unknown Company UUID."
        )
    return ordered


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


def _watchlist_listings_as_of(
    *,
    company_ids: tuple[UUID, ...],
    as_of: date,
) -> list[SecurityListing]:
    if not company_ids:
        return []
    return list(
        SecurityListing.objects.filter(
            company_id__in=company_ids,
            effective_from__lte=as_of,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=as_of))
        .order_by("company_id", "id", "effective_from", "effective_to")
    )


def _build_watchlist_manifest_rows(
    rows: list[SecurityListing],
) -> list[dict[str, object]]:
    manifest_rows: list[dict[str, object]] = [
        {
            "company_id": str(listing.company_id),
            "security_listing_id": str(listing.pk),
            "listing_effective_from": listing.effective_from.isoformat(),
            "listing_effective_to": _iso_or_none(listing.effective_to),
        }
        for listing in rows
    ]
    manifest_rows.sort(
        key=lambda row: (
            str(row["company_id"]),
            str(row["security_listing_id"]),
            str(row["listing_effective_from"]),
            str(row["listing_effective_to"] or ""),
        )
    )
    return _dedupe_dicts(manifest_rows)


def _build_members_payload_v2(
    *,
    membership_rows: list[IndexMembership],
    watchlist_company_ids: tuple[UUID, ...],
    watchlist_listings: list[SecurityListing],
) -> list[dict[str, object]]:
    index_basis: dict[UUID, set[tuple[str, str, str, str | None]]] = defaultdict(set)
    for membership in membership_rows:
        index_basis[membership.security_listing.company_id].add(
            (
                membership.index.code,
                str(membership.security_listing_id),
                membership.effective_from.isoformat(),
                _iso_or_none(membership.effective_to),
            )
        )

    watchlist_basis: dict[UUID, set[tuple[str, str, str | None]]] = defaultdict(set)
    for listing in watchlist_listings:
        watchlist_basis[listing.company_id].add(
            (
                str(listing.pk),
                listing.effective_from.isoformat(),
                _iso_or_none(listing.effective_to),
            )
        )

    member_ids = set(index_basis) | set(watchlist_company_ids)
    members: list[dict[str, object]] = []
    for company_id in sorted(member_ids, key=str):
        basis: list[dict[str, object]] = []
        for index_code, listing_id, effective_from, effective_to in sorted(
            index_basis.get(company_id, set()),
            key=lambda row: (row[0], row[1], row[2], row[3] or ""),
        ):
            basis.append(
                {
                    "source": "index",
                    "index_code": index_code,
                    "security_listing_id": listing_id,
                    "effective_from": effective_from,
                    "effective_to": effective_to,
                }
            )
        if company_id in watchlist_company_ids:
            listings = sorted(
                watchlist_basis.get(company_id, set()),
                key=lambda row: (row[0], row[1], row[2] or ""),
            )
            if listings:
                for listing_id, effective_from, effective_to in listings:
                    basis.append(
                        {
                            "source": "watchlist",
                            "security_listing_id": listing_id,
                            "effective_from": effective_from,
                            "effective_to": effective_to,
                        }
                    )
            else:
                basis.append({"source": "watchlist"})
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
    contract_version = (
        MONITORING_POOL_HASH_CONTRACT_VERSION
        if selector_version == EARNINGS_MONITORING_POOL_SELECTOR_VERSION
        else MONITORING_POOL_HASH_CONTRACT_VERSION_V2
    )
    return _sha256_json(
        {
            "contract": contract_version,
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
    watchlist_company_ids: tuple[UUID, ...],
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
                watchlist_company_ids=watchlist_company_ids,
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
                watchlist_company_ids=watchlist_company_ids,
            )

        members = _load_members(snapshot)
        return MonitoringPoolSelectionResult(
            snapshot=snapshot,
            members=members,
            created=True,
            watchlist_company_ids=watchlist_company_ids,
        )


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
    watchlist_company_ids: tuple[UUID, ...] | None = None,
) -> MonitoringPoolSelectionResult:
    members = _load_members(snapshot)
    if snapshot.enabled_index_codes != list(enabled_index_codes):
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot index policy mismatch.")
    if snapshot.input_revision != input_revision:
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot input revision mismatch.")
    if snapshot.member_count != len(members):
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot member count mismatch.")

    persisted_members = _serialize_members(
        members,
        selector_version=snapshot.selector_version,
    )
    if persisted_members != members_payload:
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot members are not canonical.")

    if snapshot.selector_version == EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2:
        rebuilt_input_revision = _rebuild_v2_input_revision(
            snapshot=snapshot,
            members_payload=persisted_members,
        )
        if rebuilt_input_revision != snapshot.input_revision:
            raise MonitoringPoolIntegrityError("Monitoring pool snapshot input revision mismatch.")

    recomputed_hash = _build_pool_hash(
        as_of=snapshot.as_of_date,
        selector_version=snapshot.selector_version,
        enabled_index_codes=_snapshot_codes(snapshot),
        input_revision=snapshot.input_revision,
        members=persisted_members,
    )
    if recomputed_hash != snapshot.pool_hash:
        raise MonitoringPoolIntegrityError("Monitoring pool snapshot hash mismatch.")

    persisted_watchlist_company_ids = _watchlist_company_ids_from_members_payload(
        persisted_members,
        selector_version=snapshot.selector_version,
    )
    if (
        watchlist_company_ids is not None
        and persisted_watchlist_company_ids != watchlist_company_ids
    ):
        raise MonitoringPoolIntegrityError(
            "Monitoring pool snapshot watchlist Company input mismatch."
        )

    return MonitoringPoolSelectionResult(
        snapshot=snapshot,
        members=members,
        created=False,
        watchlist_company_ids=persisted_watchlist_company_ids,
    )


def _load_members(snapshot: MonitoringPoolSnapshot) -> tuple[MonitoringPoolMember, ...]:
    members = tuple(MonitoringPoolMember.objects.filter(snapshot=snapshot).order_by("ordinal"))
    expected_ordinals = tuple(range(len(members)))
    actual_ordinals = tuple(member.ordinal for member in members)
    if actual_ordinals != expected_ordinals:
        raise MonitoringPoolIntegrityError("Monitoring pool member ordinals are not contiguous.")
    return members


def _serialize_members(
    members: tuple[MonitoringPoolMember, ...],
    *,
    selector_version: str,
) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for member in members:
        basis = _canonicalize_basis(
            member.basis,
            selector_version=selector_version,
        )
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
    if selector_version == EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2:
        company_ids = [cast(str, member["company_id"]) for member in serialized]
        if company_ids != sorted(company_ids):
            raise MonitoringPoolIntegrityError(
                "Monitoring pool members are not canonically ordered by Company UUID."
            )
    return serialized


def _rebuild_v2_input_revision(
    *,
    snapshot: MonitoringPoolSnapshot,
    members_payload: list[dict[str, object]],
) -> str:
    enabled_index_codes = _snapshot_codes(snapshot)
    watchlist_company_ids = _watchlist_company_ids_from_members_payload(
        members_payload,
        selector_version=snapshot.selector_version,
    )
    listing_ids = {
        UUID(cast(str, row["security_listing_id"]))
        for member in members_payload
        for row in cast(list[dict[str, object]], member["basis"])
        if "security_listing_id" in row
    }
    listings = {
        listing.pk: listing for listing in SecurityListing.objects.filter(pk__in=listing_ids)
    }
    if set(listings) != listing_ids:
        raise MonitoringPoolIntegrityError(
            "Monitoring pool basis references an unknown SecurityListing."
        )

    index_manifest_rows: list[dict[str, object]] = []
    watchlist_manifest_rows: list[dict[str, object]] = []
    for member in members_payload:
        company_id = UUID(cast(str, member["company_id"]))
        basis = cast(list[dict[str, object]], member["basis"])
        for row in basis:
            if set(row) == {"source"}:
                continue
            listing_id = UUID(cast(str, row["security_listing_id"]))
            listing = listings[listing_id]
            if listing.company_id != company_id:
                raise MonitoringPoolIntegrityError(
                    "Monitoring pool basis listing belongs to a different Company."
                )
            effective_from = date.fromisoformat(cast(str, row["effective_from"]))
            effective_to_value = cast(str | None, row["effective_to"])
            effective_to = (
                date.fromisoformat(effective_to_value) if effective_to_value is not None else None
            )
            if not _interval_contains(
                start=effective_from,
                end=effective_to,
                value=snapshot.as_of_date,
            ):
                raise MonitoringPoolIntegrityError(
                    "Monitoring pool basis interval does not contain the snapshot date."
                )
            if row["source"] == "index":
                index_code = cast(str, row["index_code"])
                if index_code not in enabled_index_codes:
                    raise MonitoringPoolIntegrityError(
                        "Monitoring pool index basis is outside the enabled index policy."
                    )
                if effective_from < listing.effective_from or (
                    listing.effective_to is not None
                    and (effective_to is None or effective_to > listing.effective_to)
                ):
                    raise MonitoringPoolIntegrityError(
                        "Monitoring pool index basis exceeds its SecurityListing interval."
                    )
                index_manifest_rows.append(
                    {
                        "company_id": str(company_id),
                        "index_code": index_code,
                        "security_listing_id": str(listing_id),
                        "membership_effective_from": effective_from.isoformat(),
                        "membership_effective_to": _iso_or_none(effective_to),
                        "listing_effective_from": listing.effective_from.isoformat(),
                        "listing_effective_to": _iso_or_none(listing.effective_to),
                    }
                )
            else:
                if effective_from != listing.effective_from or effective_to != listing.effective_to:
                    raise MonitoringPoolIntegrityError(
                        "Monitoring pool watchlist basis does not match its "
                        "SecurityListing interval."
                    )
                watchlist_manifest_rows.append(
                    {
                        "company_id": str(company_id),
                        "security_listing_id": str(listing_id),
                        "listing_effective_from": effective_from.isoformat(),
                        "listing_effective_to": _iso_or_none(effective_to),
                    }
                )

    index_manifest_rows.sort(key=_manifest_row_sort_key)
    watchlist_manifest_rows.sort(
        key=lambda row: (
            str(row["company_id"]),
            str(row["security_listing_id"]),
            str(row["listing_effective_from"]),
            str(row["listing_effective_to"] or ""),
        )
    )
    return _sha256_json(
        {
            "contract": MONITORING_POOL_INPUT_CONTRACT_VERSION_V2,
            "enabled_index_codes": list(enabled_index_codes),
            "index_memberships": _dedupe_dicts(index_manifest_rows),
            "watchlist_company_ids": [str(company_id) for company_id in watchlist_company_ids],
            "watchlist_listings": _dedupe_dicts(watchlist_manifest_rows),
        }
    )


def _interval_contains(*, start: date, end: date | None, value: date) -> bool:
    return start <= value and (end is None or value < end)


def _canonicalize_basis(
    value: object,
    *,
    selector_version: str,
) -> list[dict[str, object]]:
    if selector_version == EARNINGS_MONITORING_POOL_SELECTOR_VERSION:
        return _canonicalize_basis_v1(value)
    if selector_version == EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2:
        return _canonicalize_basis_v2(value)
    raise MonitoringPoolIntegrityError("Monitoring pool selector version is unsupported.")


def _canonicalize_basis_v1(value: object) -> list[dict[str, object]]:
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


def _canonicalize_basis_v2(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise MonitoringPoolIntegrityError("Monitoring pool member basis must be a non-empty list.")
    canonical_rows: list[dict[str, object]] = []
    marker_count = 0
    for row in value:
        if not isinstance(row, dict):
            raise MonitoringPoolIntegrityError("Monitoring pool basis rows must be objects.")
        source = row.get("source")
        if source == "index":
            if set(row) != {
                "source",
                "index_code",
                "security_listing_id",
                "effective_from",
                "effective_to",
            }:
                raise MonitoringPoolIntegrityError("Monitoring pool basis row shape mismatch.")
            index_code = row["index_code"]
            if not isinstance(index_code, str) or index_code not in ALLOWED_CODES:
                raise MonitoringPoolIntegrityError(
                    "Monitoring pool basis contains an invalid index code."
                )
            listing_id, effective_from, effective_to = _validated_listing_basis_values(row)
            canonical_rows.append(
                {
                    "source": "index",
                    "index_code": index_code,
                    "security_listing_id": listing_id,
                    "effective_from": effective_from,
                    "effective_to": effective_to,
                }
            )
        elif source == "watchlist":
            if set(row) == {"source"}:
                marker_count += 1
                canonical_rows.append({"source": "watchlist"})
                continue
            if set(row) != {
                "source",
                "security_listing_id",
                "effective_from",
                "effective_to",
            }:
                raise MonitoringPoolIntegrityError("Monitoring pool basis row shape mismatch.")
            listing_id, effective_from, effective_to = _validated_listing_basis_values(row)
            canonical_rows.append(
                {
                    "source": "watchlist",
                    "security_listing_id": listing_id,
                    "effective_from": effective_from,
                    "effective_to": effective_to,
                }
            )
        else:
            raise MonitoringPoolIntegrityError("Monitoring pool basis source is invalid.")

    if marker_count and (marker_count != 1 or len(canonical_rows) != 1):
        raise MonitoringPoolIntegrityError(
            "Monitoring pool watchlist marker must be the member's only basis row."
        )
    canonical_rows.sort(key=_v2_basis_sort_key)
    if len({_json_key(row) for row in canonical_rows}) != len(canonical_rows):
        raise MonitoringPoolIntegrityError("Monitoring pool basis contains duplicate rows.")
    return canonical_rows


def _validated_listing_basis_values(
    row: dict[str, object],
) -> tuple[str, str, str | None]:
    security_listing_id = row["security_listing_id"]
    effective_from = row["effective_from"]
    effective_to = row["effective_to"]
    if not isinstance(security_listing_id, str) or not isinstance(effective_from, str):
        raise MonitoringPoolIntegrityError("Monitoring pool basis listing identity is invalid.")
    try:
        listing_uuid = UUID(security_listing_id)
        start_date = date.fromisoformat(effective_from)
        end_date = date.fromisoformat(effective_to) if isinstance(effective_to, str) else None
        if effective_to is not None and not isinstance(effective_to, str):
            raise ValueError
        if str(listing_uuid) != security_listing_id or start_date.isoformat() != effective_from:
            raise ValueError
        if end_date is not None and end_date.isoformat() != effective_to:
            raise ValueError
        if end_date is not None and end_date <= start_date:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise MonitoringPoolIntegrityError(
            "Monitoring pool basis contains an invalid temporal identity."
        ) from error
    return security_listing_id, effective_from, effective_to


def _v2_basis_sort_key(row: dict[str, object]) -> tuple[int, str, str, str, str]:
    source = row["source"]
    if source == "index":
        return (
            0,
            str(row["index_code"]),
            str(row["security_listing_id"]),
            str(row["effective_from"]),
            str(row["effective_to"] or ""),
        )
    if set(row) == {"source"}:
        return (2, "", "", "", "")
    return (
        1,
        "",
        str(row["security_listing_id"]),
        str(row["effective_from"]),
        str(row["effective_to"] or ""),
    )


def _watchlist_company_ids_from_members_payload(
    members_payload: list[dict[str, object]],
    *,
    selector_version: str,
) -> tuple[UUID, ...]:
    if selector_version == EARNINGS_MONITORING_POOL_SELECTOR_VERSION:
        return ()
    if selector_version != EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2:
        raise MonitoringPoolIntegrityError("Monitoring pool selector version is unsupported.")
    company_ids: list[UUID] = []
    for member in members_payload:
        basis = cast(list[dict[str, object]], member["basis"])
        if any(row.get("source") == "watchlist" for row in basis):
            company_ids.append(UUID(cast(str, member["company_id"])))
    return tuple(sorted(company_ids, key=str))


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
