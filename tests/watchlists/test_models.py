from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.db import IntegrityError, transaction

from accounts.models import User
from companies.models import Company
from watchlists.models import WatchlistHardDeleteError, WatchlistItem


@pytest.fixture
def owner(db: object) -> User:
    del db
    return User.objects.create_user(email="watchlist-model@example.com", password="fixture-only")


@pytest.fixture
def company(db: object) -> Company:
    del db
    return Company.objects.create(legal_name="Watchlist Model Inc", display_name="Watchlist Model")


@pytest.mark.django_db
def test_watchlist_item_enforces_unique_user_company(owner: User, company: Company) -> None:
    WatchlistItem.objects.create(user=owner, company=company)

    with pytest.raises(IntegrityError), transaction.atomic():
        WatchlistItem.objects.create(user=owner, company=company)


@pytest.mark.django_db
def test_watchlist_item_enforces_priority_and_lifecycle(owner: User, company: Company) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        WatchlistItem.objects.create(user=owner, company=company, priority_level="urgent")

    deactivated_at = datetime(2026, 10, 18, 1, 0, tzinfo=UTC)
    with pytest.raises(IntegrityError), transaction.atomic():
        WatchlistItem.objects.create(
            user=owner,
            company=company,
            is_active=True,
            deactivated_at=deactivated_at,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        WatchlistItem.objects.create(user=owner, company=company, is_active=False)


@pytest.mark.django_db
def test_watchlist_items_cannot_be_hard_deleted(owner: User, company: Company) -> None:
    item = WatchlistItem.objects.create(user=owner, company=company)

    with pytest.raises(WatchlistHardDeleteError):
        item.delete()
    with pytest.raises(WatchlistHardDeleteError):
        WatchlistItem.objects.filter(pk=item.pk).delete()

    assert WatchlistItem.objects.filter(pk=item.pk).exists()


def test_watchlist_item_declares_required_indexes() -> None:
    fields = {tuple(index.fields) for index in WatchlistItem._meta.indexes}
    assert ("user", "is_active") in fields
    assert ("company", "is_active") in fields
