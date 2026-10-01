"""Fixture IR parser contract tests (Stage 4.5B)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime

import pytest

from earnings.ir_parsing import (
    FAILURE_DUPLICATE_SOURCE_IDENTITY,
    FAILURE_INCOMPLETE_PERIOD_IDENTITY,
    FAILURE_INVALID_CANCELLATION,
    FAILURE_INVALID_FACT,
    FAILURE_INVALID_ITEM_SHAPE,
    FAILURE_INVALID_SOURCE_IDENTITY,
    FAILURE_MISSING_COMPANY,
    FIXTURE_IR_FORMAT_VERSION,
    FIXTURE_IR_PARSER_VERSION,
    FIXTURE_IR_PROVIDER_KEY,
    FIXTURE_IR_PROVIDER_VERSION,
    FixtureInvestorRelationsParser,
    InvestorRelationsParseResult,
    InvestorRelationsPayloadError,
    derive_ir_source_event_identity,
)

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000123")


def _payload(items: list[dict[str, object]], **envelope_overrides: object) -> bytes:
    envelope: dict[str, object] = {
        "fixture_version": FIXTURE_IR_FORMAT_VERSION,
        "provider_key": FIXTURE_IR_PROVIDER_KEY,
        "provider_version": FIXTURE_IR_PROVIDER_VERSION,
        "source_key": "fixture-ir-source",
        "items": items,
    }
    envelope.update(envelope_overrides)
    return json.dumps(envelope).encode()


def _item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "company_id": str(COMPANY_ID),
        "period_end_date": "2026-09-30",
        "period_type": "Q3",
        "item_type": "release_confirmation",
        "confirmed_release": "2026-10-20",
    }
    item.update(overrides)
    return item


def _parse(
    items: list[dict[str, object]],
    **envelope_overrides: object,
) -> InvestorRelationsParseResult:
    return FixtureInvestorRelationsParser().parse(
        _payload(items, **envelope_overrides),
        provider_key=FIXTURE_IR_PROVIDER_KEY,
        provider_version=FIXTURE_IR_PROVIDER_VERSION,
    )


def test_parser_returns_date_only_and_exact_datetime_facts() -> None:
    result = _parse(
        [
            _item(
                confirmed_release={
                    "value": "2026-10-20T20:30:00Z",
                    "precision": "exact_datetime",
                }
            )
        ]
    )

    record = result.records[0]
    assert record.confirmed_release.precision == "exact_datetime"
    assert record.confirmed_release.datetime_value == datetime(2026, 10, 20, 20, 30, tzinfo=UTC)
    assert record.confirmed_release.date_value is None


def test_internal_identity_is_deterministic_and_stable() -> None:
    first = derive_ir_source_event_identity(
        source_key="fixture-ir-source",
        company_id=COMPANY_ID,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        item_type="release_confirmation",
    )
    second = derive_ir_source_event_identity(
        source_key="fixture-ir-source",
        company_id=COMPANY_ID,
        period_end_date=date(2026, 9, 30),
        period_type="Q3",
        item_type="release_confirmation",
    )

    assert first == second
    assert first.startswith("internal:ir:v1:")
    assert len(first) == len("internal:ir:v1:") + 64
    assert _parse([_item()]).records[0].source_event_identity == first


def test_provider_native_identity_is_preserved() -> None:
    result = _parse([_item(source_event_identity="native-event-1")])

    assert result.records[0].source_event_identity == "native-event-1"


def test_provider_native_identity_must_not_use_internal_namespace() -> None:
    result = _parse([_item(source_event_identity="internal:custom-1")])

    assert result.records == ()
    assert result.failures[0].reason_code == FAILURE_INVALID_SOURCE_IDENTITY


def test_missing_company_is_an_item_failure() -> None:
    item = _item()
    item.pop("company_id")
    result = _parse([item])

    assert result.failures[0].reason_code == FAILURE_MISSING_COMPANY


def test_incomplete_period_identity_is_an_item_failure() -> None:
    item = _item()
    item.pop("period_type")
    result = _parse([item])

    assert result.failures[0].reason_code == FAILURE_INCOMPLETE_PERIOD_IDENTITY


def test_invalid_item_shape_is_an_item_failure() -> None:
    item = _item()
    item.pop("confirmed_release")
    result = _parse([item])

    assert result.failures[0].reason_code == FAILURE_INVALID_ITEM_SHAPE


def test_invalid_cancellation_scope_is_an_item_failure() -> None:
    result = _parse(
        [
            _item(
                item_type="cancellation",
                cancellation={"scope": "ticker", "reason_code": "X"},
                confirmed_release=None,
            )
        ]
    )

    assert result.failures[0].reason_code == FAILURE_INVALID_CANCELLATION


def test_naive_datetime_is_an_item_failure() -> None:
    result = _parse(
        [
            _item(
                confirmed_release={
                    "value": "2026-10-20T20:30:00",
                    "precision": "exact_datetime",
                }
            )
        ]
    )

    assert result.failures[0].reason_code == FAILURE_INVALID_FACT


def test_duplicate_source_identity_isolated_to_later_item() -> None:
    result = _parse(
        [
            _item(source_event_identity="native-1", confirmed_release="2026-10-20"),
            _item(source_event_identity="native-1", confirmed_release="2026-10-21"),
        ]
    )

    assert len(result.records) == 1
    assert result.records[0].raw_position == 1
    assert result.failures[0].reason_code == FAILURE_DUPLICATE_SOURCE_IDENTITY


def test_malformed_envelope_fails_whole_payload() -> None:
    with pytest.raises(InvestorRelationsPayloadError):
        _parse([_item()], fixture_version="wrong-version")


def test_parser_version_is_fixed() -> None:
    assert FixtureInvestorRelationsParser.parser_version == FIXTURE_IR_PARSER_VERSION
