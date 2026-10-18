from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from django.utils import timezone

from accounts.models import User
from audit.models import DataSource, SyncRun
from audit.services import start_sync_run
from companies.models import Company, SecurityListing
from earnings.services import (
    CurrentMonitoringPoolComposition,
    compose_current_monitoring_pool,
    recompute_monitoring_pool_composition,
)
from indexes.models import IndexMembership, MarketIndex
from watchlists.models import WatchlistItem

AS_OF = date(2026, 10, 18)


def _user(suffix: str) -> User:
    return User.objects.create_user(
        email=f"composition-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-only",
    )


def _company(
    suffix: str,
    *,
    cik_number: int | None = None,
    monitoring_status: str = Company.MonitoringStatus.PENDING_IDENTITY,
) -> Company:
    return Company.objects.create(
        legal_name=f"Composition {suffix} Inc",
        display_name=f"Composition {suffix}",
        cik=f"{cik_number:010d}" if cik_number is not None else None,
        monitoring_status=monitoring_status,
    )


def _listing(
    company: Company,
    *,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> SecurityListing:
    return SecurityListing.objects.create(
        company=company,
        ticker=f"C{uuid.uuid4().hex[:7].upper()}",
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


def _membership(
    *,
    listing: SecurityListing,
    effective_from: date,
    effective_to: date | None = None,
) -> IndexMembership:
    return IndexMembership.objects.create(
        index=_index(),
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _sync_run(suffix: str) -> SyncRun:
    token = uuid.uuid4().hex
    source = DataSource.objects.create(
        key=f"composition-{suffix}-{token[:8]}",
        name=f"Composition {suffix}",
        source_type=DataSource.SourceType.MANUAL,
        base_url=f"https://composition-{token[:8]}.example.test/",
        license_notes="Synthetic test-only source.",
    )
    return start_sync_run(
        job_type="fixture.monitoring-pool-composition",
        source=source,
        scope={"fixture": suffix},
        idempotency_key=f"fixture.monitoring-pool-composition:{token}",
    )


def _compose(*, as_of: date = AS_OF) -> CurrentMonitoringPoolComposition:
    return compose_current_monitoring_pool(
        as_of=as_of,
        enabled_index_codes=("SP500",),
    )


@pytest.mark.django_db
def test_composition_is_deterministic_and_tracks_global_watchlist_changes() -> None:
    index = _index()
    first_user = _user("first")
    second_user = _user("second")
    first = _company(
        "first",
        cik_number=1,
        monitoring_status=Company.MonitoringStatus.INACTIVE,
    )
    second = _company(
        "second",
        cik_number=2,
        monitoring_status=Company.MonitoringStatus.INACTIVE,
    )
    _listing(first)
    _listing(second)
    first_item = WatchlistItem.objects.create(user=first_user, company=first)

    initial = _compose()
    repeated = _compose()

    assert initial.selection.snapshot.pk == repeated.selection.snapshot.pk
    assert initial.selection.monitoring_pool_hash == repeated.selection.monitoring_pool_hash
    assert initial.active_watchlist_company_ids == (first.pk,)
    assert initial.affected_company_ids == (first.pk,)

    second_item = WatchlistItem.objects.create(user=second_user, company=second)
    changed = _compose()

    assert changed.selection.created is True
    assert changed.selection.snapshot.pk != initial.selection.snapshot.pk
    assert changed.selection.input_revision != initial.selection.input_revision
    assert changed.selection.monitoring_pool_hash != initial.selection.monitoring_pool_hash
    assert changed.active_watchlist_company_ids == tuple(sorted((first.pk, second.pk), key=str))
    assert set(changed.affected_company_ids) == {first.pk, second.pk}

    snapshot_payload = json.dumps(
        {
            "selector_version": changed.selection.selector_version,
            "enabled_index_codes": list(changed.selection.enabled_index_codes),
            "members": [
                {"company_id": str(member.company_id), "basis": member.basis}
                for member in changed.selection.members
            ],
        }
    )
    assert index.code in changed.selection.enabled_index_codes
    assert first_user.email not in snapshot_payload
    assert str(first_item.pk) not in snapshot_payload
    assert str(second_item.pk) not in snapshot_payload
    assert "priority_level" not in snapshot_payload
    assert "alerts_enabled" not in snapshot_payload


@pytest.mark.django_db
def test_listing_change_creates_new_v2_snapshot_and_keeps_member_affected() -> None:
    _index()
    user = _user("listing")
    company = _company("listing", cik_number=3)
    _listing(company)
    WatchlistItem.objects.create(user=user, company=company)
    initial = _compose()

    _listing(company)
    changed = _compose()

    assert changed.selection.created is True
    assert changed.selection.snapshot.pk != initial.selection.snapshot.pk
    assert changed.selection.input_revision != initial.selection.input_revision
    assert company.pk in changed.affected_company_ids


@pytest.mark.django_db
def test_expiring_listing_becomes_marker_and_recomputes_pending_identity() -> None:
    user = _user("expired")
    company = _company("expired")
    _listing(company, effective_to=AS_OF)
    WatchlistItem.objects.create(user=user, company=company)
    sync_run = _sync_run("expired")
    before = AS_OF - timedelta(days=1)

    before_composition = _compose(as_of=before)
    before_result = recompute_monitoring_pool_composition(
        composition=before_composition,
        sync_run=sync_run,
    )
    expired_composition = _compose(as_of=AS_OF)
    expired_result = recompute_monitoring_pool_composition(
        composition=expired_composition,
        sync_run=sync_run,
    )

    company.refresh_from_db()
    assert before_result.changed_company_ids == (company.pk,)
    assert expired_result.changed_company_ids == (company.pk,)
    assert expired_composition.selection.members[0].basis == [{"source": "watchlist"}]
    assert company.monitoring_status == Company.MonitoringStatus.PENDING_IDENTITY


@pytest.mark.django_db
def test_prior_index_member_leaves_pool_and_recomputes_inactive() -> None:
    index = _index()
    company = _company(
        "exit",
        cik_number=4,
        monitoring_status=Company.MonitoringStatus.ACTIVE,
    )
    listing = _listing(company)
    _membership(listing=listing, effective_from=date(2026, 1, 1), effective_to=AS_OF)
    sync_run = _sync_run("exit")

    prior = _compose(as_of=AS_OF - timedelta(days=1))
    current = _compose()
    result = recompute_monitoring_pool_composition(
        composition=current,
        sync_run=sync_run,
    )

    company.refresh_from_db()
    assert prior.selection.members[0].company_id == company.pk
    assert current.selection.members == ()
    assert company.pk in current.affected_company_ids
    assert result.changed_company_ids == (company.pk,)
    assert company.monitoring_status == Company.MonitoringStatus.INACTIVE
    assert index.code in prior.selection.enabled_index_codes
    assert index.code in current.selection.enabled_index_codes


@pytest.mark.django_db
def test_watchlist_removal_recomputes_last_pool_member_inactive() -> None:
    _index()
    user = _user("removal")
    company = _company("removal", cik_number=5)
    _listing(company)
    item = WatchlistItem.objects.create(user=user, company=company)
    sync_run = _sync_run("removal")

    initial = _compose()
    initial_result = recompute_monitoring_pool_composition(
        composition=initial,
        sync_run=sync_run,
    )
    item.is_active = False
    item.deactivated_at = timezone.now()
    item.save(update_fields=("is_active", "deactivated_at", "updated_at"))
    removed = _compose()
    removed_result = recompute_monitoring_pool_composition(
        composition=removed,
        sync_run=sync_run,
    )

    company.refresh_from_db()
    assert initial_result.changed_company_ids == (company.pk,)
    assert removed.selection.members == ()
    assert company.pk in removed.affected_company_ids
    assert removed_result.changed_company_ids == (company.pk,)
    assert company.monitoring_status == Company.MonitoringStatus.INACTIVE


@pytest.mark.django_db
def test_identity_resolution_recomputes_previously_pending_company() -> None:
    _index()
    user = _user("identity")
    company = _company("identity")
    WatchlistItem.objects.create(user=user, company=company)
    sync_run = _sync_run("identity")

    pending_composition = _compose()
    pending_result = recompute_monitoring_pool_composition(
        composition=pending_composition,
        sync_run=sync_run,
    )
    company.cik = "0000000006"
    company.save(update_fields=("cik", "updated_at"))
    resolved_composition = _compose()
    resolved_result = recompute_monitoring_pool_composition(
        composition=resolved_composition,
        sync_run=sync_run,
    )

    company.refresh_from_db()
    assert pending_composition.selection.members[0].basis == [{"source": "watchlist"}]
    assert pending_result.unchanged_company_ids == (company.pk,)
    assert company.pk in resolved_composition.affected_company_ids
    assert resolved_result.changed_company_ids == (company.pk,)
    assert company.monitoring_status == Company.MonitoringStatus.ACTIVE


@pytest.mark.django_db
def test_recompute_results_cover_each_affected_company_once() -> None:
    _index()
    user = _user("once")
    first = _company("once-first", cik_number=7)
    second = _company("once-second", cik_number=8)
    _listing(first)
    _listing(second)
    WatchlistItem.objects.create(user=user, company=first)
    WatchlistItem.objects.create(user=user, company=second)
    composition = _compose()
    sync_run = _sync_run("once")
    result = recompute_monitoring_pool_composition(
        composition=composition,
        sync_run=sync_run,
    )

    assert composition.affected_company_ids == tuple(sorted((first.pk, second.pk), key=str))
    assert result.recomputed_company_ids == composition.affected_company_ids
    assert len(result.recomputed_company_ids) == len(set(result.recomputed_company_ids))
