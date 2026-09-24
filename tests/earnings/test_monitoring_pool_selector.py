from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import UTC, date, datetime

import pytest
from django.db import (
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.utils import timezone

from audit.models import AppendOnlyRecordError
from companies.models import Company, SecurityListing
from earnings.models import MonitoringPoolMember, MonitoringPoolSnapshot
from earnings.services import (
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
    InvalidMonitoringPoolSelectorInput,
    MonitoringPoolIntegrityError,
    MonitoringPoolSelectionResult,
    UnknownMonitoringPoolSelectorVersion,
    select_monitoring_pool,
)
from indexes.models import IndexMembership, MarketIndex
from tests.earnings.helpers import make_calendar_observation

AS_OF = date(2026, 9, 30)
LISTING_FROM = date(2026, 1, 1)
MEMBERSHIP_FROM = date(2026, 1, 1)
MEMBERSHIP_TO = date(2027, 1, 1)


def _company(suffix: str, *, company_id: uuid.UUID | None = None) -> Company:
    return Company.objects.create(
        id=company_id or uuid.uuid4(),
        legal_name=f"Legal {suffix}",
        display_name=f"Company {suffix}",
    )


def _listing(
    company: Company,
    suffix: str,
    *,
    listing_id: uuid.UUID | None = None,
    effective_from: date = LISTING_FROM,
    effective_to: date | None = None,
    ticker: str | None = None,
    exchange: str = "NYSE",
    share_class: str = "",
    is_primary: bool = False,
) -> SecurityListing:
    return SecurityListing.objects.create(
        id=listing_id or uuid.uuid4(),
        company=company,
        ticker=ticker or f"T{suffix[:8].upper()}{uuid.uuid4().hex[:4].upper()}",
        exchange=exchange,
        security_name=f"{company.display_name} {suffix}",
        security_type="common_stock",
        share_class=share_class,
        is_primary=is_primary,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _membership(
    *,
    index: MarketIndex,
    listing: SecurityListing,
    effective_from: date = MEMBERSHIP_FROM,
    effective_to: date | None = MEMBERSHIP_TO,
    status: str = IndexMembership.Status.ACTIVE,
) -> IndexMembership:
    return IndexMembership.objects.create(
        index=index,
        security_listing=listing,
        status=status,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _select(
    *,
    as_of: date = AS_OF,
    selector_version: str = EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
    enabled_index_codes: tuple[str, ...] = ("SP500",),
) -> MonitoringPoolSelectionResult:
    return select_monitoring_pool(
        as_of=as_of,
        selector_version=selector_version,
        enabled_index_codes=enabled_index_codes,
    )


def _market_index(code: str) -> MarketIndex:
    group = "SMALL" if code == "RUSSELL2000" else "LARGE"
    index, _created = MarketIndex.objects.get_or_create(
        code=code,
        defaults={
            "name": code,
            "index_group": group,
            "is_enabled": True,
        },
    )
    return index


def _canonical_sha256(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


@pytest.mark.django_db
def test_same_inputs_create_then_reuse_snapshot() -> None:
    company = _company("same-inputs")
    listing = _listing(company, "same-inputs")
    index = _market_index("SP500")
    _membership(index=index, listing=listing)

    first = _select()
    second = _select()

    assert first.created is True
    assert second.created is False
    assert first.snapshot.pk == second.snapshot.pk
    assert first.input_revision == second.input_revision
    assert first.monitoring_pool_hash == second.monitoring_pool_hash
    assert first.member_count == 1
    assert MonitoringPoolSnapshot.objects.count() == 1
    assert MonitoringPoolMember.objects.count() == 1


@pytest.mark.django_db
def test_enabled_index_codes_are_normalized_sorted_and_deduped() -> None:
    company = _company("policy-normalization")
    listing = _listing(company, "policy-normalization")
    _membership(index=_market_index("SP500"), listing=listing)
    _membership(index=_market_index("NASDAQ100"), listing=listing)

    result = _select(enabled_index_codes=(" nasdaq100 ", "NASDAQ100", "sp500"))

    assert result.enabled_index_codes == ("NASDAQ100", "SP500")
    assert result.snapshot.enabled_index_codes == ["NASDAQ100", "SP500"]
    assert result.member_count == 1
    assert len(result.members[0].basis) == 2


@pytest.mark.django_db
def test_enabled_index_policy_change_changes_revision_when_members_are_stable() -> None:
    company = _company("policy-revision")
    listing = _listing(company, "policy-revision")
    _membership(index=_market_index("SP500"), listing=listing)

    first = _select(enabled_index_codes=("SP500",))
    second = _select(enabled_index_codes=("SP500", "NASDAQ100"))

    assert first.member_count == second.member_count == 1
    assert first.input_revision != second.input_revision
    assert first.monitoring_pool_hash != second.monitoring_pool_hash
    assert first.snapshot.pk != second.snapshot.pk


@pytest.mark.django_db
def test_member_order_uses_company_uuid_not_insertion_order() -> None:
    high_id = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    low_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
    high = _company("high", company_id=high_id)
    low = _company("low", company_id=low_id)
    index = _market_index("SP500")
    _membership(index=index, listing=_listing(high, "high"))
    _membership(index=index, listing=_listing(low, "low"))

    result = _select()

    assert [member.company_id for member in result.members] == [low_id, high_id]
    assert [member.ordinal for member in result.members] == [0, 1]


@pytest.mark.django_db
def test_multiple_listings_and_share_classes_dedupe_to_one_company_member() -> None:
    company = _company("multi-listing")
    sp500 = _market_index("SP500")
    nasdaq = _market_index("NASDAQ100")
    listing_a = _listing(company, "class-a", ticker="AAA", share_class="A")
    listing_b = _listing(company, "class-b", ticker="BBB", share_class="B")
    _membership(index=sp500, listing=listing_a)
    _membership(index=nasdaq, listing=listing_b)

    result = _select(enabled_index_codes=("SP500", "NASDAQ100"))

    assert result.member_count == 1
    assert result.members[0].company_id == company.pk
    assert {
        (basis["index_code"], basis["security_listing_id"]) for basis in result.members[0].basis
    } == {
        ("SP500", str(listing_a.pk)),
        ("NASDAQ100", str(listing_b.pk)),
    }


@pytest.mark.parametrize(
    ("as_of", "expected_count"),
    (
        (date(2025, 12, 31), 0),
        (date(2026, 1, 1), 1),
        (date(2026, 6, 30), 1),
        (date(2026, 7, 1), 0),
        (date(2026, 10, 1), 0),
    ),
)
@pytest.mark.django_db
def test_as_of_uses_half_open_temporal_intervals(
    as_of: date,
    expected_count: int,
) -> None:
    company = _company(f"as-of-{as_of.isoformat()}")
    listing = _listing(company, f"as-of-{as_of.isoformat()}")
    _membership(
        index=_market_index("SP500"),
        listing=listing,
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 7, 1),
    )

    result = _select(as_of=as_of)

    assert result.member_count == expected_count


@pytest.mark.django_db
def test_listing_effective_to_boundary_excludes_company() -> None:
    company = _company("listing-boundary")
    listing = _listing(
        company,
        "listing-boundary",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 7, 1),
    )
    _membership(
        index=_market_index("SP500"),
        listing=listing,
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 7, 1),
    )

    assert _select(as_of=date(2026, 6, 30)).member_count == 1
    assert _select(as_of=date(2026, 7, 1)).member_count == 0


@pytest.mark.parametrize("status", ("cancelled", "corrected"))
@pytest.mark.django_db
def test_non_normative_membership_statuses_are_excluded(status: str) -> None:
    company = _company(f"non-normative-{status}")
    listing = _listing(company, f"non-normative-{status}")
    _membership(
        index=_market_index("SP500"),
        listing=listing,
        status=status,
    )

    assert _select().member_count == 0


@pytest.mark.django_db
def test_announced_membership_is_normative_once_effective() -> None:
    company = _company("announced")
    listing = _listing(company, "announced")
    _membership(
        index=_market_index("SP500"),
        listing=listing,
        status=IndexMembership.Status.ANNOUNCED,
    )

    assert _select().member_count == 1


@pytest.mark.django_db
def test_explicit_policy_does_not_expand_to_other_indexes() -> None:
    sp500_company = _company("sp500-only")
    nasdaq_company = _company("nasdaq-only")
    sp500 = _market_index("SP500")
    nasdaq = _market_index("NASDAQ100")
    _membership(index=sp500, listing=_listing(sp500_company, "sp500-only"))
    _membership(index=nasdaq, listing=_listing(nasdaq_company, "nasdaq-only"))

    result = _select(enabled_index_codes=("SP500",))

    assert [member.company_id for member in result.members] == [sp500_company.pk]


@pytest.mark.django_db
def test_current_market_index_is_enabled_flag_does_not_change_historical_selection() -> None:
    company = _company("disabled-current")
    listing = _listing(company, "disabled-current")
    sp500 = _market_index("SP500")
    _membership(index=sp500, listing=listing)
    MarketIndex.objects.filter(pk=sp500.pk).update(is_enabled=False)

    result = _select(enabled_index_codes=("SP500",))

    assert result.member_count == 1


@pytest.mark.django_db
def test_company_monitoring_status_is_not_selector_input() -> None:
    company = _company("monitoring-status")
    listing = _listing(company, "monitoring-status")
    _membership(index=_market_index("SP500"), listing=listing)
    Company.objects.filter(pk=company.pk).update(monitoring_status="inactive")

    result = _select()

    assert [member.company_id for member in result.members] == [company.pk]


@pytest.mark.parametrize("codes", ((), [" "], "SP500"))
@pytest.mark.django_db
def test_invalid_enabled_index_codes_are_rejected(codes: object) -> None:
    with pytest.raises(InvalidMonitoringPoolSelectorInput):
        select_monitoring_pool(
            as_of=AS_OF,
            selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
            enabled_index_codes=codes,  # type: ignore[arg-type]
        )


@pytest.mark.django_db
def test_unknown_enabled_index_code_is_rejected() -> None:
    with pytest.raises(InvalidMonitoringPoolSelectorInput, match="Unknown"):
        _select(enabled_index_codes=("UNKNOWN",))


@pytest.mark.django_db
def test_unknown_selector_version_is_rejected() -> None:
    with pytest.raises(UnknownMonitoringPoolSelectorVersion):
        _select(selector_version="earnings-monitoring-pool-v2")


def test_datetime_as_of_is_rejected() -> None:
    with pytest.raises(InvalidMonitoringPoolSelectorInput):
        select_monitoring_pool(
            as_of=datetime(2026, 9, 30, tzinfo=UTC),
            selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION,
            enabled_index_codes=("SP500",),
        )


@pytest.mark.django_db
def test_missing_market_index_row_is_rejected() -> None:
    MarketIndex.objects.filter(code="SP500").delete()

    with pytest.raises(InvalidMonitoringPoolSelectorInput, match="do not exist"):
        _select()


@pytest.mark.django_db
def test_selector_does_not_read_current_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    company = _company("no-clock")
    listing = _listing(company, "no-clock")
    _membership(index=_market_index("SP500"), listing=listing)

    def fail_now() -> None:
        raise AssertionError("selector must not read the current clock")

    monkeypatch.setattr(timezone, "localdate", fail_now)
    result = _select()

    assert result.member_count == 1


@pytest.mark.django_db
def test_expected_input_revision_and_pool_hash_payload() -> None:
    company_id = uuid.UUID("00000000-0000-4000-8000-000000000101")
    listing_id = uuid.UUID("00000000-0000-4000-8000-000000000102")
    membership_id = uuid.UUID("00000000-0000-4000-8000-000000000103")
    company = _company("expected-hash", company_id=company_id)
    listing = _listing(
        company,
        "expected-hash",
        listing_id=listing_id,
        effective_from=date(2025, 1, 1),
    )
    IndexMembership.objects.create(
        id=membership_id,
        index=_market_index("SP500"),
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
        effective_to=date(2027, 1, 1),
    )

    result = _select()

    expected_input_revision = _canonical_sha256(
        {
            "enabled_index_codes": ["SP500"],
            "memberships": [
                {
                    "company_id": str(company_id),
                    "index_code": "SP500",
                    "listing_effective_from": "2025-01-01",
                    "listing_effective_to": None,
                    "membership_effective_from": "2026-01-01",
                    "membership_effective_to": "2027-01-01",
                    "security_listing_id": str(listing_id),
                }
            ],
        }
    )
    expected_pool_hash = _canonical_sha256(
        {
            "as_of": "2026-09-30",
            "contract": "earnings-monitoring-pool-hash-v1",
            "enabled_index_codes": ["SP500"],
            "input_revision": expected_input_revision,
            "members": [
                {
                    "basis": [
                        {
                            "effective_from": "2026-01-01",
                            "effective_to": "2027-01-01",
                            "index_code": "SP500",
                            "security_listing_id": str(listing_id),
                        }
                    ],
                    "company_id": str(company_id),
                }
            ],
            "selector_version": "earnings-monitoring-pool-v1",
        }
    )

    assert result.input_revision == expected_input_revision
    assert result.monitoring_pool_hash == expected_pool_hash


@pytest.mark.django_db
def test_non_semantic_timestamps_and_provider_records_do_not_change_identity() -> None:
    company = _company("ignored-metadata")
    listing = _listing(company, "ignored-metadata")
    sp500 = _market_index("SP500")
    membership = _membership(index=sp500, listing=listing)
    first = _select()

    MarketIndex.objects.filter(pk=sp500.pk).update(updated_at=timezone.now())
    IndexMembership.objects.filter(pk=membership.pk).update(
        status=IndexMembership.Status.ANNOUNCED,
        last_verified_at=timezone.now(),
    )
    make_calendar_observation()
    second = _select()

    assert second.created is False
    assert second.snapshot.pk == first.snapshot.pk
    assert second.input_revision == first.input_revision
    assert second.monitoring_pool_hash == first.monitoring_pool_hash


@pytest.mark.django_db
def test_temporal_correction_creates_new_snapshot_without_mutating_old_one() -> None:
    company = _company("late-correction")
    listing = _listing(company, "late-correction")
    membership = _membership(
        index=_market_index("SP500"),
        listing=listing,
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 7, 1),
    )
    first = _select(as_of=date(2026, 6, 1))

    IndexMembership.objects.filter(pk=membership.pk).update(effective_to=date(2026, 8, 1))
    second = _select(as_of=date(2026, 7, 15))

    assert second.created is True
    assert second.snapshot.pk != first.snapshot.pk
    assert second.input_revision != first.input_revision
    assert second.monitoring_pool_hash != first.monitoring_pool_hash
    assert MonitoringPoolSnapshot.objects.count() == 2
    first.snapshot.refresh_from_db()
    assert first.snapshot.member_count == 1
    assert [
        member.company_id
        for member in MonitoringPoolMember.objects.filter(snapshot=first.snapshot).order_by(
            "ordinal"
        )
    ] == [company.pk]


