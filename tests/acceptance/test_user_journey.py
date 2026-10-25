"""Acceptance: the key user journey, end to end with zero real I/O.

login -> add watchlist item -> fixture IR calendar sync -> earnings date
change -> notifications enqueued -> send command (fake sender) -> the
in-app page shows the notification and the captured email body is correct.

The date-change -> notification wiring point is the IR confirmation path
(ADR-026 section 3); the third-party calendar reconciliation date change
is an explicitly future supplementary wiring point, so the journey drives
the production-wired path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client

from accounts.models import User
from earnings.models import EarningsDateChange
from earnings.services import evaluate_investor_relations_observation
from earnings.services.ir_confirmation import InvestorRelationsEvaluationResult
from notifications.models import Notification
from notifications.services.delivery import send_pending_notifications
from notifications.services.email import FakeEmailSender
from tests.acceptance.helpers import AS_OF, add_membership, make_pool_company
from tests.earnings.helpers import make_event
from tests.earnings.ir_helpers import (
    ingest_ir,
    ir_item,
    ir_payload,
    make_ir_source,
    make_ir_sync_run,
)
from watchlists.services import add_watchlist_item

pytestmark = pytest.mark.django_db


def _user(suffix: str) -> User:
    return User.objects.create_user(
        email=f"journey-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-password-only",
    )


def test_login_watchlist_date_change_send_and_visibility(client: Client) -> None:
    user = _user("watcher")
    plain = _user("plain")
    company, listing = make_pool_company("journey")
    # The IR sync run scope requires the company to declare its IR page.
    company.investor_relations_url = f"https://ir.example.test/journey/{company.pk}"
    company.save(update_fields=("investor_relations_url",))
    add_membership(listing, code="SP500")
    source = make_ir_source("journey")
    event = make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        status="scheduled_confirmed",
        confirmed_release_date=date(2026, 10, 28),
        confirmed_release_precision="date_only",
    )

    # 1. login (the MVP ships admin-created accounts; public registration
    #    opens at Stage 8.4 per the roadmap alpha/beta split).
    client.force_login(user)

    # 2. add the company to the watchlist through the public service.
    item = add_watchlist_item(
        user=user,
        company_id=company.pk,
        as_of=AS_OF,
        reason="acceptance journey",
        request_id=f"journey-add-{uuid.uuid4().hex[:8]}",
    )
    assert item.item.company_id == company.pk
    assert item.created is True

    # 3. fixture calendar (IR) sync: ingest the fixture payload and run
    #    the production evaluation; the confirmed date moves 10-28 -> 10-20.
    run = make_ir_sync_run(
        company=company, source=source, request_id=f"journey-ir-{uuid.uuid4().hex[:8]}"
    )
    ingestion = ingest_ir(
        sync_run=run,
        payload=ir_payload(
            [
                ir_item(
                    company,
                    item_type="release_confirmation",
                    confirmed_release="2026-10-20",
                )
            ]
        ),
        company=company,
    )
    assert len(ingestion.observations) == 1
    evaluation: InvestorRelationsEvaluationResult = evaluate_investor_relations_observation(
        observation=ingestion.observations[0],
        sync_run=run,
    )
    assert evaluation.decision.decision_type == "confirmed_schedule"
    change = EarningsDateChange.objects.get(earnings_event=event)
    assert change.change_kind == "value_change"

    # 4. the wired date-change point enqueued notifications for both users
    #    and both channels (transactional outbox, ADR-025).
    assert Notification.objects.count() == 4  # 2 users x 2 channels
    assert {row.status for row in Notification.objects.all()} == {"pending"}

    # 5. the send command delivers every due row. The watcher's P0 rows
    #    (email + in-app) are due immediately; the B-level plain user's
    #    rows are P3 digest rows scheduled into their digest bucket
    #    (ADR-027 D2) and stay pending until then.
    sender = FakeEmailSender()
    result = send_pending_notifications(
        now=datetime(2026, 10, 24, 12, 0, tzinfo=UTC), sender=sender
    )
    assert result.sent == 2
    assert {row.status for row in Notification.objects.filter(user=user)} == {"sent"}
    plain_rows = list(Notification.objects.filter(user=plain))
    assert {row.status for row in plain_rows} == {"pending"}
    assert all(row.digest_bucket is not None for row in plain_rows)

    # 6. the in-app page shows the notification for its owner.
    response = client.get("/notifications/")
    assert response.status_code == 200
    body = response.content.decode()
    assert company.display_name in body

    # 7. the captured email body is correct: named company, stamped with
    #    the old and new confirmed dates.
    assert len(sender.sent) == 1
    assert sender.sent[0].recipient == user.email
    assert sender.sent[0].subject == f"{company.display_name}: earnings date changed"
    assert f"Company: {company.display_name}" in sender.sent[0].body
    assert "New value: 2026-10-20" in sender.sent[0].body
    assert "Old value: 2026-10-28" in sender.sent[0].body

    # 8. rerunning the send command is a no-op (idempotent queue drain).
    out = StringIO()
    call_command("send_pending_notifications", "--dry-run", stdout=out)
    assert Notification.objects.filter(status="sent").count() == 2


def test_event_unchanged_when_ir_confirms_same_schedule() -> None:
    user = _user("confirm-only")
    company, listing = make_pool_company("confirm-only")
    company.investor_relations_url = f"https://ir.example.test/confirm-only/{company.pk}"
    company.save(update_fields=("investor_relations_url",))
    add_membership(listing, code="SP500")
    source = make_ir_source("confirm-only")
    event = make_event(
        company=company,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        status="scheduled_confirmed",
        confirmed_release_date=date(2026, 10, 20),
        confirmed_release_precision="date_only",
    )
    add_watchlist_item(
        user=user,
        company_id=company.pk,
        as_of=AS_OF,
        reason="acceptance confirm",
        request_id=f"journey-confirm-{uuid.uuid4().hex[:8]}",
    )

    run = make_ir_sync_run(
        company=company, source=source, request_id=f"journey-ir-same-{uuid.uuid4().hex[:8]}"
    )
    ingestion = ingest_ir(
        sync_run=run,
        payload=ir_payload([ir_item(company, confirmed_release="2026-10-20")]),
        company=company,
    )
    evaluate_investor_relations_observation(
        observation=ingestion.observations[0],
        sync_run=run,
    )

    event.refresh_from_db()
    assert event.confirmed_release_date == date(2026, 10, 20)
    assert EarningsDateChange.objects.count() == 0
    # ADR-025 section 5: precision-only refinements never enqueue.
    assert Notification.objects.count() == 0
