from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from django.db import close_old_connections, connections

import watchlists.services as watchlist_services
from accounts.models import User
from audit.models import AuditRecord
from companies.models import Company, SecurityListing
from earnings.services import (
    EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2,
    select_monitoring_pool,
)
from indexes.models import IndexMembership, MarketIndex
from watchlists.models import WatchlistItem
from watchlists.selectors import get_active_watchlist_company_ids
from watchlists.services import (
    WatchlistCompanyNotEligible,
    WatchlistItemNotFound,
    WatchlistMutationResult,
    add_watchlist_item,
    remove_watchlist_item,
    set_watchlist_alerts,
    set_watchlist_priority,
)

AS_OF = date(2026, 10, 18)


def _user(suffix: str) -> User:
    return User.objects.create_user(
        email=f"watchlist-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-only",
    )


def _company(suffix: str) -> Company:
    return Company.objects.create(
        legal_name=f"Watchlist {suffix} Inc",
        display_name=f"Watchlist {suffix}",
    )


def _listing(
    company: Company,
    suffix: str,
    *,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> SecurityListing:
    return SecurityListing.objects.create(
        company=company,
        ticker=f"W{suffix[:5].upper()}{uuid.uuid4().hex[:5].upper()}",
        exchange="NYSE",
        security_name=f"{company.display_name} Common",
        security_type="common_stock",
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _add(
    *,
    user: User,
    company: Company,
    request_id: str,
    priority_level: str = WatchlistItem.PriorityLevel.NORMAL,
) -> WatchlistMutationResult:
    return add_watchlist_item(
        user=user,
        company_id=company.pk,
        as_of=AS_OF,
        identity_pending=False,
        reason="watchlist test",
        request_id=request_id,
        priority_level=priority_level,
    )


def _close_worker_connections() -> None:
    for database_connection in connections.all():
        database_connection.close()


@pytest.mark.django_db
def test_first_add_audits_and_recomputes_company_monitoring_status() -> None:
    owner = _user("first-add")
    company = _company("first-add")
    _listing(company, "first-add")

    result = _add(user=owner, company=company, request_id="watchlist-first-add")

    company.refresh_from_db()
    assert result.created is True
    assert result.changed is True
    assert result.item.is_active is True
    audit_record = result.audit_record
    assert audit_record is not None
    assert audit_record.target_type == AuditRecord.TargetType.WATCHLIST_ITEM
    assert audit_record.actor_user_id == owner.pk
    assert audit_record.after is not None
    assert set(audit_record.after) == {
        "company_id",
        "priority_level",
        "alerts_enabled",
        "is_active",
        "deactivated_at",
    }
    assert result.monitoring_result is not None
    assert result.monitoring_result.status_changed is True
    assert company.monitoring_status == Company.MonitoringStatus.ACTIVE
    assert company.monitoring_recalculated_at is not None


@pytest.mark.django_db
def test_active_duplicate_add_is_a_noop_without_overwriting_preferences() -> None:
    owner = _user("duplicate")
    company = _company("duplicate")
    _listing(company, "duplicate")
    first = _add(
        user=owner,
        company=company,
        request_id="watchlist-duplicate-first",
        priority_level=WatchlistItem.PriorityLevel.IMPORTANT,
    )
    audit_count = AuditRecord.objects.count()
    recalculated_at = Company.objects.get(pk=company.pk).monitoring_recalculated_at

    second = _add(user=owner, company=company, request_id="watchlist-duplicate-first")

    second.item.refresh_from_db()
    company.refresh_from_db()
    assert second.item.pk == first.item.pk
    assert second.item.priority_level == WatchlistItem.PriorityLevel.IMPORTANT
    assert second.created is False
    assert second.changed is False
    assert second.audit_record is None
    assert second.monitoring_result is None
    assert AuditRecord.objects.count() == audit_count
    assert company.monitoring_recalculated_at == recalculated_at


@pytest.mark.django_db
def test_reactivation_reuses_row_and_preserves_preferences_and_creation_time() -> None:
    owner = _user("reactivate")
    company = _company("reactivate")
    _listing(company, "reactivate")
    first = _add(
        user=owner,
        company=company,
        request_id="watchlist-reactivate-add",
        priority_level=WatchlistItem.PriorityLevel.IMPORTANT,
    )
    set_watchlist_alerts(
        user=owner,
        item_id=first.item.pk,
        alerts_enabled=False,
        reason="watchlist test",
        request_id="watchlist-reactivate-alerts",
    )
    remove_watchlist_item(
        user=owner,
        item_id=first.item.pk,
        as_of=AS_OF,
        identity_pending=False,
        reason="watchlist test",
        request_id="watchlist-reactivate-remove",
    )
    original_created_at = WatchlistItem.objects.get(pk=first.item.pk).created_at

    reactivated = _add(
        user=owner,
        company=company,
        request_id="watchlist-reactivate-again",
    )

    reactivated.item.refresh_from_db()
    assert reactivated.item.pk == first.item.pk
    assert reactivated.item.created_at == original_created_at
    assert reactivated.item.priority_level == WatchlistItem.PriorityLevel.IMPORTANT
    assert reactivated.item.alerts_enabled is False
    assert reactivated.item.is_active is True
    assert reactivated.item.deactivated_at is None
    assert reactivated.created is False
    assert reactivated.changed is True


@pytest.mark.django_db
def test_inactive_reactivation_rejects_expired_listing_without_mutating_item() -> None:
    owner = _user("reactivate-expired")
    company = _company("reactivate-expired")
    _listing(
        company,
        "reactivate-expired",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 10, 1),
    )
    before_expiry = date(2026, 9, 30)
    active = add_watchlist_item(
        user=owner,
        company_id=company.pk,
        as_of=before_expiry,
        identity_pending=False,
        reason="reactivation expired listing",
        request_id="reactivate-expired-add",
    )
    removed = remove_watchlist_item(
        user=owner,
        item_id=active.item.pk,
        as_of=before_expiry,
        identity_pending=False,
        reason="reactivation expired listing",
        request_id="reactivate-expired-remove",
    )
    before_audit_count = AuditRecord.objects.filter(
        target_type=AuditRecord.TargetType.WATCHLIST_ITEM,
        target_id=active.item.pk,
    ).count()
    deactivated_at = removed.item.deactivated_at

    with pytest.raises(WatchlistCompanyNotEligible, match="effective SecurityListing"):
        add_watchlist_item(
            user=owner,
            company_id=company.pk,
            as_of=date(2026, 10, 1),
            identity_pending=False,
            reason="reactivation expired listing",
            request_id="reactivate-expired-retry",
        )

    item = WatchlistItem.objects.get(pk=active.item.pk)
    company.refresh_from_db()
    assert item.is_active is False
    assert item.deactivated_at == deactivated_at
    assert company.monitoring_status == Company.MonitoringStatus.INACTIVE
    assert (
        AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.WATCHLIST_ITEM,
            target_id=active.item.pk,
        ).count()
        == before_audit_count
    )