@pytest.mark.django_db
def test_listing_interval_change_changes_input_revision() -> None:
    company = _company("listing-revision")
    listing = _listing(
        company,
        "listing-revision",
        effective_from=date(2026, 1, 1),
        effective_to=date(2027, 1, 1),
    )
    _membership(
        index=_market_index("SP500"),
        listing=listing,
        effective_from=date(2026, 1, 1),
        effective_to=date(2027, 1, 1),
    )
    first = _select()

    SecurityListing.objects.filter(pk=listing.pk).update(effective_to=date(2027, 6, 1))
    second = _select()

    assert second.snapshot.pk != first.snapshot.pk
    assert second.input_revision != first.input_revision
    assert second.monitoring_pool_hash != first.monitoring_pool_hash


@pytest.mark.django_db
def test_member_change_changes_pool_hash() -> None:
    first_company = _company("member-a")
    second_company = _company("member-b")
    index = _market_index("SP500")
    _membership(index=index, listing=_listing(first_company, "member-a"))
    first = _select()

    _membership(index=index, listing=_listing(second_company, "member-b"))
    second = _select()

    assert first.member_count == 1
    assert second.member_count == 2
    assert first.monitoring_pool_hash != second.monitoring_pool_hash
    assert first.input_revision != second.input_revision


