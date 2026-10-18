from __future__ import annotations

import uuid

import pytest

from accounts.models import User
from companies.models import Company
from watchlists.models import WatchlistItem
from watchlists.selectors import (
    get_active_watchlist_company_ids,
    get_watchlist_items_for_user,
)


@pytest.mark.django_db
def test_watchlist_selectors_enforce_user_scope_and_canonical_global_company_set() -> None:
    first_user = User.objects.create_user(email="selector-first@example.com", password="fixture")
    second_user = User.objects.create_user(email="selector-second@example.com", password="fixture")
    first_company = Company.objects.create(
        id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        legal_name="Selector First Inc",
        display_name="Selector First",
    )
    second_company = Company.objects.create(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        legal_name="Selector Second Inc",
        display_name="Selector Second",
    )
    WatchlistItem.objects.create(user=first_user, company=first_company)
    WatchlistItem.objects.create(user=second_user, company=first_company)
    WatchlistItem.objects.create(user=second_user, company=second_company)

    assert set(get_watchlist_items_for_user(first_user).values_list("user_id", flat=True)) == {
        first_user.pk
    }
    assert get_active_watchlist_company_ids() == (second_company.pk, first_company.pk)
    assert get_active_watchlist_company_ids(company_ids=(first_company.pk,)) == (first_company.pk,)
