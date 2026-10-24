"""Stage 6.4 delivery tests (ADR-027, roadmap 6.4 acceptance criteria).

Covers the fake-sender full flow (claim -> re-check -> deliver -> status
transition), the PD-4 send-time re-check, the retry ladder (3 attempts /
15-minute interval / permanent stop), digest bucketing across timezones and
DST, idempotency-key v2, concurrent claiming, the attempt append-only
contract, and the management command (config guard, dry run, rerun).
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, close_old_connections, connections
from django.db.models import QuerySet
from django.test import override_settings

from accounts.models import User
from companies.models import SecurityListing
from filings.models import Filing
from indexes.models import IndexMembership, MarketIndex
from notifications.models import Notification, NotificationDeliveryAttempt, ReminderRule
from notifications.services import delivery as delivery_module
from notifications.services.delivery import (
    MAX_DELIVERY_ATTEMPTS,
    RETRY_INTERVAL,
    send_pending_notifications,
)
from notifications.services.dispatch import dispatch_filing
from notifications.services.email import DeliveryOutcome, EmailSendResult, FakeEmailSender
from notifications.services.enqueue import notification_idempotency_key
from notifications.services.settings_service import (
    set_digest_time_local,
    set_event_type_enabled,
    update_notification_preference,
)
from tests.earnings.filing_helpers import make_filing_with_evidence
from tests.earnings.helpers import make_company
from watchlists.services import (
    add_watchlist_item,
    remove_watchlist_item,
    set_watchlist_alerts,
)

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 10, 24)
EMAIL = ReminderRule.Channel.EMAIL
IN_APP = ReminderRule.Channel.IN_APP

# 2026-10-26 America/New_York is EDT (UTC-4): 08:00 local == 12:00 UTC.
ENQUEUE_NOW = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)  # 06:00 EDT
BEFORE_DUE_NOW = datetime(2026, 10, 26, 11, 0, tzinfo=UTC)  # 07:00 EDT
DIGEST_DUE_NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)  # 09:00 EDT
DIGEST_BUCKET = date(2026, 10, 26)
# Far in the past so due-ness never depends on the wall clock.  2026-01-05
# America/New_York is EST (UTC-5): dispatch at 10:00 UTC (05:00 EST) buckets
# P3 rows on 2026-01-05, whose 08:00 EST digest (13:00 UTC) is due from
# PAST_SEND_NOW onward.
PAST_NOW = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
PAST_SEND_NOW = datetime(2026, 1, 5, 20, 0, tzinfo=UTC)  # 15:00 EST


def _temporary() -> EmailSendResult:
    return EmailSendResult(
        outcome=DeliveryOutcome.TEMPORARY_FAILURE, error_code="network_error", error_message="boom"
    )


def _permanent() -> EmailSendResult:
    return EmailSendResult(
        outcome=DeliveryOutcome.PERMANENT_FAILURE,
        error_code="invalid_recipient",
        error_message="550 user unknown",
    )


def _unknown() -> EmailSendResult:
    return EmailSendResult(outcome=DeliveryOutcome.UNKNOWN, error_code="unclassified")


class Bundle:
    """Owner (A-level watchlist) + plain user + LARGE-index company."""

    def __init__(self, suffix: str) -> None:
        self.owner = User.objects.create_user(
            email=f"delivery-owner-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
            password="fixture-only",
        )
        self.plain = User.objects.create_user(
            email=f"delivery-plain-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
            password="fixture-only",
        )
        self.company = make_company(f"delivery-{suffix}")
        self.listing = SecurityListing.objects.create(
            company=self.company,
            ticker=f"D{suffix[:4].upper()}",
            exchange="NYSE",
            security_name=f"{self.company.display_name} Common",
            security_type="common_stock",
            effective_from=date(2026, 1, 1),
        )
        index = MarketIndex.objects.get_or_create(
            code="SP500",
            defaults={"name": "S&P 500", "index_group": "LARGE", "is_enabled": True},
        )[0]
        IndexMembership.objects.create(
            index=index,
            security_listing=self.listing,
            status=IndexMembership.Status.ACTIVE,
            effective_from=date(2026, 1, 1),
        )
        self.owner_item = add_watchlist_item(
            user=self.owner,
            company_id=self.company.pk,
            as_of=AS_OF,
            reason="delivery test",
            request_id=f"delivery-add-{suffix}",
            priority_level="normal",
        ).item

    def filing(self, suffix: str) -> Filing:
        return make_filing_with_evidence(
            company=self.company,
            suffix=f"delivery-{suffix}",
        )

    def dispatch_filing(self, suffix: str, *, now: datetime = ENQUEUE_NOW) -> None:
        dispatch_filing(self.filing(suffix).pk, as_of=AS_OF, now=now)

    def rows(self, **filters: Any) -> QuerySet[Notification]:
        return Notification.objects.filter(**filters)


def _attempts(notification: Notification) -> list[NotificationDeliveryAttempt]:
    return list(notification.delivery_attempts.order_by("attempt_number"))


def _run(
    *, now: datetime, sender: FakeEmailSender | None = None, batch_size: int = 50
) -> tuple[Any, FakeEmailSender]:
    resolved = sender if sender is not None else FakeEmailSender()
    result = send_pending_notifications(now=now, sender=resolved, batch_size=batch_size)
    return result, resolved


# ---------------------------------------------------------------------------
# Full flow
# ---------------------------------------------------------------------------


class TestFullFlow:
    def test_owner_immediate_and_plain_digest(self) -> None:
        bundle = Bundle("flow")
        bundle.dispatch_filing("flow")

        result, sender = _run(now=DIGEST_DUE_NOW)

        owner_email = bundle.rows(user=bundle.owner, channel=EMAIL).get()
        owner_in_app = bundle.rows(user=bundle.owner, channel=IN_APP).get()
        plain_email = bundle.rows(user=bundle.plain, channel=EMAIL).get()
        plain_in_app = bundle.rows(user=bundle.plain, channel=IN_APP).get()

        assert owner_email.status == "sent"
        assert owner_email.sent_at == DIGEST_DUE_NOW
        assert owner_in_app.status == "sent"
        assert plain_email.status == "sent"
        assert plain_in_app.status == "sent"
        for row in (owner_email, owner_in_app, plain_email, plain_in_app):
            assert row.attempt_count == 1
            assert _attempts(row)[0].outcome == "accepted"

        # Owner (A -> P0) gets one individual email; the plain user (B -> P3)
        # gets one digest email covering the row.
        assert len(sender.sent) == 2
        assert bundle.company.display_name in sender.sent[0].subject
        assert "digest" in sender.sent[1].subject.lower()
        assert sender.sent[1].subject.endswith(DIGEST_BUCKET.isoformat())
        assert bundle.company.display_name in sender.sent[1].body
        assert result.sent == 4
        assert result.digest_emails == 1

    def test_email_body_contains_prd_fields_and_dual_time(self) -> None:
        bundle = Bundle("content")
        bundle.owner.timezone = "Asia/Tokyo"
        bundle.owner.save(update_fields=("timezone",))
        accepted = datetime(2026, 10, 26, 20, 30, tzinfo=UTC)  # 16:30 EDT / 01:30 JST +1d
        filing = make_filing_with_evidence(
            company=bundle.company, accepted_at=accepted, suffix="delivery-content"
        )
        dispatch_filing(filing.pk, as_of=AS_OF, now=ENQUEUE_NOW)

        _run(now=DIGEST_DUE_NOW)

        owner_email = bundle.rows(user=bundle.owner, channel=EMAIL).get()
        body = owner_email.body_snapshot or ""
        assert filing.form_type in body
        assert filing.accession_number in body
        assert filing.filing_url in body
        assert "2026-10-26 16:30 ET" in body
        assert "JST" in body  # user-local alongside the market reference
        assert owner_email.source_url == filing.filing_url

    def test_p3_rows_wait_for_digest_time(self) -> None:
        bundle = Bundle("wait")
        bundle.dispatch_filing("wait")

        result, sender = _run(now=BEFORE_DUE_NOW)

        plain_rows = bundle.rows(user=bundle.plain)
        assert {row.status for row in plain_rows} == {"pending"}
        owner_rows = bundle.rows(user=bundle.owner)
        assert {row.status for row in owner_rows} == {"sent"}
        assert len(sender.sent) == 1  # only the owner's immediate email
        assert result.digest_emails == 0

    def test_rerunning_same_input_changes_nothing(self) -> None:
        bundle = Bundle("rerun")
        bundle.dispatch_filing("rerun")

        _first, sender = _run(now=DIGEST_DUE_NOW)
        second, sender_after = _run(now=DIGEST_DUE_NOW, sender=sender)

        assert second.claimed == 0
        assert len(sender_after.sent) == 2  # no duplicate emails
        assert Notification.objects.count() == 4
        for row in Notification.objects.all():
            assert row.attempt_count == 1


# ---------------------------------------------------------------------------
# Send-time re-check (PD-4 + gates, ADR-027 D4)
# ---------------------------------------------------------------------------


class TestRecheck:
    def test_sa_origin_suppressed_after_watchlist_removal(self) -> None:
        bundle = Bundle("pd4-remove")
        bundle.dispatch_filing("pd4-remove")
        remove_watchlist_item(
            user=bundle.owner,
            item_id=bundle.owner_item.pk,
            as_of=AS_OF,
            reason="delivery test removal",
            request_id="delivery-remove-pd4",
        )

        _run(now=DIGEST_DUE_NOW)

        for row in bundle.rows(user=bundle.owner):
            assert row.status == "suppressed"
            assert row.suppressed_reason == "company_alerts_disabled"
            assert _attempts(row) == []  # suppression is not an attempt
        # The plain user's B-origin rows are untouched by the removal.
        assert {row.status for row in bundle.rows(user=bundle.plain)} == {"sent"}

    def test_sa_origin_suppressed_when_company_alerts_off(self) -> None:
        bundle = Bundle("pd4-alerts")
        bundle.dispatch_filing("pd4-alerts")
        set_watchlist_alerts(
            user=bundle.owner,
            item_id=bundle.owner_item.pk,
            alerts_enabled=False,
            reason="delivery test alerts off",
            request_id="delivery-alerts-off",
        )

        _run(now=DIGEST_DUE_NOW)

        assert {row.status for row in bundle.rows(user=bundle.owner)} == {"suppressed"}

    def test_channel_gate_suppresses_only_that_channel(self) -> None:
        bundle = Bundle("gate-channel")
        bundle.dispatch_filing("gate-channel")
        update_notification_preference(bundle.owner, email_enabled=False)

        _run(now=DIGEST_DUE_NOW)

        owner_email = bundle.rows(user=bundle.owner, channel=EMAIL).get()
        owner_in_app = bundle.rows(user=bundle.owner, channel=IN_APP).get()
        assert owner_email.status == "suppressed"
        assert owner_email.suppressed_reason == "channel_disabled"
        assert owner_in_app.status == "sent"

    def test_bc_origin_row_suppressed_when_user_later_disables_company_alerts(self) -> None:
        bundle = Bundle("gate-bc")
        bundle.dispatch_filing("gate-bc")
        # The plain user later adds the company with company alerts off.
        plain_item = add_watchlist_item(
            user=bundle.plain,
            company_id=bundle.company.pk,
            as_of=AS_OF,
            reason="delivery test late add",
            request_id="delivery-late-add",
            priority_level="normal",
        ).item
        set_watchlist_alerts(
            user=bundle.plain,
            item_id=plain_item.pk,
            alerts_enabled=False,
            reason="delivery test late alerts off",
            request_id="delivery-late-alerts-off",
        )

        _run(now=DIGEST_DUE_NOW)

        assert {row.status for row in bundle.rows(user=bundle.plain)} == {"suppressed"}

    def test_rule_changes_do_not_suppress_pending_rows(self) -> None:
        bundle = Bundle("gate-rule")
        bundle.dispatch_filing("gate-rule")
        set_event_type_enabled(bundle.owner, ReminderRule.EventType.FILING, False)

        _run(now=DIGEST_DUE_NOW)

        # D4.3: rule re-resolution is out of scope; pending rows still go out.
        assert {row.status for row in bundle.rows(user=bundle.owner)} == {"sent"}

    def test_legacy_origin_null_survives_while_still_in_pool(self) -> None:
        bundle = Bundle("legacy")
        bundle.dispatch_filing("legacy")
        # Strip origin from the owner's rows to emulate pre-6.4 rows.
        Notification.objects.filter(user=bundle.owner).update(origin_company_level=None)
        remove_watchlist_item(
            user=bundle.owner,
            item_id=bundle.owner_item.pk,
            as_of=AS_OF,
            reason="legacy test removal",
            request_id="delivery-legacy-remove",
        )
        # Company keeps its LARGE index membership -> still in the pool.

        _run(now=DIGEST_DUE_NOW)

        assert {row.status for row in bundle.rows(user=bundle.owner)} == {"sent"}

    def test_legacy_origin_null_out_of_pool_row_is_suppressed(self) -> None:
        bundle = Bundle("legacy-out")
        company = make_company("delivery-legacy-out")
        # No listing / no index membership / no watchlist -> out of pool.
        filing = make_filing_with_evidence(company=company, suffix="delivery-legacy-out")
        Notification.objects.create(
            user=bundle.plain,
            event_type=ReminderRule.EventType.FILING,
            related_object_type=Notification.RelatedObjectType.FILING,
            related_object_id=filing.pk,
            event_version=filing.accession_number,
            priority="P1",
            channel=EMAIL,
            idempotency_key=uuid.uuid4().hex,
            origin_company_level=None,
        )

        _run(now=DIGEST_DUE_NOW)

        row = bundle.rows(user=bundle.plain, channel=EMAIL, event_type="filing").get()
        assert row.status == "suppressed"
        assert row.suppressed_reason == "company_alerts_disabled"


# ---------------------------------------------------------------------------
# Retry ladder (ADR-027 D3)
# ---------------------------------------------------------------------------


class TestRetry:
    def _owner_email_row(self, bundle: Bundle) -> Notification:
        return bundle.rows(user=bundle.owner, channel=EMAIL).get()

    def test_temporary_failure_schedules_retry_in_15_minutes(self) -> None:
        bundle = Bundle("retry-1")
        bundle.dispatch_filing("retry-1")

        _run(now=DIGEST_DUE_NOW, sender=FakeEmailSender(scripted=(_temporary(),)))

        row = self._owner_email_row(bundle)
        assert row.status == "pending"
        assert row.attempt_count == 1
        assert row.next_attempt_at == DIGEST_DUE_NOW + RETRY_INTERVAL
        assert row.last_error_code == "network_error"
        assert [attempt.outcome for attempt in _attempts(row)] == ["temporary_failure"]

    def test_retry_row_not_claimed_before_next_attempt(self) -> None:
        bundle = Bundle("retry-early")
        bundle.dispatch_filing("retry-early")

        _run(now=DIGEST_DUE_NOW, sender=FakeEmailSender(scripted=(_temporary(),)))
        result, _sender = _run(now=DIGEST_DUE_NOW + timedelta(minutes=14))

        row = self._owner_email_row(bundle)
        assert result.claimed == 0
        assert row.attempt_count == 1
        assert row.status == "pending"

    def test_second_attempt_success(self) -> None:
        bundle = Bundle("retry-2")
        bundle.dispatch_filing("retry-2")

        _run(now=DIGEST_DUE_NOW, sender=FakeEmailSender(scripted=(_temporary(),)))
        _run(now=DIGEST_DUE_NOW + RETRY_INTERVAL)

        row = self._owner_email_row(bundle)
        assert row.status == "sent"
        assert row.attempt_count == 2
        assert [attempt.outcome for attempt in _attempts(row)] == [
            "temporary_failure",
            "accepted",
        ]
        assert row.last_error_code is None

    def test_three_temporary_failures_fail_permanently(self) -> None:
        bundle = Bundle("retry-3")
        bundle.dispatch_filing("retry-3")

        _run(now=DIGEST_DUE_NOW, sender=FakeEmailSender(scripted=(_temporary(),)))
        _run(
            now=DIGEST_DUE_NOW + RETRY_INTERVAL,
            sender=FakeEmailSender(scripted=(_temporary(),)),
        )
        _run(
            now=DIGEST_DUE_NOW + 2 * RETRY_INTERVAL,
            sender=FakeEmailSender(scripted=(_temporary(),)),
        )

        row = self._owner_email_row(bundle)
        assert row.status == "failed"
        assert row.attempt_count == MAX_DELIVERY_ATTEMPTS
        assert [attempt.outcome for attempt in _attempts(row)] == ["temporary_failure"] * 3
        # The terminal failure is never re-claimed.
        result, _sender = _run(now=DIGEST_DUE_NOW + 3 * RETRY_INTERVAL)
        assert result.claimed == 0

    def test_permanent_failure_stops_immediately(self) -> None:
        bundle = Bundle("retry-perm")
        bundle.dispatch_filing("retry-perm")

        _run(now=DIGEST_DUE_NOW, sender=FakeEmailSender(scripted=(_permanent(),)))

        row = self._owner_email_row(bundle)
        assert row.status == "failed"
        assert row.attempt_count == 1
        assert row.last_error_code == "invalid_recipient"
        assert [attempt.outcome for attempt in _attempts(row)] == ["permanent_failure"]

    def test_unknown_outcome_retries(self) -> None:
        bundle = Bundle("retry-unknown")
        bundle.dispatch_filing("retry-unknown")

        _run(now=DIGEST_DUE_NOW, sender=FakeEmailSender(scripted=(_unknown(),)))

        row = self._owner_email_row(bundle)
        assert row.status == "pending"
        assert [attempt.outcome for attempt in _attempts(row)] == ["unknown"]

    def test_digest_bucket_retries_together(self) -> None:
        bundle = Bundle("digest-retry")
        bundle.dispatch_filing("a")
        bundle.dispatch_filing("b")

        # Three send calls in the first run: two owner emails plus the
        # plain user's digest; all fail temporarily.
        first = FakeEmailSender(scripted=(_temporary(), _temporary(), _temporary()))
        _run(now=DIGEST_DUE_NOW, sender=first)

        plain_rows = list(bundle.rows(user=bundle.plain, channel=EMAIL))
        assert len(plain_rows) == 2
        for row in plain_rows:
            assert row.status == "pending"
            assert row.attempt_count == 1
            assert row.next_attempt_at == DIGEST_DUE_NOW + RETRY_INTERVAL

        second = FakeEmailSender()
        _run(now=DIGEST_DUE_NOW + RETRY_INTERVAL, sender=second)

        # The failed digest attempt retried the whole bucket and the
        # successful run sent exactly one digest email again.
        assert len(first.sent) == 3
        assert len(second.sent) == 3
        for row in plain_rows:
            row.refresh_from_db()
            assert row.status == "sent"
            assert row.attempt_count == 2


# ---------------------------------------------------------------------------
# Digest bucketing across timezones and DST
# ---------------------------------------------------------------------------


class TestDigestTimezones:
    def test_tokyo_user_buckets_by_local_date(self) -> None:
        bundle = Bundle("tokyo")
        bundle.plain.timezone = "Asia/Tokyo"
        bundle.plain.save(update_fields=("timezone",))
        # 10:00 UTC = 19:00 JST Oct 26 -> after 08:00 JST -> bucket Oct 27.
        bundle.dispatch_filing("tokyo", now=ENQUEUE_NOW)

        plain_email = bundle.rows(user=bundle.plain, channel=EMAIL).get()
        assert plain_email.digest_bucket == date(2026, 10, 27)

        _run(now=DIGEST_DUE_NOW)  # 22:00 JST, still Oct 26 -> not due
        plain_email.refresh_from_db()
        assert plain_email.status == "pending"

        _run(now=datetime(2026, 10, 26, 23, 0, tzinfo=UTC))  # 08:00 JST Oct 27
        plain_email.refresh_from_db()
        assert plain_email.status == "sent"

    def test_custom_digest_time_changes_bucket(self) -> None:
        bundle = Bundle("custom-time")
        set_digest_time_local(bundle.plain, time(17, 30))
        bundle.dispatch_filing("custom-time")

        plain_email = bundle.rows(user=bundle.plain, channel=EMAIL).get()
        assert plain_email.digest_bucket == DIGEST_BUCKET  # 06:00 EDT < 17:30

        _, sender = _run(now=DIGEST_DUE_NOW)  # 09:00 EDT < 17:30 -> not due
        plain_email.refresh_from_db()
        assert plain_email.status == "pending"
        assert len(sender.sent) == 1

        _run(now=datetime(2026, 10, 26, 21, 30, tzinfo=UTC))  # 17:30 EDT
        plain_email.refresh_from_db()
        assert plain_email.status == "sent"

    def test_digest_time_defaults_to_08_00_without_preference(self) -> None:
        bundle = Bundle("default-time")
        # No preference row at all for the plain user.
        bundle.dispatch_filing("default-time")

        plain_email = bundle.rows(user=bundle.plain, channel=EMAIL).get()
        assert plain_email.digest_bucket == DIGEST_BUCKET
        _, sender = _run(now=DIGEST_DUE_NOW)
        plain_email.refresh_from_db()
        assert plain_email.status == "sent"
        assert len(sender.sent) == 2

    def test_fall_back_dst_digest_due_at_est_instant(self) -> None:
        bundle = Bundle("dst-fall")
        user = bundle.plain
        user.timezone = "America/New_York"
        user.save(update_fields=("timezone",))
        filing = make_filing_with_evidence(company=bundle.company, suffix="delivery-dst-fall")
        Notification.objects.create(
            user=user,
            event_type=ReminderRule.EventType.FILING,
            related_object_type=Notification.RelatedObjectType.FILING,
            related_object_id=filing.pk,
            event_version=filing.accession_number,
            priority="P3",
            channel=EMAIL,
            idempotency_key=uuid.uuid4().hex,
            digest_bucket=date(2026, 11, 1),  # fall-back Sunday
        )

        before, _sender_before = _run(now=datetime(2026, 11, 1, 12, 59, tzinfo=UTC))
        row = bundle.rows(user=user, channel=EMAIL, priority="P3").get()
        assert row.status == "pending"
        assert before.digest_emails == 0

        after, _sender = _run(now=datetime(2026, 11, 1, 13, 0, tzinfo=UTC))  # 08:00 EST
        row.refresh_from_db()
        assert row.status == "sent"
        assert after.digest_emails == 1


# ---------------------------------------------------------------------------
# Idempotency key v2 and enqueue-time facts
# ---------------------------------------------------------------------------


class TestKeyV2AndFacts:
    def test_same_bucket_rerun_dedups_p3_rows(self) -> None:
        bundle = Bundle("key-dedup")
        filing = bundle.filing("key-dedup")
        dispatch_filing(filing.pk, as_of=AS_OF, now=ENQUEUE_NOW)
        dispatch_filing(filing.pk, as_of=AS_OF, now=ENQUEUE_NOW)  # same bucket

        assert bundle.rows(user=bundle.plain, channel=EMAIL).count() == 1
        assert bundle.rows(user=bundle.plain, channel=IN_APP).count() == 1
        assert bundle.rows(user=bundle.owner).count() == 2  # P0 keeps v1 dedup too

    def test_next_bucket_after_digest_time_creates_new_row(self) -> None:
        bundle = Bundle("key-next")
        filing = bundle.filing("key-next")
        dispatch_filing(filing.pk, as_of=AS_OF, now=ENQUEUE_NOW)
        # Re-dispatch the same occurrence after the digest time: new bucket.
        dispatch_filing(filing.pk, as_of=AS_OF, now=datetime(2026, 10, 26, 14, 0, tzinfo=UTC))

        plain_rows = bundle.rows(user=bundle.plain, channel=EMAIL)
        assert plain_rows.count() == 2
        assert {row.digest_bucket for row in plain_rows} == {
            date(2026, 10, 26),
            date(2026, 10, 27),
        }

    def test_origin_level_and_bucket_recorded(self) -> None:
        bundle = Bundle("facts")
        bundle.dispatch_filing("facts")

        for row in bundle.rows(user=bundle.owner):
            assert row.origin_company_level == "A"  # normal watchlist
            assert row.priority == "P0"
            assert row.digest_bucket is None
        for row in bundle.rows(user=bundle.plain):
            assert row.origin_company_level == "B"  # LARGE index, non-watchlist
            assert row.priority == "P3"
            assert row.digest_bucket == DIGEST_BUCKET

    def test_non_digest_keys_stay_v1_byte_identical(self) -> None:
        bundle = Bundle("key-v1")
        filing = bundle.filing("key-v1")
        dispatch_filing(filing.pk, as_of=AS_OF, now=ENQUEUE_NOW)

        row = bundle.rows(user=bundle.owner, channel=EMAIL).get()
        v1 = notification_idempotency_key(
            user_id=bundle.owner.pk,
            event_type="filing",
            channel="email",
            related_object_type="filing",
            related_object_id=filing.pk,
            event_version=filing.accession_number,
        )
        assert row.idempotency_key == v1

    def test_key_v2_function_differs_from_v1_and_is_stable(self) -> None:
        base: dict[str, Any] = dict(
            user_id=uuid.uuid4(),
            event_type="filing",
            channel="email",
            related_object_type="filing",
            related_object_id=uuid.uuid4(),
            event_version="0000000000-00-000000",
        )
        v1 = notification_idempotency_key(**base)
        v2 = notification_idempotency_key(**base, digest_bucket=date(2026, 10, 26))
        v2_again = notification_idempotency_key(**base, digest_bucket=date(2026, 10, 26))
        v2_other = notification_idempotency_key(**base, digest_bucket=date(2026, 10, 27))
        assert v1 != v2
        assert v2 == v2_again  # stable across calls
        assert v2 != v2_other  # bucket is a key dimension


# ---------------------------------------------------------------------------
# Attempt append-only contract
# ---------------------------------------------------------------------------


class TestAttemptModel:
    def test_attempt_number_unique_per_notification(self) -> None:
        bundle = Bundle("attempt-unique")
        bundle.dispatch_filing("attempt-unique")
        row = bundle.rows(user=bundle.owner, channel=EMAIL).get()
        NotificationDeliveryAttempt.objects.create(
            notification=row,
            attempt_number=1,
            started_at=DIGEST_DUE_NOW,
            finished_at=DIGEST_DUE_NOW,
            outcome="accepted",
        )

        with pytest.raises(IntegrityError):
            NotificationDeliveryAttempt.objects.create(
                notification=row,
                attempt_number=1,
                started_at=DIGEST_DUE_NOW,
                finished_at=DIGEST_DUE_NOW,
                outcome="accepted",
            )

    def test_finished_must_not_precede_started(self) -> None:
        bundle = Bundle("attempt-order")
        bundle.dispatch_filing("attempt-order")
        row = bundle.rows(user=bundle.owner, channel=EMAIL).get()

        with pytest.raises(IntegrityError):
            NotificationDeliveryAttempt.objects.create(
                notification=row,
                attempt_number=1,
                started_at=DIGEST_DUE_NOW,
                finished_at=DIGEST_DUE_NOW - timedelta(seconds=1),
                outcome="accepted",
            )


# ---------------------------------------------------------------------------
# Concurrent claiming
# ---------------------------------------------------------------------------


class TestConcurrentClaiming:
    @pytest.mark.django_db(transaction=True)
    def test_two_concurrent_senders_never_claim_the_same_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = Bundle("concurrent")
        for index in range(3):
            bundle.dispatch_filing(f"concurrent-{index}", now=PAST_NOW)

        assert Notification.objects.count() == 12  # 3 filings x 2 users x 2 channels

        # Synchronize only the first candidate scan per thread so both hit
        # the locking claim at the same moment; later iterations run free.
        start_barrier = threading.Barrier(2, timeout=15)
        original = delivery_module._due_candidate_ids
        thread_local = threading.local()

        def synchronized_ids(*, now: datetime) -> list[Any]:
            if not getattr(thread_local, "entered", False):
                thread_local.entered = True
                start_barrier.wait(timeout=15)
            return original(now=now)

        monkeypatch.setattr(delivery_module, "_due_candidate_ids", synchronized_ids)

        def worker(_sequence: int) -> dict[str, int]:
            close_old_connections()
            try:
                sender = FakeEmailSender()
                result = send_pending_notifications(now=PAST_SEND_NOW, sender=sender)
                return {
                    "claimed": result.claimed,
                    "emails": len(sender.sent),
                    "failed": result.failed,
                }
            finally:
                # close_old_connections leaves fresh (CONN_MAX_AGE) worker
                # connections open; close them so test teardown is clean.
                for database_connection in connections.all():
                    database_connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(worker, (1, 2)))

        # Every row was processed exactly once across both senders.
        assert sum(item["claimed"] for item in outcomes) == 12
        assert sum(item["failed"] for item in outcomes) == 0
        assert Notification.objects.filter(status="sent").count() == 12
        assert NotificationDeliveryAttempt.objects.count() == 12
        assert NotificationDeliveryAttempt.objects.exclude(outcome="accepted").count() == 0


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------


class TestCommand:
    @override_settings(EMAIL_HOST="", DEFAULT_FROM_EMAIL="radar@example.test")
    def test_refuses_to_run_without_email_host(self) -> None:
        bundle = Bundle("cmd-config")
        bundle.dispatch_filing("cmd-config", now=PAST_NOW)

        with pytest.raises(CommandError, match="EMAIL_HOST"):
            call_command("send_pending_notifications")

        assert {row.status for row in Notification.objects.all()} == {"pending"}
        assert NotificationDeliveryAttempt.objects.count() == 0

    def test_dry_run_leaves_no_trace(self) -> None:
        bundle = Bundle("cmd-dry")
        bundle.dispatch_filing("cmd-dry", now=PAST_NOW)

        call_command("send_pending_notifications", "--dry-run")

        assert {row.status for row in Notification.objects.all()} == {"pending"}
        assert NotificationDeliveryAttempt.objects.count() == 0
        assert Notification.objects.filter(claimed_at__isnull=False).count() == 0

    def test_normal_run_processes_rows(self) -> None:
        import notifications.management.commands.send_pending_notifications as command_module

        bundle = Bundle("cmd-run")
        # Enqueue in the past so due-ness never depends on the wall clock.
        bundle.dispatch_filing("cmd-run", now=PAST_NOW)
        with patch.object(command_module, "default_email_sender", lambda: FakeEmailSender()):
            call_command("send_pending_notifications")

        assert Notification.objects.filter(status="sent").count() == 4