@pytest.mark.django_db
def test_empty_pool_is_valid_and_deterministic() -> None:
    first = _select(as_of=date(2025, 1, 1))
    second = _select(as_of=date(2025, 1, 1))

    assert first.created is True
    assert first.member_count == 0
    assert first.members == ()
    assert len(first.monitoring_pool_hash) == 64
    assert second.created is False
    assert second.snapshot.pk == first.snapshot.pk


@pytest.mark.django_db
def test_snapshot_and_members_are_append_only() -> None:
    company = _company("append-only")
    _membership(
        index=_market_index("SP500"),
        listing=_listing(company, "append-only"),
    )
    result = _select()
    snapshot = result.snapshot
    member = result.members[0]

    with pytest.raises(AppendOnlyRecordError):
        snapshot.member_count = 99
        snapshot.save()
    with pytest.raises(AppendOnlyRecordError):
        MonitoringPoolSnapshot.objects.filter(pk=snapshot.pk).update(member_count=99)
    with pytest.raises(AppendOnlyRecordError):
        MonitoringPoolMember.objects.filter(pk=member.pk).update(ordinal=99)
    with pytest.raises(AppendOnlyRecordError):
        snapshot.delete()


@pytest.mark.django_db
def test_snapshot_identity_constraints_are_enforced() -> None:
    company = _company("snapshot-constraints")
    _membership(
        index=_market_index("SP500"),
        listing=_listing(company, "snapshot-constraints"),
    )
    result = _select()
    snapshot = result.snapshot

    with pytest.raises(IntegrityError), transaction.atomic():
        MonitoringPoolSnapshot.objects.create(
            as_of_date=snapshot.as_of_date,
            selector_version=snapshot.selector_version,
            enabled_index_codes=list(snapshot.enabled_index_codes),
            input_revision="b" * 64,
            pool_hash=snapshot.pool_hash,
            member_count=snapshot.member_count,
        )

    other = MonitoringPoolSnapshot.objects.create(
        as_of_date=date(2027, 1, 1),
        selector_version=snapshot.selector_version,
        enabled_index_codes=list(snapshot.enabled_index_codes),
        input_revision="c" * 64,
        pool_hash="d" * 64,
        member_count=0,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        MonitoringPoolMember.objects.create(
            snapshot=other,
            company=company,
            ordinal=0,
            basis=[],
        )
    other_company = _company("snapshot-constraints-other")
    MonitoringPoolMember.objects.create(
        snapshot=other,
        company=company,
        ordinal=0,
        basis=[{"index_code": "SP500"}],
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        MonitoringPoolMember.objects.create(
            snapshot=other,
            company=other_company,
            ordinal=0,
            basis=[{"index_code": "SP500"}],
        )
    MonitoringPoolMember.objects.create(
        snapshot=other,
        company=other_company,
        ordinal=1,
        basis=[{"index_code": "SP500"}],
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        MonitoringPoolMember.objects.create(
            snapshot=other,
            company=other_company,
            ordinal=2,
            basis=[{"index_code": "SP500"}],
        )


@pytest.mark.django_db
def test_corrupted_snapshot_is_rejected_on_reuse() -> None:
    company = _company("corrupted-snapshot")
    _membership(
        index=_market_index("SP500"),
        listing=_listing(company, "corrupted-snapshot"),
    )
    result = _select()
    table = MonitoringPoolSnapshot._meta.db_table

    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE "{table}" SET member_count = %s WHERE id = %s',
            [99, result.snapshot.pk],
        )

    with pytest.raises(MonitoringPoolIntegrityError, match="member count"):
        _select()


@pytest.mark.django_db
def test_corrupted_input_revision_is_rejected_on_reuse() -> None:
    company = _company("corrupted-revision")
    _membership(
        index=_market_index("SP500"),
        listing=_listing(company, "corrupted-revision"),
    )
    result = _select()
    table = MonitoringPoolSnapshot._meta.db_table

    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE "{table}" SET input_revision = %s WHERE id = %s',
            ["b" * 64, result.snapshot.pk],
        )

    with pytest.raises(MonitoringPoolIntegrityError, match="input revision"):
        _select()


@pytest.mark.django_db
def test_corrupted_pool_hash_is_rejected_on_reuse() -> None:
    company = _company("corrupted-hash")
    _membership(
        index=_market_index("SP500"),
        listing=_listing(company, "corrupted-hash"),
    )
    result = _select()
    table = MonitoringPoolSnapshot._meta.db_table

    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE "{table}" SET pool_hash = %s WHERE id = %s',
            ["b" * 64, result.snapshot.pk],
        )

    with pytest.raises(MonitoringPoolIntegrityError, match="hash mismatch"):
        _select()


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_selector_contract_creates_one_snapshot() -> None:
    company = _company("concurrency")
    _membership(
        index=_market_index("SP500"),
        listing=_listing(company, "concurrency"),
    )
    barrier = threading.Barrier(2, timeout=10)
    results: list[MonitoringPoolSelectionResult] = []
    errors: list[Exception] = []

    def worker() -> None:
        close_old_connections()
        try:
            barrier.wait()
            results.append(_select())
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)
        finally:
            for connection in connections.all():
                connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert {result.snapshot.pk for result in results} == {results[0].snapshot.pk}
    assert sum(result.created for result in results) == 1
    assert MonitoringPoolSnapshot.objects.count() == 1
    assert MonitoringPoolMember.objects.count() == 1


def test_retry_and_replay_modules_do_not_expose_selector() -> None:
    from earnings.services import calendar_execution, calendar_replay_orchestration

    assert not hasattr(calendar_execution, "select_monitoring_pool")
    assert not hasattr(calendar_replay_orchestration, "select_monitoring_pool")
