from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from datetime import date

import pytest
from django.db import connection

from companies.models import Company, SecurityListing
from earnings.models import MonitoringPoolMember, MonitoringPoolSnapshot
from earnings.services import (
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2,
    WATCHLIST_MARKER_NO_EFFECTIVE_LISTING_REASON,
    WATCHLIST_MARKER_SOURCE,
    InvalidMonitoringPoolSelectorInput,
    MonitoringPoolIntegrityError,
    MonitoringPoolSelectionResult,
    PoolMemberDiagnostic,
    is_watchlist_marker_basis,
    monitoring_pool_member_diagnostics,
    resolve_monitoring_pool_snapshot_contract,
    select_monitoring_pool,
)
from indexes.models import IndexMembership, MarketIndex

AS_OF = date(2026, 9, 30)
V2 = EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2
INPUT_CONTRACT_V2 = "earnings-monitoring-pool-input-v2"
HASH_CONTRACT_V2 = "earnings-monitoring-pool-hash-v2"


def _sha256_json(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


def _company(suffix: str, *, company_id: uuid.UUID | None = None) -> Company:
    return Company.objects.create(
        id=company_id or uuid.uuid4(),
        legal_name=f"Monitoring v2 {suffix} Inc",
        display_name=f"Monitoring v2 {suffix}",
    )


def _listing(
    company: Company,
    suffix: str,
    *,
    listing_id: uuid.UUID | None = None,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> SecurityListing:
    return SecurityListing.objects.create(
        id=listing_id or uuid.uuid4(),
        company=company,
        ticker=f"V{suffix[:5].upper()}{uuid.uuid4().hex[:5].upper()}",
        exchange="NYSE",
        security_name=f"{company.display_name} Common",
        security_type="common_stock",
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _index() -> MarketIndex:
    index, _created = MarketIndex.objects.get_or_create(
        code="SP500",
        defaults={
            "name": "S&P 500",
            "index_group": MarketIndex.IndexGroup.LARGE,
            "is_enabled": True,
        },
    )
    return index


def _membership(listing: SecurityListing) -> IndexMembership:
    return IndexMembership.objects.create(
        index=_index(),
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )


def _select_v2(company_ids: Iterable[uuid.UUID]) -> MonitoringPoolSelectionResult:
    return select_monitoring_pool(
        as_of=AS_OF,
        selector_version=V2,
        enabled_index_codes=("SP500",),
        watchlist_company_ids=company_ids,
    )


def test_watchlist_marker_classifier_is_exact_and_v2_only() -> None:
    marker = {"source": "watchlist"}

    assert is_watchlist_marker_basis(marker, selector_version=V2) is True
    assert (
        is_watchlist_marker_basis(
            marker,
            selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        )
        is False
    )
    for malformed in (
        {},
        {"source": "index"},
        {"source": "watchlist", "security_listing_id": None},
        {"source": "watchlist", "unexpected": "value"},
        None,
    ):
        assert is_watchlist_marker_basis(malformed, selector_version=V2) is False


def test_watchlist_marker_diagnostics_are_stably_sorted_and_deduplicated() -> None:
    higher = uuid.UUID("00000000-0000-0000-0000-000000000002")
    lower = uuid.UUID("00000000-0000-0000-0000-000000000001")
    members = (
        MonitoringPoolMember(company_id=higher, ordinal=0, basis=[{"source": "watchlist"}]),
        MonitoringPoolMember(company_id=lower, ordinal=1, basis=[{"source": "watchlist"}]),
        MonitoringPoolMember(company_id=higher, ordinal=2, basis=[{"source": "watchlist"}]),
    )

    diagnostics = monitoring_pool_member_diagnostics(members, selector_version=V2)

    assert diagnostics == (
        PoolMemberDiagnostic(
            company_id=lower,
            reason=WATCHLIST_MARKER_NO_EFFECTIVE_LISTING_REASON,
            source=WATCHLIST_MARKER_SOURCE,
        ),
        PoolMemberDiagnostic(
            company_id=higher,
            reason=WATCHLIST_MARKER_NO_EFFECTIVE_LISTING_REASON,
            source=WATCHLIST_MARKER_SOURCE,
        ),
    )
    assert (
        monitoring_pool_member_diagnostics(
            members,
            selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        )
        == ()
    )


@pytest.mark.django_db
def test_selector_versions_enforce_their_watchlist_input_contract() -> None:
    _index()

    with pytest.raises(InvalidMonitoringPoolSelectorInput, match="must be provided"):
        select_monitoring_pool(
            as_of=AS_OF,
            selector_version=V2,
            enabled_index_codes=("SP500",),
        )
    with pytest.raises(InvalidMonitoringPoolSelectorInput, match="must be None"):
        select_monitoring_pool(
            as_of=AS_OF,
            selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
            enabled_index_codes=("SP500",),
            watchlist_company_ids=(),
        )
    with pytest.raises(InvalidMonitoringPoolSelectorInput, match="unknown Company"):
        _select_v2((uuid.uuid4(),))


@pytest.mark.django_db
def test_v2_unions_index_and_watchlist_members_with_canonical_source_basis() -> None:
    index_only = _company(
        "index-only",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
    )
    both = _company(
        "both",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
    )
    watchlist_only = _company(
        "watchlist-only",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    index_listing = _listing(index_only, "index-only")
    both_listing = _listing(both, "both")
    watchlist_listing = _listing(watchlist_only, "watchlist-only")
    _membership(index_listing)
    _membership(both_listing)

    result = _select_v2((both.pk, watchlist_only.pk, both.pk))

    assert result.watchlist_company_ids == (watchlist_only.pk, both.pk)
    assert tuple(member.company_id for member in result.members) == (
        watchlist_only.pk,
        both.pk,
        index_only.pk,
    )
    basis_by_company = {member.company_id: member.basis for member in result.members}
    assert [row["source"] for row in basis_by_company[index_only.pk]] == ["index"]
    assert [row["source"] for row in basis_by_company[both.pk]] == [
        "index",
        "watchlist",
    ]
    assert basis_by_company[watchlist_only.pk] == [
        {
            "source": "watchlist",
            "security_listing_id": str(watchlist_listing.pk),
            "effective_from": "2026-01-01",
            "effective_to": None,
        }
    ]


@pytest.mark.django_db
def test_v2_uses_marker_only_basis_when_watchlist_company_has_no_effective_listing() -> None:
    _index()
    company = _company("marker")

    result = _select_v2((company.pk,))

    assert result.member_count == 1
    assert result.members[0].company_id == company.pk
    assert result.members[0].basis == [{"source": "watchlist"}]


@pytest.mark.django_db
def test_v2_rejects_marker_and_listing_in_same_basis() -> None:
    _index()
    company = _company("marker-mixed")
    listing = _listing(company, "marker-mixed")
    snapshot = MonitoringPoolSnapshot.objects.create(
        as_of_date=AS_OF,
        selector_version=V2,
        enabled_index_codes=["SP500"],
        input_revision="a" * 64,
        pool_hash="b" * 64,
        member_count=1,
    )
    MonitoringPoolMember.objects.create(
        snapshot=snapshot,
        company=company,
        ordinal=0,
        basis=[
            {
                "source": "watchlist",
                "security_listing_id": str(listing.pk),
                "effective_from": "2026-01-01",
                "effective_to": None,
            },
            {"source": "watchlist"},
        ],
    )

    with pytest.raises(MonitoringPoolIntegrityError, match="only basis row"):
        resolve_monitoring_pool_snapshot_contract(
            as_of=AS_OF,
            selector_version=V2,
            pool_hash="b" * 64,
        )


@pytest.mark.django_db
def test_v2_rejects_invalid_basis_source() -> None:
    _index()
    company = _company("invalid-source")
    snapshot = MonitoringPoolSnapshot.objects.create(
        as_of_date=AS_OF,
        selector_version=V2,
        enabled_index_codes=["SP500"],
        input_revision="a" * 64,
        pool_hash="b" * 64,
        member_count=1,
    )
    MonitoringPoolMember.objects.create(
        snapshot=snapshot,
        company=company,
        ordinal=0,
        basis=[{"source": "unknown"}],
    )

    with pytest.raises(MonitoringPoolIntegrityError, match="source is invalid"):
        resolve_monitoring_pool_snapshot_contract(
            as_of=AS_OF,
            selector_version=V2,
            pool_hash="b" * 64,
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "basis",
    (
        {"source": "index"},
        {"source": "watchlist", "effective_from": "2026-01-01", "effective_to": None},
        {"source": "watchlist", "security_listing_id": None},
        {"source": "watchlist", "unexpected": "value"},
        {"source": "unknown"},
    ),
)
def test_v2_resolver_rejects_malformed_marker_like_basis(
    basis: dict[str, object],
) -> None:
    company = _company(f"malformed-{uuid.uuid4().hex[:8]}")
    snapshot = MonitoringPoolSnapshot.objects.create(
        as_of_date=AS_OF,
        selector_version=V2,
        enabled_index_codes=["SP500"],
        input_revision="a" * 64,
        pool_hash="b" * 64,
        member_count=1,
    )
    MonitoringPoolMember.objects.create(
        snapshot=snapshot,
        company=company,
        ordinal=0,
        basis=[basis],
    )

    with pytest.raises(MonitoringPoolIntegrityError):
        resolve_monitoring_pool_snapshot_contract(
            as_of=AS_OF,
            selector_version=V2,
            pool_hash="b" * 64,
        )


@pytest.mark.django_db
def test_v2_rejects_invalid_watchlist_listing_interval() -> None:
    _index()
    company = _company("invalid-interval")
    listing = _listing(company, "invalid-interval")
    snapshot = MonitoringPoolSnapshot.objects.create(
        as_of_date=AS_OF,
        selector_version=V2,
        enabled_index_codes=["SP500"],
        input_revision="a" * 64,
        pool_hash="b" * 64,
        member_count=1,
    )
    MonitoringPoolMember.objects.create(
        snapshot=snapshot,
        company=company,
        ordinal=0,
        basis=[
            {
                "source": "watchlist",
                "security_listing_id": str(listing.pk),
                "effective_from": "2026-01-01",
                "effective_to": "2025-01-01",
            }
        ],
    )

    with pytest.raises(MonitoringPoolIntegrityError, match="temporal identity"):
        resolve_monitoring_pool_snapshot_contract(
            as_of=AS_OF,
            selector_version=V2,
            pool_hash="b" * 64,
        )


@pytest.mark.django_db
def test_v2_rejects_noncanonical_member_order() -> None:
    higher_company = _company(
        "order-higher",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
    )
    lower_company = _company(
        "order-lower",
        company_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )
    higher_listing = _listing(higher_company, "order-higher")
    lower_listing = _listing(lower_company, "order-lower")
    higher_basis = [
        {
            "source": "watchlist",
            "security_listing_id": str(higher_listing.pk),
            "effective_from": "2026-01-01",
            "effective_to": None,
        }
    ]
    lower_basis = [
        {
            "source": "watchlist",
            "security_listing_id": str(lower_listing.pk),
            "effective_from": "2026-01-01",
            "effective_to": None,
        }
    ]
    input_revision = _sha256_json(
        {
            "contract": INPUT_CONTRACT_V2,
            "enabled_index_codes": ["SP500"],
            "index_memberships": [],
            "watchlist_company_ids": [str(lower_company.pk), str(higher_company.pk)],
            "watchlist_listings": [
                {
                    "company_id": str(lower_company.pk),
                    "security_listing_id": str(lower_listing.pk),
                    "listing_effective_from": "2026-01-01",
                    "listing_effective_to": None,
                },
                {
                    "company_id": str(higher_company.pk),
                    "security_listing_id": str(higher_listing.pk),
                    "listing_effective_from": "2026-01-01",
                    "listing_effective_to": None,
                },
            ],
        }
    )
    members = [
        {"company_id": str(higher_company.pk), "basis": higher_basis},
        {"company_id": str(lower_company.pk), "basis": lower_basis},
    ]
    pool_hash = _sha256_json(
        {
            "contract": HASH_CONTRACT_V2,
            "selector_version": V2,
            "as_of": AS_OF.isoformat(),
            "enabled_index_codes": ["SP500"],
            "input_revision": input_revision,
            "members": members,
        }
    )
    snapshot = MonitoringPoolSnapshot.objects.create(
        as_of_date=AS_OF,
        selector_version=V2,
        enabled_index_codes=["SP500"],
        input_revision=input_revision,
        pool_hash=pool_hash,
        member_count=2,
    )
    MonitoringPoolMember.objects.create(
        snapshot=snapshot,
        company=higher_company,
        ordinal=0,
        basis=higher_basis,
    )
    MonitoringPoolMember.objects.create(
        snapshot=snapshot,
        company=lower_company,
        ordinal=1,
        basis=lower_basis,
    )

    with pytest.raises(MonitoringPoolIntegrityError, match="canonically ordered"):
        resolve_monitoring_pool_snapshot_contract(
            as_of=AS_OF,
            selector_version=V2,
            pool_hash=pool_hash,
        )


@pytest.mark.django_db
def test_v2_empty_watchlist_ids_create_empty_snapshot() -> None:
    _index()

    result = _select_v2(())

    assert result.created is True
    assert result.member_count == 0
    assert result.watchlist_company_ids == ()
    assert result.members == ()


@pytest.mark.django_db
def test_v2_input_order_and_duplicates_reuse_the_same_snapshot() -> None:
    _index()
    first = _company("canonical-first")
    second = _company("canonical-second")
    _listing(first, "canonical-first")
    _listing(second, "canonical-second")

    initial = _select_v2((second.pk, first.pk, second.pk))
    repeated = _select_v2((first.pk, second.pk))

    assert initial.created is True
    assert repeated.created is False
    assert repeated.snapshot.pk == initial.snapshot.pk
    assert repeated.input_revision == initial.input_revision
    assert repeated.monitoring_pool_hash == initial.monitoring_pool_hash


@pytest.mark.django_db
def test_v2_listing_input_change_creates_a_new_revision_and_hash() -> None:
    _index()
    company = _company("listing-change")
    _listing(company, "listing-change-first")
    initial = _select_v2((company.pk,))

    _listing(company, "listing-change-second")
    changed = _select_v2((company.pk,))

    assert changed.created is True
    assert changed.snapshot.pk != initial.snapshot.pk
    assert changed.input_revision != initial.input_revision
    assert changed.monitoring_pool_hash != initial.monitoring_pool_hash


@pytest.mark.django_db
def test_v2_hashes_the_exact_canonical_input_and_member_contract() -> None:
    _index()
    company_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
    listing_id = uuid.UUID("20000000-0000-0000-0000-000000000001")
    company = _company("exact-hash", company_id=company_id)
    _listing(company, "exact-hash", listing_id=listing_id)

    result = _select_v2((company_id,))

    expected_input_revision = _sha256_json(
        {
            "contract": INPUT_CONTRACT_V2,
            "enabled_index_codes": ["SP500"],
            "index_memberships": [],
            "watchlist_company_ids": [str(company_id)],
            "watchlist_listings": [
                {
                    "company_id": str(company_id),
                    "security_listing_id": str(listing_id),
                    "listing_effective_from": "2026-01-01",
                    "listing_effective_to": None,
                }
            ],
        }
    )
    members = [
        {
            "company_id": str(company_id),
            "basis": [
                {
                    "source": "watchlist",
                    "security_listing_id": str(listing_id),
                    "effective_from": "2026-01-01",
                    "effective_to": None,
                }
            ],
        }
    ]
    expected_hash = _sha256_json(
        {
            "contract": HASH_CONTRACT_V2,
            "selector_version": V2,
            "as_of": AS_OF.isoformat(),
            "enabled_index_codes": ["SP500"],
            "input_revision": expected_input_revision,
            "members": members,
        }
    )

    assert result.input_revision == expected_input_revision
    assert result.monitoring_pool_hash == expected_hash
    assert "user" not in json.dumps(members)
    assert "email" not in json.dumps(members)


@pytest.mark.django_db
def test_v2_resolver_rejects_noncanonical_persisted_basis() -> None:
    company = _company("noncanonical")
    listing = _listing(company, "noncanonical")
    _membership(listing)
    result = _select_v2((company.pk,))
    member = result.members[0]
    assert len(member.basis) == 2
    table = MonitoringPoolMember._meta.db_table

    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE "{table}" SET basis = %s::jsonb WHERE id = %s',
            [json.dumps(list(reversed(member.basis))), member.pk],
        )

    with pytest.raises(MonitoringPoolIntegrityError, match="canonically ordered"):
        resolve_monitoring_pool_snapshot_contract(
            as_of=result.as_of_date,
            selector_version=result.selector_version,
            pool_hash=result.monitoring_pool_hash,
        )


@pytest.mark.django_db
def test_v2_resolver_rebuilds_and_validates_the_input_revision() -> None:
    _index()
    company = _company("revision-integrity")
    result = _select_v2((company.pk,))
    tampered_revision = "b" * 64
    members = [
        {
            "company_id": str(member.company_id),
            "basis": member.basis,
        }
        for member in result.members
    ]
    tampered_hash = _sha256_json(
        {
            "contract": HASH_CONTRACT_V2,
            "selector_version": V2,
            "as_of": AS_OF.isoformat(),
            "enabled_index_codes": ["SP500"],
            "input_revision": tampered_revision,
            "members": members,
        }
    )
    table = MonitoringPoolSnapshot._meta.db_table

    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE "{table}" SET input_revision = %s, pool_hash = %s WHERE id = %s',
            [tampered_revision, tampered_hash, result.snapshot.pk],
        )

    with pytest.raises(MonitoringPoolIntegrityError, match="input revision mismatch"):
        resolve_monitoring_pool_snapshot_contract(
            as_of=result.as_of_date,
            selector_version=result.selector_version,
            pool_hash=tampered_hash,
        )


@pytest.mark.django_db
def test_resolver_dispatches_for_v1_and_v2_snapshots() -> None:
    company = _company("resolver")
    listing = _listing(company, "resolver")
    _membership(listing)
    v1 = select_monitoring_pool(
        as_of=AS_OF,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
        enabled_index_codes=("SP500",),
    )
    v2 = _select_v2((company.pk,))

    resolved_v1 = resolve_monitoring_pool_snapshot_contract(
        as_of=v1.as_of_date,
        selector_version=v1.selector_version,
        pool_hash=v1.monitoring_pool_hash,
    )
    resolved_v2 = resolve_monitoring_pool_snapshot_contract(
        as_of=v2.as_of_date,
        selector_version=v2.selector_version,
        pool_hash=v2.monitoring_pool_hash,
    )

    assert resolved_v1.snapshot.pk == v1.snapshot.pk
    assert resolved_v2.snapshot.pk == v2.snapshot.pk
    assert tuple(member.company_id for member in resolved_v2.members) == (company.pk,)
