"""Acceptance: the four built-in indexes are seeded by the 3.1 migration."""

from __future__ import annotations

import pytest

from indexes.models import MarketIndex

EXPECTED_INDEXES = {
    "SP500": "S&P 500",
    "NASDAQ100": "Nasdaq 100",
    "DJIA": "Dow Jones Industrial Average",
    "RUSSELL2000": "Russell 2000",
}


@pytest.mark.django_db
def test_all_four_builtin_indexes_are_seeded_and_enabled() -> None:
    seeded = {index.code: index for index in MarketIndex.objects.all()}

    assert set(seeded) == set(EXPECTED_INDEXES)
    for code, name in EXPECTED_INDEXES.items():
        assert seeded[code].name == name
        assert seeded[code].is_enabled is True
