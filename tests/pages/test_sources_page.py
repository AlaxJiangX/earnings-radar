"""Stage 7.1 public data-sources page tests: anonymous access and content.

The page mirrors docs/data-sources.md: the MVP source inventory (which
capabilities are live vs pending) and the freshness targets, stated honestly
as internal engineering targets rather than an SLA.
"""

from __future__ import annotations

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


def test_sources_page_is_anonymously_reachable() -> None:
    response = Client().get("/sources/")
    assert response.status_code == 200


def test_sources_page_lists_sources_and_pending_licenses() -> None:
    content = Client().get("/sources/").content.decode()

    assert "SEC EDGAR" in content
    assert "Alpha Vantage" in content
    assert "Provider selection and licensing pending confirmation" in content
    assert "per-source licensing review" in content
    assert "Source and licensing pending confirmation" in content


def test_sources_page_lists_freshness_targets_honestly() -> None:
    content = Client().get("/sources/").content.decode()

    assert "SEC filing discovery" in content
    assert "Within 15 minutes" in content
    assert "Within 24 hours" in content
    assert "Within 6 hours" in content
    assert "Within 5 minutes" in content
    assert "not a service-level agreement" in content
    assert "not reported on this page yet" in content
    assert "not investment advice" in content
