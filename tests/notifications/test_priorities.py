"""Deterministic S/A/B/C and P0-P3 computation tests (PRD §9, ADR-024)."""

from __future__ import annotations

from typing import Any

import pytest

from notifications.models import ReminderRule
from notifications.services.priorities import (
    CompanyOutsideMonitoringPool,
    CompanyPriorityLevel,
    EventPriority,
    InvalidPriorityFacts,
    resolve_company_priority_level,
    resolve_event_priority,
)

ALL_LEVELS = tuple(CompanyPriorityLevel)


class TestCompanyPriorityLevel:
    def test_important_watchlist_is_s(self) -> None:
        assert (
            resolve_company_priority_level(
                watchlist_priority_level="important",
                in_large_index=False,
                in_small_index=False,
            )
            is CompanyPriorityLevel.S
        )

    def test_normal_watchlist_is_a(self) -> None:
        assert (
            resolve_company_priority_level(
                watchlist_priority_level="normal",
                in_large_index=True,
                in_small_index=True,
            )
            is CompanyPriorityLevel.A
        )

    def test_non_watchlist_large_index_is_b(self) -> None:
        assert (
            resolve_company_priority_level(
                watchlist_priority_level=None,
                in_large_index=True,
                in_small_index=True,
            )
            is CompanyPriorityLevel.B
        )

    def test_non_watchlist_small_index_is_c(self) -> None:
        assert (
            resolve_company_priority_level(
                watchlist_priority_level=None,
                in_large_index=False,
                in_small_index=True,
            )
            is CompanyPriorityLevel.C
        )

    def test_company_outside_monitoring_pool_is_rejected(self) -> None:
        with pytest.raises(CompanyOutsideMonitoringPool):
            resolve_company_priority_level(
                watchlist_priority_level=None,
                in_large_index=False,
                in_small_index=False,
            )

    def test_invalid_watchlist_level_is_rejected(self) -> None:
        with pytest.raises(InvalidPriorityFacts):
            resolve_company_priority_level(
                watchlist_priority_level="urgent",
                in_large_index=False,
                in_small_index=False,
            )


@pytest.mark.parametrize(
    ("event_type", "expected_by_level"),
    [
        # P0 "重点自选股财报发布"; A gap-filled per ADR-024 (普通自选重要事件实时);
        # B = P1 "大型指数...财报发布"; C falls to digest.
        ("earnings_released", "P0 P1 P1 P3"),
        # P0 "自选股日期变化" covers both watchlist levels.
        ("date_changed", "P0 P0 P3 P3"),
        # P0 "自选股 SEC 文件" covers both watchlist levels.
        ("filing", "P0 P0 P3 P3"),
        # P0 "自选股指数偏移"; P1 for large-index non-watchlist transfer.
        ("index_transferred", "P0 P0 P1 P3"),
        # P0 "自选股退出全部基础指数".
        ("full_exit", "P0 P0 P3 P3"),
    ],
)
def test_fixed_event_matrix(event_type: str, expected_by_level: str) -> None:
    expected = dict(zip(ALL_LEVELS, expected_by_level.split(), strict=True))
    for level in ALL_LEVELS:
        assert (
            resolve_event_priority(company_level=level, event_type=event_type).value
            == expected[level]
        )


@pytest.mark.parametrize("event_type", ["index_added", "index_removed"])
@pytest.mark.parametrize("level", ALL_LEVELS)
def test_index_membership_change_depends_on_index_size(
    level: CompanyPriorityLevel, event_type: str
) -> None:
    # P1 "大型指数公司加入或移除" regardless of company level; SMALL stays P3.
    assert (
        resolve_event_priority(company_level=level, event_type=event_type, index_is_large=True)
        is EventPriority.P1
    )
    assert (
        resolve_event_priority(company_level=level, event_type=event_type, index_is_large=False)
        is EventPriority.P3
    )


