"""Acceptance: index-change pipeline, end to end on fixtures.

Fixture change events -> leg classification (ADR-002/ADR-006) ->
notifications for every mapping (added / removed / transferred /
full_exit) -> send command with the fake sender -> rerunning the
classification never duplicates notifications.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from django.test import Client

from accounts.models import User
from indexes.services import classify_index_change_event, record_index_change_leg
from notifications.models import Notification
from notifications.services.delivery import send_pending_notifications
from notifications.services.email import FakeEmailSender
from tests.acceptance.helpers import AS_OF, add_membership, make_pool_company
from watchlists.services import add_watchlist_item

pytestmark = pytest.mark.django_db


def _watcher(suffix: str) -> User:
    user = User.objects.create_user(
        email=f"acc-idx-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-password-only",
    )
    # One non-watchlist user keeps fan-out counts deterministic
    # (2 users x 2 channels per occurrence).
    User.objects.create_user(
        email=f"acc-idx-plain-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-password-only",
    )
    return user


def _watch(company: Any, user: User, suffix: str) -> None:
    add_watchlist_item(
        user=user,
        company_id=company.pk,
        as_of=AS_OF,
        reason="acceptance index",
        request_id=f"acc-idx-{suffix}",
    )


def test_added_leg_notifies_and_rerun_dedups() -> None:
    user = _watcher("added")
    company, listing = make_pool_company("added", ticker_prefix="IA")
    add_membership(listing, code="SP500")
    _watch(company, user, "added")

    write = record_index_change_leg(
        company=company,
        security_listing=listing,
        index=_index("SP500"),
        action="added",
        effective_date=AS_OF,
    )
    assert Notification.objects.count() == 0  # not yet classified

    classify_index_change_event(write.event)

    assert {row.event_type for row in Notification.objects.all()} == {"index_added"}
    assert Notification.objects.count() == 4  # 2 users x 2 channels

    # Rerunning the classification must not duplicate (AGENTS section 8).
    classify_index_change_event(write.event)
    assert Notification.objects.count() == 4

    result, sender = _send()
    assert result.sent == 4  # P1 rows are due immediately on both channels
    assert {row.status for row in Notification.objects.all()} == {"sent"}
    assert len(sender.sent) == 2


def test_full_exit_notifies_removed_and_exit() -> None:
    user = _watcher("exit")
    company, listing = make_pool_company("exit", ticker_prefix="IX")
    add_membership(listing, code="RUSSELL2000")
    _watch(company, user, "exit")

    write = record_index_change_leg(
        company=company,
        security_listing=listing,
        index=_index("RUSSELL2000"),
        action="removed",
        effective_date=AS_OF,
    )
    classify_index_change_event(write.event)

    write.event.refresh_from_db()
    assert write.event.monitoring_impact == "exits_base_pool"
    owner_types = {row.event_type: row.priority for row in Notification.objects.filter(user=user)}
    assert owner_types == {"index_removed": "P3", "full_exit": "P0"}
    assert Notification.objects.count() == 8  # 2 event types x 2 users x 2 channels

    classify_index_change_event(write.event)
    assert Notification.objects.count() == 8

    result, sender = _send()
    # Only the watcher's P0 full_exit rows are due now; every P3 row
    # (index_removed for both users, full_exit for the B-level user) waits
    # for its digest bucket (ADR-027 D2).
    assert result.sent == 2
    assert Notification.objects.filter(status="sent").count() == 2
    assert Notification.objects.filter(status="pending").count() == 6
    assert len(sender.sent) == 1


def test_transferred_event_notifies_once_per_event() -> None:
    user = _watcher("transfer")
    company, listing = make_pool_company("transfer", ticker_prefix="IT")
    add_membership(listing, code="RUSSELL2000")
    _watch(company, user, "transfer")

    removed = record_index_change_leg(
        company=company,
        security_listing=listing,
        index=_index("RUSSELL2000"),
        action="removed",
        effective_date=AS_OF,
    )
    added = record_index_change_leg(
        company=company,
        security_listing=listing,
        index=_index("SP500"),
        action="added",
        effective_date=AS_OF,
    )
    assert added.event.pk == removed.event.pk  # same company/date -> one event

    classify_index_change_event(added.event)

    assert {row.event_type for row in Notification.objects.all()} == {"index_transferred"}
    assert Notification.objects.count() == 4  # 2 users x 2 channels

    added.event.refresh_from_db()
    assert added.event.monitoring_impact == "continues"

    result, sender = _send()
    assert result.sent == 4  # S->P0 and B->P1 are both non-digest
    assert {row.status for row in Notification.objects.all()} == {"sent"}
    assert len(sender.sent) == 2


def test_in_app_page_lists_delivered_change_for_owner(client: Client) -> None:
    user = _watcher("page")
    company, listing = make_pool_company("page", ticker_prefix="IP")
    add_membership(listing, code="SP500")
    _watch(company, user, "page")
    write = record_index_change_leg(
        company=company,
        security_listing=listing,
        index=_index("SP500"),
        action="added",
        effective_date=AS_OF,
    )
    classify_index_change_event(write.event)
    send_pending_notifications(
        now=datetime(2026, 10, 24, 12, 0, tzinfo=UTC), sender=FakeEmailSender()
    )

    client.force_login(user)
    response = client.get("/notifications/")

    assert response.status_code == 200
    assert company.display_name in response.content.decode()


def _index(code: str) -> Any:
    from indexes.models import MarketIndex

    index, _created = MarketIndex.objects.get_or_create(
        code=code,
        defaults={
            "name": {
                "SP500": "S&P 500",
                "NASDAQ100": "Nasdaq 100",
                "DJIA": "Dow Jones Industrial Average",
                "RUSSELL2000": "Russell 2000",
            }[code],
            "index_group": "SMALL" if code == "RUSSELL2000" else "LARGE",
            "is_enabled": True,
        },
    )
    return index


def _send() -> tuple[Any, FakeEmailSender]:
    sender = FakeEmailSender()
    result = send_pending_notifications(
        now=datetime(2026, 10, 24, 12, 0, tzinfo=UTC), sender=sender
    )
    return result, sender
