"""Shared fixture builders for the Stage 7.3 end-to-end acceptance tests.

Thin wrappers over the per-stage fixture factories; each acceptance test
module composes the production service functions exactly as the wired
pipelines do (ADR-026), with zero real network and zero real email.
"""

from __future__ import annotations

from datetime import date

from companies.models import Company, SecurityListing
from indexes.models import IndexMembership, MarketIndex
from tests.earnings.helpers import make_company

AS_OF = date(2026, 10, 24)


def make_pool_company(suffix: str, *, ticker_prefix: str = "AC") -> tuple[Company, SecurityListing]:
    """Create a Company with one effective listing (the pool join key)."""

    company = make_company(suffix)
    listing = SecurityListing.objects.create(
        company=company,
        ticker=f"{ticker_prefix}{company.pk.hex[:3].upper()}",
        exchange="NYSE",
        security_name=f"{company.display_name} Common",
        security_type="common_stock",
        effective_from=date(2026, 1, 1),
    )
    return company, listing


def add_membership(listing: SecurityListing, *, code: str = "SP500") -> MarketIndex:
    """Attach an active membership to a seeded built-in index."""

    index, _created = MarketIndex.objects.get_or_create(
        code=code,
        defaults={
            "name": {
                "SP500": "S&P 500",
                "NASDAQ100": "Nasdaq 100",
                "DJIA": "Dow Jones Industrial Average",
                "RUSSELL2000": "Russell 2000",
            }.get(code, code),
            "index_group": "LARGE" if code != "RUSSELL2000" else "SMALL",
            "is_enabled": True,
        },
    )
    IndexMembership.objects.create(
        index=index,
        security_listing=listing,
        status=IndexMembership.Status.ACTIVE,
        effective_from=date(2026, 1, 1),
    )
    return index
