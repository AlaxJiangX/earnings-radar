"""Domain-event dispatch tests (Stage 6.2 acceptance criteria).

Covers every MVP event type (roadmap 6.2: date change, release, filing,
index add/remove/transfer, advance reminder), rerun dedup, suppression
with recorded reasons, the PD-4 suppressed->pending reactivation path,
per-user priority from the Stage 6.1 matrix, and the transactional-outbox
atomicity requirement.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from django.db import transaction

from accounts.models import User
from audit.models import DataChange
from companies.models import Company, SecurityListing
from earnings.models import EarningsEvent, EventStatus
from earnings.services import mark_earnings_released, update_earnings_schedule
from filings.models import Filing
from indexes.models import IndexChangeEvent, IndexChangeLeg, IndexMembership, MarketIndex
from notifications.models import Notification, ReminderRule, UserNotificationPreference
from notifications.services.dispatch import (
    DispatchTargetError,
    dispatch_earnings_date_change,
    dispatch_earnings_released,
    dispatch_filing,
    dispatch_index_change_event,
    enqueue_due_advance_reminders,
    upcoming_business_date,
)
from tests.earnings.filing_helpers import make_filing_with_evidence
from tests.earnings.helpers import make_company, make_event
from watchlists.services import (
    WatchlistMutationResult,
    add_watchlist_item,
    set_watchlist_alerts,
)

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 10, 24)

EMAIL = ReminderRule.Channel.EMAIL
IN_APP = ReminderRule.Channel.IN_APP


def _user(suffix: str) -> User:
    return User.objects.create_user(
        email=f"dispatch-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-only",
    )


def _listing(company: Company, suffix: str) -> SecurityListing:
    return SecurityListing.objects.create(
        company=company,
        ticker=f"D{suffix[:4].upper()}",
        exchange="NYSE",
        security_name=f"{company.display_name} Common",
        security_type="common_stock",
        effective_from=date(2026, 1, 1),
    )


def _index(code: str, group: str) -> MarketIndex:
    return MarketIndex.objects.get_or_create(
        code=code,
        defaults={"name": code, "index_group": group, "is_enabled": True},
    )[0]


def _member(company: Company, listing: SecurityListing, index: MarketIndex) -> None:
    IndexMembership.objects.create(
        index=index,
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )


def _watch(
    user: User, company: Company, suffix: str, *, important: bool = False
) -> WatchlistMutationResult:
    return add_watchlist_item(
        user=user,
        company_id=company.pk,
        as_of=AS_OF,
        reason="dispatch test",
        request_id=f"dispatch-add-{suffix}",
        priority_level="important" if important else "normal",
    )


def _notifications(**filters: object) -> list[Notification]:
    return list(Notification.objects.filter(**filters))


def _status_pairs(user: User) -> set[tuple[str, str]]:
    return {
        (notification.channel, notification.status) for notification in _notifications(user=user)
    }


class FixtureBundle:
    """Owner (A-level watchlist) + plain user + LARGE-index company."""

    def __init__(self, suffix: str) -> None:
        self.owner = _user(f"owner-{suffix}")
        self.plain = _user(f"plain-{suffix}")
        self.company = make_company(f"dispatch-{suffix}")
        self.listing = _listing(self.company, suffix)
        self.index = _index("SP500", "LARGE")
        _member(self.company, self.listing, self.index)
        _watch(self.owner, self.company, suffix)


class TestDateChanged:
    def test_value_change_dispatches_with_matrix_priorities(self) -> None:
        bundle = FixtureBundle("date-change")
        event = make_event(
            company=bundle.company,
            status="scheduled_confirmed",
            confirmed_release_date=date(2026, 11, 1),
            confirmed_release_precision="date_only",
        )
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"confirmed_release": date(2026, 11, 15)},
            actor_user=bundle.owner,
            reason="dispatch test",
            request_id="dispatch-date-change",
        )
        change = result.date_changes[0]
        assert change.change_kind == "value_change"

        outcome = dispatch_earnings_date_change(change.pk, as_of=AS_OF)

        assert len(outcome.results) == 4  # 2 users x 2 channels
        owner_rows = _notifications(user=bundle.owner)
        plain_rows = _notifications(user=bundle.plain)
        assert {row.priority for row in owner_rows} == {"P0"}  # A watchlist
        assert {row.priority for row in plain_rows} == {"P3"}  # B non-watchlist
        assert _status_pairs(bundle.owner) == {(EMAIL, "pending"), (IN_APP, "pending")}

    def test_rerun_does_not_create_a_second_row(self) -> None:
        bundle = FixtureBundle("date-dedup")
        event = make_event(
            company=bundle.company,
            status="scheduled_confirmed",
            confirmed_release_date=date(2026, 11, 1),
            confirmed_release_precision="date_only",
        )
        result = update_earnings_schedule(
            earnings_event=event,
            changes={"confirmed_release": date(2026, 11, 15)},
            actor_user=bundle.owner,
            reason="dispatch test",
            request_id="dispatch-date-dedup",
        )
        change = result.date_changes[0]

        dispatch_earnings_date_change(change.pk, as_of=AS_OF)
        second = dispatch_earnings_date_change(change.pk, as_of=AS_OF)

        assert second.created == 0
        assert Notification.objects.count() == 4

    def test_precision_refinement_is_not_dispatched(self) -> None:
        bundle = FixtureBundle("date-refine")
        event = make_event(
            company=bundle.company,
            status="scheduled_confirmed",
            confirmed_release_date=date(2026, 11, 1),
            confirmed_release_precision="date_only",
        )
        result = update_earnings_schedule(
            earnings_event=event,
            changes={
                "confirmed_release": {
                    "precision": "exact_datetime",
                    "value": datetime(2026, 11, 1, 21, 0, tzinfo=UTC),
                }
            },
            actor_user=bundle.owner,
            reason="dispatch test",
            request_id="dispatch-date-refine",
        )
        assert result.date_changes[0].change_kind == "precision_refinement"

        outcome = dispatch_earnings_date_change(result.date_changes[0].pk, as_of=AS_OF)

        assert outcome.results == ()
        assert Notification.objects.count() == 0


class TestEarningsReleased:
    def _release(self, bundle: FixtureBundle) -> tuple[EarningsEvent, DataChange]:
        event = make_event(
            company=bundle.company,
            status="scheduled_confirmed",
            confirmed_release_date=date(2026, 11, 1),
            confirmed_release_precision="date_only",
        )
        write = mark_earnings_released(
            earnings_event=event,
            actor_user=bundle.owner,
            reason="dispatch test",
            request_id="dispatch-release",
        )
        assert write.changed is True
        assert write.data_change is not None
        data_change = write.data_change.change
        assert data_change is not None
        return event, data_change

    def test_release_dispatches_p1_for_watchlist_and_large_index(self) -> None:
        bundle = FixtureBundle("release")
        event, data_change = self._release(bundle)

        outcome = dispatch_earnings_released(event.pk, data_change.pk, as_of=AS_OF)

        assert outcome.created == 4
        owner_rows = _notifications(user=bundle.owner)
        plain_rows = _notifications(user=bundle.plain)
        assert {row.priority for row in owner_rows} == {"P1"}  # A release
        assert {row.priority for row in plain_rows} == {"P1"}  # B release
        assert {row.event_version for row in owner_rows} == {str(data_change.pk)}

    def test_release_requires_released_status(self) -> None:
        bundle = FixtureBundle("release-guard")
        event = make_event(company=bundle.company, status="scheduled_confirmed")
        other = make_event(
            company=bundle.company,
            status="scheduled_confirmed",
            period_end_date=date(2026, 6, 30),
        )
        write = mark_earnings_released(
            earnings_event=other,
            actor_user=bundle.owner,
            reason="dispatch test",
            request_id="dispatch-release-guard",
        )
        assert write.data_change is not None
        guard_change = write.data_change.change
        assert guard_change is not None

        with pytest.raises(DispatchTargetError):
            dispatch_earnings_released(event.pk, guard_change.pk, as_of=AS_OF)


class TestFiling:
    def test_filing_dispatches_with_source_url(self) -> None:
        bundle = FixtureBundle("filing")
        filing = make_filing_with_evidence(company=bundle.company, suffix="dispatch")

        outcome = dispatch_filing(filing.pk, as_of=AS_OF)

        assert outcome.created == 4
        owner_rows = _notifications(user=bundle.owner)
        plain_rows = _notifications(user=bundle.plain)
        assert {row.priority for row in owner_rows} == {"P0"}
        assert {row.priority for row in plain_rows} == {"P3"}
        assert {row.source_url for row in owner_rows} == {filing.filing_url}
        assert {row.event_version for row in plain_rows} == {filing.accession_number}

    def test_filing_rerun_is_idempotent(self) -> None:
        bundle = FixtureBundle("filing-dedup")
        filing = make_filing_with_evidence(company=bundle.company, suffix="dispatch-dedup")

        dispatch_filing(filing.pk, as_of=AS_OF)
        second = dispatch_filing(filing.pk, as_of=AS_OF)

        assert second.created == 0
        assert Notification.objects.count() == 4


class TestIndexChangeEvent:
    def _change_event(
        self,
        company: Company,
        listing: SecurityListing,
        legs: list[tuple[str, MarketIndex]],
        *,
        impact: str = IndexChangeEvent.MonitoringImpact.CONTINUES,
        effective_date: date = date(2026, 10, 24),
    ) -> IndexChangeEvent:
        event = IndexChangeEvent.objects.create(
            status=IndexChangeEvent.Status.ACTIVE,
            displacement=IndexChangeEvent.Displacement.NONE,
            monitoring_impact=impact,
            company=company,
            effective_date=effective_date,
        )
        for action, index in legs:
            IndexChangeLeg.objects.create(
                event=event,
                index=index,
                security_listing=listing,
                action=action,
                effective_date=effective_date,
            )
        return event

    def test_added_leg_large_index_is_p1_for_everyone(self) -> None:
        bundle = FixtureBundle("index-add")
        nasdaq = _index("NASDAQ100", "LARGE")
        change_event = self._change_event(
            bundle.company,
            bundle.listing,
            [("added", nasdaq)],
            impact=IndexChangeEvent.MonitoringImpact.CONTINUES,
        )

        outcome = dispatch_index_change_event(change_event.pk, as_of=AS_OF)

        assert outcome.created == 4
        assert {row.event_type for row in Notification.objects.all()} == {"index_added"}
        assert {row.priority for row in Notification.objects.all()} == {"P1"}

    def test_added_leg_small_index_is_p3_even_for_new_entrant(self) -> None:
        # Company has no membership yet; the leg itself classifies it SMALL.
        plain = _user("index-small-plain")
        company = make_company("index-small")
        listing = _listing(company, "small")
        russell = _index("RUSSELL2000", "SMALL")
        change_event = self._change_event(
            company,
            listing,
            [("added", russell)],
            impact=IndexChangeEvent.MonitoringImpact.ENTERS_BASE_POOL,
        )

        dispatch_index_change_event(change_event.pk, as_of=AS_OF)

        rows = _notifications(user=plain)
        assert rows
        assert {row.priority for row in rows} == {"P3"}

    def test_mixed_legs_dispatch_transfer_only(self) -> None:
        bundle = FixtureBundle("transfer")
        russell = _index("RUSSELL2000", "SMALL")
        # Company is currently only in RUSSELL2000, moving up to SP500.
        IndexMembership.objects.filter(security_listing=bundle.listing).delete()
        _member(bundle.company, bundle.listing, russell)
        change_event = self._change_event(
            bundle.company,
            bundle.listing,
            [("removed", russell), ("added", bundle.index)],
            impact=IndexChangeEvent.MonitoringImpact.CONTINUES,
        )

        dispatch_index_change_event(change_event.pk, as_of=AS_OF)

        assert {row.event_type for row in Notification.objects.all()} == {"index_transferred"}
        owner_rows = _notifications(user=bundle.owner)
        plain_rows = _notifications(user=bundle.plain)
        assert {row.priority for row in owner_rows} == {"P0"}
        assert {row.priority for row in plain_rows} == {"P1"}  # via ADDED leg group

    def test_removal_from_last_index_also_dispatches_full_exit(self) -> None:
        owner = _user("exit-owner")
        plain = _user("exit-plain")
        company = make_company("exit")
        listing = _listing(company, "exit")
        russell = _index("RUSSELL2000", "SMALL")
        # Membership is ended as of the removal date (half-open interval),
        # so as-of queries no longer see the company in the pool.
        IndexMembership.objects.create(
            index=russell,
            security_listing=listing,
            status=IndexMembership.Status.ENDED,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 10, 24),
        )
        _watch(owner, company, "exit")
        change_event = self._change_event(
            company,
            listing,
            [("removed", russell)],
            impact=IndexChangeEvent.MonitoringImpact.EXITS_BASE_POOL,
        )

        dispatch_index_change_event(change_event.pk, as_of=AS_OF)

        owner_types = {row.event_type: row.priority for row in _notifications(user=owner)}
        assert owner_types["full_exit"] == "P0"
        # The company left the pool: plain users get the exit fact at P3 but
        # no index_removed row (out-of-pool skip).
        plain_types = {row.event_type: row.priority for row in _notifications(user=plain)}
        assert plain_types == {"full_exit": "P3"}

    def test_cancelled_event_does_not_dispatch(self) -> None:
        bundle = FixtureBundle("index-cancel")
        nasdaq = _index("NASDAQ100", "LARGE")
        change_event = self._change_event(
            bundle.company,
            bundle.listing,
            [("added", nasdaq)],
        )
        change_event.status = IndexChangeEvent.Status.CANCELLED
        change_event.save(update_fields=("status", "updated_at"))

        outcome = dispatch_index_change_event(change_event.pk, as_of=AS_OF)

        assert outcome.results == ()
        assert Notification.objects.count() == 0


class TestSuppressionAndReactivation:
    def test_company_alerts_gate_suppresses_and_reactivates(self) -> None:
        """PD-4 enqueue-side contract: suppressed -> pending on re-dispatch."""

        bundle = FixtureBundle("gate")
        filing = make_filing_with_evidence(company=bundle.company, suffix="gate")
        result = _watch(bundle.owner, bundle.company, "gate")
        set_watchlist_alerts(
            user=bundle.owner,
            item_id=result.item.pk,
            alerts_enabled=False,
            reason="dispatch test",
            request_id="dispatch-gate-off",
        )

        dispatch_filing(filing.pk, as_of=AS_OF)
        owner_rows = _notifications(user=bundle.owner)
        assert _status_pairs(bundle.owner) == {
            (EMAIL, "suppressed"),
            (IN_APP, "suppressed"),
        }
        assert {row.suppressed_reason for row in owner_rows} == {"company_alerts_disabled"}

        set_watchlist_alerts(
            user=bundle.owner,
            item_id=result.item.pk,
            alerts_enabled=True,
            reason="dispatch test",
            request_id="dispatch-gate-on",
        )
        outcome = dispatch_filing(filing.pk, as_of=AS_OF)

        assert outcome.reactivated == 2
        assert Notification.objects.filter(user=bundle.owner).count() == 2
        assert _status_pairs(bundle.owner) == {(EMAIL, "pending"), (IN_APP, "pending")}

    def test_disabled_channel_records_reason_per_channel(self) -> None:
        bundle = FixtureBundle("channel")
        UserNotificationPreference.objects.create(
            user=bundle.owner,
            email_enabled=False,
        )
        filing = make_filing_with_evidence(company=bundle.company, suffix="channel")

        dispatch_filing(filing.pk, as_of=AS_OF)

        email_row = _notifications(user=bundle.owner, channel=EMAIL)[0]
        in_app_row = _notifications(user=bundle.owner, channel=IN_APP)[0]
        assert email_row.status == Notification.Status.SUPPRESSED
        assert email_row.suppressed_reason == "channel_disabled"
        assert in_app_row.status == Notification.Status.PENDING

    def test_disabled_rule_records_reason(self) -> None:
        bundle = FixtureBundle("rule")
        ReminderRule.objects.create(
            user=bundle.owner,
            event_type=ReminderRule.EventType.FILING,
            channel=EMAIL,
            lead_time_minutes=0,
            is_enabled=False,
        )
        filing = make_filing_with_evidence(company=bundle.company, suffix="rule")

        dispatch_filing(filing.pk, as_of=AS_OF)

        email_row = _notifications(user=bundle.owner, channel=EMAIL)[0]
        assert email_row.status == Notification.Status.SUPPRESSED
        assert email_row.suppressed_reason == "rule_disabled"
        assert email_row.reminder_rule_id is not None

    def test_other_users_rules_do_not_leak(self) -> None:
        bundle = FixtureBundle("isolation")
        stranger = _user("isolation-stranger")
        ReminderRule.objects.create(
            user=stranger,
            event_type=ReminderRule.EventType.FILING,
            channel=EMAIL,
            lead_time_minutes=0,
            is_enabled=False,
        )
        filing = make_filing_with_evidence(company=bundle.company, suffix="isolation")

        dispatch_filing(filing.pk, as_of=AS_OF)

        owner_statuses = _status_pairs(bundle.owner)
        assert owner_statuses == {(EMAIL, "pending"), (IN_APP, "pending")}
        stranger_email = _notifications(user=stranger, channel=EMAIL)[0]
        assert stranger_email.status == Notification.Status.SUPPRESSED


class TestTransactionalOutbox:
    def test_rollback_of_domain_change_removes_notifications(self) -> None:
        bundle = FixtureBundle("atomic")

        with pytest.raises(RuntimeError, match="boom"), transaction.atomic():
            filing = make_filing_with_evidence(company=bundle.company, suffix="atomic")
            dispatch_filing(filing.pk, as_of=AS_OF)
            raise RuntimeError("boom")

        assert Notification.objects.count() == 0
        assert Filing.objects.count() == 0


class TestAdvanceReminderScan:
    def _scheduled_event(
        self,
        company: Company,
        *,
        market_date: date,
        days_ahead: int,
        confirmed: bool = True,
    ) -> EarningsEvent:
        release_date = market_date + timedelta(days=days_ahead)
        if confirmed:
            return make_event(
                company=company,
                status="scheduled_confirmed",
                confirmed_release_date=release_date,
                confirmed_release_precision="date_only",
            )
        return make_event(
            company=company,
            status="scheduled_estimated",
            estimated_release_date=release_date,
            estimated_release_precision="date_only",
        )

    def test_due_one_day_before_enqueues_with_matrix_priorities(self) -> None:
        bundle = FixtureBundle("upcoming")
        market_date = date(2026, 11, 3)
        self._scheduled_event(bundle.company, market_date=market_date, days_ahead=1)

        outcome = enqueue_due_advance_reminders(market_date=market_date)

        assert outcome.created == 4
        owner_rows = _notifications(user=bundle.owner)
        plain_rows = _notifications(user=bundle.plain)
        assert {row.priority for row in owner_rows} == {"P1"}  # A, 24h band
        assert {row.priority for row in plain_rows} == {"P2"}  # B, 24h band
        assert {row.event_type for row in owner_rows} == {"earnings_upcoming"}

    def test_not_due_events_are_skipped(self) -> None:
        bundle = FixtureBundle("upcoming-skip")
        market_date = date(2026, 11, 3)
        self._scheduled_event(bundle.company, market_date=market_date, days_ahead=2)

        outcome = enqueue_due_advance_reminders(market_date=market_date)

        assert outcome.results == ()
        assert Notification.objects.count() == 0

    def test_scan_rerun_is_idempotent(self) -> None:
        bundle = FixtureBundle("upcoming-dedup")
        market_date = date(2026, 11, 3)
        self._scheduled_event(bundle.company, market_date=market_date, days_ahead=1)

        first = enqueue_due_advance_reminders(market_date=market_date)
        second = enqueue_due_advance_reminders(market_date=market_date)

        assert first.created == 4
        assert second.created == 0
        assert Notification.objects.count() == 4

    def test_estimated_date_downgrades_only_non_watchlist(self) -> None:
        bundle = FixtureBundle("upcoming-estimated")
        market_date = date(2026, 11, 3)
        self._scheduled_event(
            bundle.company, market_date=market_date, days_ahead=1, confirmed=False
        )

        enqueue_due_advance_reminders(market_date=market_date)

        assert {row.priority for row in _notifications(user=bundle.owner)} == {"P1"}
        assert {row.priority for row in _notifications(user=bundle.plain)} == {"P3"}

    def test_exact_datetime_uses_market_natural_day(self) -> None:
        bundle = FixtureBundle("upcoming-exact")
        # 2026-11-04 01:00 UTC is 2026-11-03 21:00 in New York: the business
        # date is 11-03, so the reminder is due on 11-02 (market date).
        event = make_event(
            company=bundle.company,
            status="scheduled_confirmed",
            confirmed_release_at=datetime(2026, 11, 4, 1, 0, tzinfo=UTC),
            confirmed_release_precision="exact_datetime",
        )

        schedule = upcoming_business_date(event)
        assert schedule == (date(2026, 11, 3), True)

        outcome = enqueue_due_advance_reminders(market_date=date(2026, 11, 2))
        assert outcome.created == 4

    def test_multi_lead_rule_gets_its_own_occurrence(self) -> None:
        bundle = FixtureBundle("upcoming-multi-lead")
        market_date = date(2026, 11, 3)
        ReminderRule.objects.create(
            user=bundle.owner,
            event_type=ReminderRule.EventType.EARNINGS_UPCOMING,
            channel=EMAIL,
            lead_time_minutes=2880,
        )
        self._scheduled_event(bundle.company, market_date=market_date, days_ahead=2)

        enqueue_due_advance_reminders(market_date=market_date)

        owner_rows = _notifications(user=bundle.owner, channel=EMAIL)
        assert len(owner_rows) == 1  # only the 2-day lead rule fires
        assert owner_rows[0].priority == "P1"  # A, 2-3 day band

    def test_candidate_events_are_excluded(self) -> None:
        bundle = FixtureBundle("upcoming-candidate")
        market_date = date(2026, 11, 3)
        make_event(
            company=bundle.company,
            period_type=None,
            status="scheduled_estimated",
            estimated_release_date=market_date + timedelta(days=1),
            estimated_release_precision="date_only",
        )

        outcome = enqueue_due_advance_reminders(market_date=market_date)

        assert outcome.results == ()

    def test_released_events_are_excluded(self) -> None:
        bundle = FixtureBundle("upcoming-released")
        market_date = date(2026, 11, 3)
        event = self._scheduled_event(bundle.company, market_date=market_date, days_ahead=1)
        event.status = EventStatus.RELEASED
        event.save(update_fields=("status", "updated_at"))

        outcome = enqueue_due_advance_reminders(market_date=market_date)

        assert outcome.results == ()