@pytest.mark.django_db
@pytest.mark.parametrize("listing_state", ["missing", "future", "expired"])
def test_add_rejects_company_without_effective_listing(listing_state: str) -> None:
    owner = _user(f"ineligible-{listing_state}")
    company = _company(f"ineligible-{listing_state}")
    if listing_state == "future":
        _listing(company, listing_state, effective_from=AS_OF + timedelta(days=1))
    elif listing_state == "expired":
        _listing(company, listing_state, effective_to=AS_OF)

    with pytest.raises(WatchlistCompanyNotEligible, match="effective SecurityListing"):
        _add(
            user=owner,
            company=company,
            request_id=f"watchlist-ineligible-{listing_state}",
        )

    assert not WatchlistItem.objects.filter(user=owner, company=company).exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_add_uses_real_unique_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _user("concurrent")
    company = _company("concurrent")
    _listing(company, "concurrent")
    insert_barrier = threading.Barrier(2, timeout=10)
    insert_attempts: list[str] = []
    attempts_lock = threading.Lock()
    original_create = WatchlistItem.objects.create

    def synchronized_create(*args: object, **kwargs: object) -> WatchlistItem:
        del args
        with attempts_lock:
            insert_attempts.append("insert")
        insert_barrier.wait(timeout=10)
        return original_create(**kwargs)

    def unlocked_company(company_id: uuid.UUID) -> Company:
        return Company.objects.get(pk=company_id)

    monkeypatch.setattr(
        watchlist_services,
        "_lock_company_for_watchlist_mutation",
        unlocked_company,
    )
    monkeypatch.setattr(WatchlistItem.objects, "create", synchronized_create)

    def worker(sequence: int) -> WatchlistMutationResult:
        close_old_connections()
        try:
            return _add(
                user=owner,
                company=company,
                request_id=f"watchlist-concurrent-{sequence}",
            )
        finally:
            _close_worker_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, (1, 2)))

    assert len(insert_attempts) == 2
    assert len({result.item.pk for result in results}) == 1
    assert sum(result.created for result in results) == 1
    assert sum(result.changed for result in results) == 1
    assert WatchlistItem.objects.filter(user=owner, company=company, is_active=True).count() == 1
    target_id = results[0].item.pk
    assert (
        AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.WATCHLIST_ITEM,
            target_id=target_id,
            action=AuditRecord.Action.CREATE,
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_last_watchlist_removals_serialize_on_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_user = _user("last-remove-first")
    second_user = _user("last-remove-second")
    company = _company("last-remove-race")
    _listing(company, "last-remove-race")
    first = _add(user=first_user, company=company, request_id="last-remove-add-first")
    second = _add(user=second_user, company=company, request_id="last-remove-add-second")
    lock_barrier = threading.Barrier(2, timeout=10)
    original_lock = watchlist_services._lock_company_for_watchlist_mutation

    def synchronized_lock(company_id: uuid.UUID) -> Company:
        lock_barrier.wait(timeout=10)
        return original_lock(company_id)

    monkeypatch.setattr(
        watchlist_services,
        "_lock_company_for_watchlist_mutation",
        synchronized_lock,
    )

    def remove_worker(user: User, item_id: uuid.UUID) -> WatchlistMutationResult:
        close_old_connections()
        try:
            return remove_watchlist_item(
                user=user,
                item_id=item_id,
                as_of=AS_OF,
                identity_pending=False,
                reason="last removal race",
                request_id=f"last-remove-race-{item_id}",
            )
        finally:
            _close_worker_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: remove_worker(*args),
                ((first_user, first.item.pk), (second_user, second.item.pk)),
            )
        )

    company.refresh_from_db()
    assert WatchlistItem.objects.filter(company=company, is_active=True).count() == 0
    assert company.monitoring_status == Company.MonitoringStatus.INACTIVE
    assert all(result.changed for result in results)
    assert all(result.monitoring_result is not None for result in results)
    assert WatchlistItem.objects.get(pk=first.item.pk).is_active is False
    assert WatchlistItem.objects.get(pk=second.item.pk).is_active is False
    assert (
        AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.WATCHLIST_ITEM,
            target_id__in=(first.item.pk, second.item.pk),
            action=AuditRecord.Action.DEACTIVATE,
        ).count()
        == 2
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_add_and_remove_serialize_on_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removing_user = _user("add-remove-removing")
    adding_user = _user("add-remove-adding")
    company = _company("add-remove-race")
    _listing(company, "add-remove-race")
    existing = _add(
        user=removing_user,
        company=company,
        request_id="add-remove-existing",
    )
    lock_barrier = threading.Barrier(2, timeout=10)
    original_lock = watchlist_services._lock_company_for_watchlist_mutation

    def synchronized_lock(company_id: uuid.UUID) -> Company:
        lock_barrier.wait(timeout=10)
        return original_lock(company_id)

    monkeypatch.setattr(
        watchlist_services,
        "_lock_company_for_watchlist_mutation",
        synchronized_lock,
    )

    def remove_worker() -> WatchlistMutationResult:
        close_old_connections()
        try:
            return remove_watchlist_item(
                user=removing_user,
                item_id=existing.item.pk,
                as_of=AS_OF,
                identity_pending=False,
                reason="add remove race",
                request_id="add-remove-race-remove",
            )
        finally:
            _close_worker_connections()

    def add_worker() -> WatchlistMutationResult:
        close_old_connections()
        try:
            return _add(
                user=adding_user,
                company=company,
                request_id="add-remove-race-add",
            )
        finally:
            _close_worker_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(remove_worker), executor.submit(add_worker)]
        mutation_results = [future.result() for future in results]

    company.refresh_from_db()
    added_item = WatchlistItem.objects.get(user=adding_user, company=company)
    assert WatchlistItem.objects.filter(company=company, is_active=True).count() == 1
    assert company.monitoring_status == Company.MonitoringStatus.ACTIVE
    assert all(result.changed for result in mutation_results)
    assert added_item.is_active is True
    assert (
        AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.WATCHLIST_ITEM,
            target_id=existing.item.pk,
            action=AuditRecord.Action.DEACTIVATE,
        ).count()
        == 1
    )
    assert (
        AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.WATCHLIST_ITEM,
            target_id=added_item.pk,
            action=AuditRecord.Action.CREATE,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_remove_is_user_scoped_soft_delete_and_idempotent() -> None:
    owner = _user("remove-owner")
    other = _user("remove-other")
    company = _company("remove")
    _listing(company, "remove")
    added = _add(user=owner, company=company, request_id="watchlist-remove-add")

    with pytest.raises(WatchlistItemNotFound):
        remove_watchlist_item(
            user=other,
            item_id=added.item.pk,
            as_of=AS_OF,
            identity_pending=False,
            reason="watchlist test",
            request_id="watchlist-remove-cross-user",
        )

    removed = remove_watchlist_item(
        user=owner,
        item_id=added.item.pk,
        as_of=AS_OF,
        identity_pending=False,
        reason="watchlist test",
        request_id="watchlist-remove-owner",
    )
    audit_count = AuditRecord.objects.filter(
        target_type=AuditRecord.TargetType.WATCHLIST_ITEM,
        target_id=added.item.pk,
    ).count()
    repeated = remove_watchlist_item(
        user=owner,
        item_id=added.item.pk,
        as_of=AS_OF,
        identity_pending=False,
        reason="watchlist test",
        request_id="watchlist-remove-owner",
    )

    removed.item.refresh_from_db()
    assert removed.item.is_active is False
    assert removed.item.deactivated_at is not None
    assert removed.audit_record is not None
    assert removed.audit_record.action == AuditRecord.Action.DEACTIVATE
    assert repeated.changed is False
    assert repeated.audit_record is None
    assert repeated.monitoring_result is None
    assert (
        AuditRecord.objects.filter(
            target_type=AuditRecord.TargetType.WATCHLIST_ITEM,
            target_id=added.item.pk,
        ).count()
        == audit_count
    )


@pytest.mark.django_db
def test_preference_updates_are_user_scoped_idempotent_and_never_reactivate() -> None:
    owner = _user("preferences-owner")
    other = _user("preferences-other")
    company = _company("preferences")
    item = WatchlistItem.objects.create(
        user=owner,
        company=company,
        is_active=False,
        deactivated_at=datetime(2026, 10, 18, tzinfo=UTC),
    )

    priority = set_watchlist_priority(
        user=owner,
        item_id=item.pk,
        priority_level=WatchlistItem.PriorityLevel.IMPORTANT,
        reason="watchlist test",
        request_id="watchlist-priority",
    )
    alerts = set_watchlist_alerts(
        user=owner,
        item_id=item.pk,
        alerts_enabled=False,
        reason="watchlist test",
        request_id="watchlist-alerts",
    )
    repeated = set_watchlist_alerts(
        user=owner,
        item_id=item.pk,
        alerts_enabled=False,
        reason="watchlist test",
        request_id="watchlist-alerts-repeat",
    )
    with pytest.raises(WatchlistItemNotFound):
        set_watchlist_priority(
            user=other,
            item_id=item.pk,
            priority_level=WatchlistItem.PriorityLevel.NORMAL,
            reason="watchlist test",
            request_id="watchlist-priority-cross-user",
        )

    item.refresh_from_db()
    assert item.priority_level == WatchlistItem.PriorityLevel.IMPORTANT
    assert item.alerts_enabled is False
    assert item.is_active is False
    assert priority.monitoring_result is None
    assert alerts.monitoring_result is None
    assert repeated.changed is False
    assert repeated.audit_record is None


@pytest.mark.django_db
def test_priority_and_alert_changes_do_not_change_monitoring_pool_membership() -> None:
    owner = _user("pool-preferences")
    company = _company("pool-preferences")
    _listing(company, "pool-preferences")
    index, _created = MarketIndex.objects.get_or_create(
        code="SP500",
        defaults={
            "name": "S&P 500",
            "index_group": MarketIndex.IndexGroup.LARGE,
            "is_enabled": True,
        },
    )
    added = _add(user=owner, company=company, request_id="watchlist-pool-add")
    initial_company_ids = get_active_watchlist_company_ids()
    initial_pool = select_monitoring_pool(
        as_of=AS_OF,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2,
        enabled_index_codes=(index.code,),
        watchlist_company_ids=initial_company_ids,
    )

    set_watchlist_priority(
        user=owner,
        item_id=added.item.pk,
        priority_level=WatchlistItem.PriorityLevel.IMPORTANT,
        reason="watchlist test",
        request_id="watchlist-pool-priority",
    )
    set_watchlist_alerts(
        user=owner,
        item_id=added.item.pk,
        alerts_enabled=False,
        reason="watchlist test",
        request_id="watchlist-pool-alerts",
    )
    current_company_ids = get_active_watchlist_company_ids()
    current_pool = select_monitoring_pool(
        as_of=AS_OF,
        selector_version=EARNINGS_MONITORING_POOL_SELECTOR_VERSION_V2,
        enabled_index_codes=(index.code,),
        watchlist_company_ids=current_company_ids,
    )

    assert current_company_ids == initial_company_ids == (company.pk,)
    assert current_pool.snapshot.pk == initial_pool.snapshot.pk
    assert current_pool.monitoring_pool_hash == initial_pool.monitoring_pool_hash


@pytest.mark.django_db
def test_last_watchlist_removal_without_index_membership_makes_company_inactive() -> None:
    owner = _user("last-removal")
    company = _company("last-removal")
    _listing(company, "last-removal")
    added = _add(user=owner, company=company, request_id="watchlist-last-add")

    removed = remove_watchlist_item(
        user=owner,
        item_id=added.item.pk,
        as_of=AS_OF,
        identity_pending=False,
        reason="watchlist test",
        request_id="watchlist-last-remove",
    )

    company.refresh_from_db()
    assert removed.monitoring_result is not None
    assert company.monitoring_status == Company.MonitoringStatus.INACTIVE


@pytest.mark.django_db
def test_removal_keeps_company_active_when_enabled_index_membership_exists() -> None:
    owner = _user("indexed-removal")
    company = _company("indexed-removal")
    listing = _listing(company, "indexed-removal")
    index, _created = MarketIndex.objects.get_or_create(
        code="SP500",
        defaults={
            "name": "S&P 500",
            "index_group": MarketIndex.IndexGroup.LARGE,
            "is_enabled": True,
        },
    )
    IndexMembership.objects.create(
        index=index,
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )
    added = _add(user=owner, company=company, request_id="watchlist-indexed-add")

    remove_watchlist_item(
        user=owner,
        item_id=added.item.pk,
        as_of=AS_OF,
        identity_pending=False,
        reason="watchlist test",
        request_id="watchlist-indexed-remove",
    )

    company.refresh_from_db()
    assert company.monitoring_status == Company.MonitoringStatus.ACTIVE


@pytest.mark.django_db
def test_removal_keeps_company_active_while_another_user_still_watches() -> None:
    first_user = _user("shared-first")
    second_user = _user("shared-second")
    company = _company("shared")
    _listing(company, "shared")
    first = _add(user=first_user, company=company, request_id="watchlist-shared-first")
    _add(user=second_user, company=company, request_id="watchlist-shared-second")

    remove_watchlist_item(
        user=first_user,
        item_id=first.item.pk,
        as_of=AS_OF,
        identity_pending=False,
        reason="watchlist test",
        request_id="watchlist-shared-remove",
    )

    company.refresh_from_db()
    assert company.monitoring_status == Company.MonitoringStatus.ACTIVE


@pytest.mark.django_db
def test_add_rolls_back_item_and_audit_when_monitoring_recompute_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _user("rollback")
    company = _company("rollback")
    _listing(company, "rollback")

    def fail_recompute(**_kwargs: object) -> None:
        raise RuntimeError("forced monitoring failure")

    monkeypatch.setattr(
        watchlist_services,
        "recalculate_company_monitoring_status",
        fail_recompute,
    )

    with pytest.raises(RuntimeError, match="forced monitoring failure"):
        _add(user=owner, company=company, request_id="watchlist-rollback")

    assert not WatchlistItem.objects.filter(user=owner, company=company).exists()
    assert not AuditRecord.objects.filter(
        target_type=AuditRecord.TargetType.WATCHLIST_ITEM
    ).exists()
