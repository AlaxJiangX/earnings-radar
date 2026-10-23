"""Shared factories for Stage 5.2 page tests."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from django.test import Client

from accounts.models import User
from companies.models import Company, SecurityListing
from earnings.models import EarningsDateChange, EarningsEvent
from filings.models import Filing
from indexes.models import IndexChangeEvent, IndexChangeLeg, MarketIndex
from tests.earnings.helpers import make_company, make_data_change, make_event
from watchlists.models import WatchlistItem
from watchlists.services import add_watchlist_item

AS_OF = date(2026, 10, 20)


def make_user(suffix: str) -> User:
    return User.objects.create_user(
        email=f"pages-{suffix}-{uuid.uuid4().hex[:8]}@example.com",
        password="fixture-only-password",
    )


def company_with_listing(
    ticker: str,
    *,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> tuple[Company, SecurityListing]:
    company = make_company(ticker)
    listing = SecurityListing.objects.create(
        company=company,
        ticker=ticker,
        exchange="NASDAQ",
        security_name=f"{company.display_name} {ticker}",
        security_type="common_stock",
        effective_from=effective_from,
        effective_to=effective_to,
        is_primary=True,
    )
    return company, listing


def add_item(
    user: User,
    company: Company,
    *,
    priority_level: str = WatchlistItem.PriorityLevel.NORMAL,
) -> WatchlistItem:
    result = add_watchlist_item(
        user=user,
        company_id=company.pk,
        as_of=AS_OF,
        priority_level=priority_level,
        reason="page test setup",
        request_id=uuid.uuid4().hex,
    )
    return result.item


def scheduled_event(
    company: Company,
    *,
    release_date: date,
    status: str = "scheduled_estimated",
    period_end_date: date = date(2026, 6, 30),
) -> EarningsEvent:
    if status == "released":
        overrides: dict[str, object] = {
            "earnings_release_date": release_date,
            "earnings_release_precision": "date_only",
        }
    elif status == "scheduled_estimated":
        overrides = {
            "estimated_release_date": release_date,
            "estimated_release_precision": "date_only",
        }
    else:
        overrides = {
            "confirmed_release_date": release_date,
            "confirmed_release_precision": "date_only",
        }
    return make_event(
        company=company,
        period_end_date=period_end_date,
        period_type="Q2",
        status=status,
        **overrides,
    )


def date_only_change(
    event: EarningsEvent,
    *,
    old_date: date,
    new_date: date,
    detected_at: datetime,
    suffix: str = "pages",
) -> EarningsDateChange:
    result = make_data_change(
        event=event,
        old_value={
            "kind": "date",
            "precision": "date_only",
            "value": old_date.isoformat(),
        },
        new_value={
            "kind": "date",
            "precision": "date_only",
            "value": new_date.isoformat(),
        },
        suffix=suffix,
    )
    assert result.change is not None
    return EarningsDateChange.objects.create(
        earnings_event=event,
        field_name="estimated_release",
        change_kind="value_change",
        old_precision="date_only",
        new_precision="date_only",
        old_date=old_date,
        new_date=new_date,
        data_change=result.change,
        detected_at=detected_at,
    )


def make_filing(company: Company, *, accepted_at: datetime) -> Filing:
    token = uuid.uuid4().int
    accession = f"{token % 10**10:010d}-26-{(token >> 40) % 10**6:06d}"
    return Filing.objects.create(
        company=company,
        accession_number=accession,
        form_type="8-K",
        accepted_at=accepted_at,
        primary_document="ex991.htm",
        filing_url="https://www.sec.gov/cgi-bin/browse-edgar/test",
    )


def make_index_change(
    company: Company,
    *,
    effective_date: date,
    listing: SecurityListing,
    action: str = "added",
) -> IndexChangeEvent:
    event = IndexChangeEvent.objects.create(
        company=company,
        effective_date=effective_date,
        status="active",
        displacement="none",
        monitoring_impact="continues",
    )
    IndexChangeLeg.objects.create(
        event=event,
        index=MarketIndex.objects.get(code="SP500"),
        security_listing=listing,
        action=action,
        effective_date=effective_date,
    )
    return event


def login_client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client
