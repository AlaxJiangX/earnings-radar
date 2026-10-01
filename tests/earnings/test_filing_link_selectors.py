# mypy: ignore-errors
"""Selector tests for derived has_release_filing / has_periodic_filing state."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from earnings.models import EarningsEvent, FilingEarningsLink
from earnings.selectors import (
    get_filing_earnings_state,
    get_filing_earnings_states,
)
from earnings.services.filing_links import (
    evaluate_filing_earnings_link,
    reject_filing_earnings_link,
)
from tests.earnings.filing_helpers import make_filing_with_evidence
from tests.earnings.helpers import make_company, make_event, make_user

pytestmark = pytest.mark.django_db


def _run(filing: object) -> object:
    evidence = filing.source_evidence
    assert evidence is not None
    return evaluate_filing_earnings_link(filing=filing, sync_run=evidence.sync_run)


def _release_event(company: object) -> EarningsEvent:
    return make_event(
        company=company,
        period_end_date=date(2026, 6, 30),
        period_type="Q2",
        estimated_release_date=date(2026, 6, 1),
        estimated_release_precision="date_only",
    )


def _release_filing(company: object, *, accepted_at: datetime, reported_items: str = "2.02"):
    return make_filing_with_evidence(
        company=company,
        form_type="8-K",
        accepted_at=accepted_at,
        reported_items=reported_items,
        document_types=("EX-99.1",),
    )


def test_release_yes_sets_has_release_filing() -> None:
    company = make_company("selector-release")
    event = _release_event(company)
    filing = _release_filing(
        company,
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    result = _run(filing)

    state = get_filing_earnings_state(earnings_event=event)

    assert state.has_release_filing is True
    assert state.has_periodic_filing is False
    detail = state.release_filings[0]
    assert detail.filing_id == filing.pk
    assert detail.form_type == "8-K"
    assert detail.release_filing_classification == "YES"
    assert detail.review_status == "auto"
    assert detail.current_decision_id == result.decision.pk
    assert detail.classification_rule_version == "filing-release-classification-v1"


def test_review_required_release_is_not_true() -> None:
    company = make_company("selector-review")
    event = _release_event(company)
    filing = _release_filing(
        company,
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        reported_items="",
    )
    _run(filing)

    state = get_filing_earnings_state(earnings_event=event)

    assert state.has_release_filing is False
    assert state.release_filings[0].release_filing_classification == "REVIEW_REQUIRED"


def test_no_item_202_release_is_not_true() -> None:
    company = make_company("selector-no")
    event = _release_event(company)
    filing = _release_filing(
        company,
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        reported_items="9.01",
    )
    _run(filing)

    state = get_filing_earnings_state(earnings_event=event)

    assert state.has_release_filing is False
    assert state.release_filings[0].release_filing_classification == "NO"


def test_rejected_release_link_is_excluded() -> None:
    company = make_company("selector-rejected")
    event = _release_event(company)
    filing = _release_filing(
        company,
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    _run(filing)
    actor = make_user("selector-rejected")
    reject_filing_earnings_link(
        filing=filing,
        relation_type="RELEASE_FILING",
        actor_user=actor,
        reason="Not this quarter's release.",
        request_id="selector-reject-1",
    )

    state = get_filing_earnings_state(earnings_event=event)

    assert state.has_release_filing is False
    assert state.release_filings == ()


def test_periodic_link_does_not_require_manual_confirmation() -> None:
    company = make_company("selector-periodic")
    event = make_event(
        company=company,
        period_end_date=date(2026, 3, 31),
        period_type="Q1",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 3, 31),
    )
    _run(filing)

    state = get_filing_earnings_state(earnings_event=event)

    assert state.has_periodic_filing is True
    assert state.has_release_filing is False
    assert state.periodic_filings[0].review_status == "auto"


def test_rejected_periodic_link_is_excluded() -> None:
    company = make_company("selector-periodic-rejected")
    event = make_event(
        company=company,
        period_end_date=date(2026, 3, 31),
        period_type="Q1",
    )
    filing = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 3, 31),
    )
    _run(filing)
    actor = make_user("selector-periodic-rejected")
    reject_filing_earnings_link(
        filing=filing,
        relation_type="PERIODIC_FILING",
        actor_user=actor,
        reason="Wrong filing period.",
        request_id="selector-periodic-reject-1",
    )

    state = get_filing_earnings_state(earnings_event=event)

    assert state.has_periodic_filing is False
    assert state.periodic_filings == ()


def test_release_and_periodic_state_are_independent() -> None:
    company = make_company("selector-independent")
    event = _release_event(company)
    release = _release_filing(
        company,
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    periodic = make_filing_with_evidence(
        company=company,
        form_type="10-Q",
        period_of_report=date(2026, 6, 30),
    )
    _run(release)
    _run(periodic)

    state = get_filing_earnings_state(earnings_event=event)

    assert state.has_release_filing is True
    assert state.has_periodic_filing is True
    assert len(state.release_filings) == 1
    assert len(state.periodic_filings) == 1
    assert event.status == "scheduled_estimated"
    event.refresh_from_db()
    assert event.status == "scheduled_estimated"


def test_selector_orders_details_by_accepted_at_then_filing_id() -> None:
    company = make_company("selector-order")
    event = _release_event(company)
    first = _release_filing(
        company,
        accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    second = _release_filing(
        company,
        accepted_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    _run(first)
    _run(second)

    state = get_filing_earnings_state(earnings_event=event)

    assert [detail.filing_id for detail in state.release_filings] == [
        second.pk,
        first.pk,
    ]
    links = FilingEarningsLink.objects.filter(earnings_event=event).exclude(
        review_status="rejected"
    )
    assert links.count() == 2


def test_batch_selector_returns_state_for_each_event() -> None:
    first_company = make_company("selector-batch-a")
    second_company = make_company("selector-batch-b")
    first_event = _release_event(first_company)
    second_event = _release_event(second_company)
    _run(
        _release_filing(
            first_company,
            accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
    )
    _run(
        _release_filing(
            second_company,
            accepted_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            reported_items="9.01",
        )
    )

    states = get_filing_earnings_states(earnings_event_ids=(first_event.pk, second_event.pk))

    assert states[first_event.pk].has_release_filing is True
    assert states[second_event.pk].has_release_filing is False
    assert set(states) == {first_event.pk, second_event.pk}