@pytest.mark.parametrize(
    ("days_until", "s", "a", "b", "c"),
    [
        # "24 小时内" == market natural days 0 or 1 (ADR-024).
        (0, "P0", "P1", "P2", "P3"),
        (1, "P0", "P1", "P2", "P3"),
        (2, "P1", "P1", "P3", "P3"),
        (3, "P1", "P1", "P3", "P3"),
        (4, "P2", "P2", "P3", "P3"),
        (7, "P2", "P2", "P3", "P3"),
        (8, "P3", "P3", "P3", "P3"),
        (30, "P3", "P3", "P3", "P3"),
    ],
)
def test_upcoming_matrix_confirmed(days_until: int, s: str, a: str, b: str, c: str) -> None:
    expected = dict(zip(ALL_LEVELS, (s, a, b, c), strict=True))
    for level in ALL_LEVELS:
        assert (
            resolve_event_priority(
                company_level=level,
                event_type=ReminderRule.EventType.EARNINGS_UPCOMING,
                days_until=days_until,
                is_confirmed=True,
            ).value
            == expected[level]
        )


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        # §9.2 ranks user attention above reliability: watchlist levels keep
        # their band priority on estimated dates; non-watchlist drop to P3
        # ("尚未正式确认的预计日期").
        (CompanyPriorityLevel.S, "P0"),
        (CompanyPriorityLevel.A, "P1"),
        (CompanyPriorityLevel.B, "P3"),
        (CompanyPriorityLevel.C, "P3"),
    ],
)
def test_upcoming_estimated_downgrades_only_non_watchlist(
    level: CompanyPriorityLevel, expected: str
) -> None:
    assert (
        resolve_event_priority(
            company_level=level,
            event_type=ReminderRule.EventType.EARNINGS_UPCOMING,
            days_until=1,
            is_confirmed=False,
        ).value
        == expected
    )


def test_same_input_computes_stably() -> None:
    # AGENTS.md §8: identical inputs must yield identical priorities.
    cases = _all_priority_cases()
    assert len(cases) > 80  # every event type, level, band and fact flag
    first_pass = [_resolve(case) for case in cases]
    second_pass = [_resolve(case) for case in cases]
    assert first_pass == second_pass


def _all_priority_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for level in ALL_LEVELS:
        for event_type in ReminderRule.EventType.values:
            if event_type == "earnings_upcoming":
                for days_until in (0, 1, 2, 3, 4, 7, 8, 30):
                    for is_confirmed in (True, False):
                        cases.append(
                            {
                                "company_level": level,
                                "event_type": event_type,
                                "days_until": days_until,
                                "is_confirmed": is_confirmed,
                            }
                        )
            elif event_type in ("index_added", "index_removed"):
                for index_is_large in (True, False):
                    cases.append(
                        {
                            "company_level": level,
                            "event_type": event_type,
                            "index_is_large": index_is_large,
                        }
                    )
            else:
                cases.append({"company_level": level, "event_type": event_type})
    return cases


def _resolve(case: dict[str, object]) -> EventPriority:
    kwargs: Any = case
    return resolve_event_priority(**kwargs)


class TestInvalidFacts:
    def test_upcoming_requires_days_until_and_confirmation(self) -> None:
        with pytest.raises(InvalidPriorityFacts):
            resolve_event_priority(
                company_level=CompanyPriorityLevel.S,
                event_type="earnings_upcoming",
                is_confirmed=True,
            )
        with pytest.raises(InvalidPriorityFacts):
            resolve_event_priority(
                company_level=CompanyPriorityLevel.S,
                event_type="earnings_upcoming",
                days_until=1,
            )

    def test_upcoming_rejects_negative_days(self) -> None:
        with pytest.raises(InvalidPriorityFacts):
            resolve_event_priority(
                company_level=CompanyPriorityLevel.S,
                event_type="earnings_upcoming",
                days_until=-1,
                is_confirmed=True,
            )

    def test_upcoming_rejects_index_fact(self) -> None:
        with pytest.raises(InvalidPriorityFacts):
            resolve_event_priority(
                company_level=CompanyPriorityLevel.S,
                event_type="earnings_upcoming",
                days_until=1,
                is_confirmed=True,
                index_is_large=True,
            )

    def test_index_change_requires_index_fact(self) -> None:
        with pytest.raises(InvalidPriorityFacts):
            resolve_event_priority(
                company_level=CompanyPriorityLevel.B,
                event_type="index_added",
            )

    def test_fixed_events_reject_extra_facts(self) -> None:
        with pytest.raises(InvalidPriorityFacts):
            resolve_event_priority(
                company_level=CompanyPriorityLevel.S,
                event_type="earnings_released",
                days_until=1,
                is_confirmed=True,
            )

    def test_unknown_event_type_is_rejected(self) -> None:
        with pytest.raises(InvalidPriorityFacts):
            resolve_event_priority(
                company_level=CompanyPriorityLevel.S,
                event_type="market_trending",
            )

    def test_company_level_must_be_enum(self) -> None:
        with pytest.raises(InvalidPriorityFacts):
            resolve_event_priority(company_level="S", event_type="filing")  # type: ignore[arg-type]
