from __future__ import annotations

import ast
import http.client
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from audit.models import AuditRecord, SourceEvidence
from earnings.calendar_parsing import (
    FIXTURE_EARNINGS_CALENDAR_FORMAT_VERSION,
    FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION,
    FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
    FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
    EarningsCalendarParser,
    EarningsCalendarParserContextError,
    EarningsCalendarParseResult,
    EarningsCalendarPayloadError,
    FixtureEarningsCalendarParser,
    NormalizedEarningsCalendarRecord,
    UnsupportedEarningsCalendarIdentityError,
)
from earnings.models import (
    EarningsCalendarObservation,
    EarningsEvent,
    EarningsReconciliationDecision,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "earnings_calendar"
PARSER_MODULE_PATH = Path(__file__).resolve().parents[2] / "earnings" / "calendar_parsing.py"
FORBIDDEN_MODULE_PREFIXES = ("audit", "providers", "django.db", "django.http")
FORBIDDEN_IMPORTED_NAMES = frozenset(
    {
        "AuditRecord",
        "EarningsCalendarObservation",
        "EarningsEvent",
        "EarningsReconciliationDecision",
        "Provider",
        "SourceEvidence",
        "SyncRun",
        "derive_earnings_identity_key",
        "record_earnings_calendar_observation",
    }
)


def _parser() -> FixtureEarningsCalendarParser:
    return FixtureEarningsCalendarParser()


def _parse_fixture(name: str) -> EarningsCalendarParseResult:
    return _parser().parse(
        (FIXTURE_DIR / name).read_bytes(),
        provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
        provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
    )


def _envelope(events: object) -> dict[str, object]:
    return {
        "fixture_version": FIXTURE_EARNINGS_CALENDAR_FORMAT_VERSION,
        "provider_key": FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
        "provider_version": FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        "events": events,
    }


def _parse_document(document: object) -> EarningsCalendarParseResult:
    return _parser().parse(
        json.dumps(document).encode(),
        provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
        provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
    )


def _parse_events(events: object) -> EarningsCalendarParseResult:
    return _parse_document(_envelope(events))


def _event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {"provider_event_id": "fixture-evt-test"}
    event.update(overrides)
    return event


def _accept_parser(parser: EarningsCalendarParser) -> EarningsCalendarParser:
    return parser


def _row_counts() -> tuple[int, int, int, int, int]:
    return (
        EarningsEvent.objects.count(),
        EarningsCalendarObservation.objects.count(),
        EarningsReconciliationDecision.objects.count(),
        SourceEvidence.objects.count(),
        AuditRecord.objects.count(),
    )


def test_fixture_parser_satisfies_protocol_and_exposes_version() -> None:
    parser = _parser()

    assert isinstance(parser, EarningsCalendarParser)
    assert _accept_parser(parser) is parser
    assert parser.parser_version == FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION


def test_empty_payload_is_a_successful_zero_record_parse() -> None:
    result = _parse_fixture("empty_payload.json")

    assert result.records == ()
    assert result.provider_key == FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY
    assert result.provider_version == FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION
    assert result.parser_version == FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION


def test_complete_payload_preserves_order_and_raw_positions() -> None:
    result = _parse_fixture("complete_payload.json")

    assert [record.raw_position for record in result.records] == [1, 2]
    assert [record.provider_event_id for record in result.records] == [
        "fixture-evt-1001",
        "fixture-evt-1002",
    ]
    assert all(
        record.parser_version == FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION
        for record in result.records
    )
    assert all(isinstance(record, NormalizedEarningsCalendarRecord) for record in result.records)


def test_complete_payload_normalizes_company_and_fiscal_hints() -> None:
    first, second = _parse_fixture("complete_payload.json").records

    assert first.cik == "0000000123"
    assert first.ticker == "FAKE"
    assert first.exchange == "NASDAQ"
    assert first.provider_symbol == "FAKE"
    assert first.company_name == "Fixture Calendar Corp"
    assert first.fiscal_label_raw == "Q1"
    assert first.fiscal_year == 2026
    assert first.period_end_date == date(2026, 3, 31)
    assert first.period_type == "Q1"
    assert first.fiscal_calendar_type == "month_based"
    assert first.period_length_weeks is None

    assert second.cik == "0000000456"
    assert second.fiscal_label_raw == "Q4"
    assert second.period_type == "FY"
    assert second.fiscal_calendar_type == "week_based_52_53"
    assert second.period_length_weeks == 53


def test_date_and_datetime_precision_are_not_confused() -> None:
    first, second = _parse_fixture("complete_payload.json").records

    assert first.estimated_release_date == date(2026, 4, 22)
    assert first.estimated_release_at is None
    assert first.estimated_release_precision == "date_only"

    assert second.estimated_release_date is None
    assert second.estimated_release_at == datetime(2027, 1, 28, 21, 5, tzinfo=UTC)
    assert second.estimated_release_precision == "exact_datetime"


def test_release_session_timestamp_and_confidence_are_normalized() -> None:
    first, second = _parse_fixture("complete_payload.json").records

    assert first.release_session == "after_market"
    assert first.source_observed_at == datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    assert first.confidence == Decimal("0.9000")

    assert second.release_session == "after_market"
    assert second.confidence == Decimal("0.7500")


def test_optional_fields_missing_stay_missing() -> None:
    record = _parse_fixture("optional_missing.json").records[0]

    assert record.provider_event_id == "fixture-evt-2001"
    assert record.parser_version == FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION
    assert record.raw_position == 1
    assert record.cik == ""
    assert record.ticker == ""
    assert record.exchange == ""
    assert record.provider_symbol == ""
    assert record.company_name == ""
    assert record.fiscal_label_raw == ""
    assert record.fiscal_year is None
    assert record.period_end_date is None
    assert record.period_type is None
    assert record.fiscal_calendar_type is None
    assert record.period_length_weeks is None
    assert record.estimated_release_date is None
    assert record.estimated_release_at is None
    assert record.estimated_release_precision == "unknown"
    assert record.release_session == "unknown"
    assert record.source_observed_at is None
    assert record.confidence is None


def test_custom_parser_version_is_reported() -> None:
    class CustomVersionParser(FixtureEarningsCalendarParser):
        parser_version = "fixture-earnings-calendar-parser-v2"

    result = CustomVersionParser().parse(
        (FIXTURE_DIR / "complete_payload.json").read_bytes(),
        provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
        provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
    )

    assert result.parser_version == "fixture-earnings-calendar-parser-v2"
    assert all(
        record.parser_version == "fixture-earnings-calendar-parser-v2" for record in result.records
    )


def test_unknown_fiscal_label_does_not_become_other() -> None:
    record = _parse_events([_event(fiscal_label_raw="NOT_A_PERIOD")]).records[0]

    assert record.period_type is None


def test_explicit_period_type_is_used_when_label_is_not_recognized() -> None:
    record = _parse_events([_event(fiscal_label_raw="FY2026", period_type="Q3")]).records[0]

    assert record.period_type == "Q3"


def test_q4_label_with_explicit_fy_is_consistent() -> None:
    record = _parse_events([_event(fiscal_label_raw="Q4", period_type="FY")]).records[0]

    assert record.period_type == "FY"


def test_conflicting_explicit_period_type_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="conflicts"):
        _parse_events([_event(fiscal_label_raw="Q1", period_type="FY")])


def test_missing_provider_event_id_is_unsupported_identity() -> None:
    with pytest.raises(UnsupportedEarningsCalendarIdentityError, match="raw_position 2"):
        _parse_fixture("missing_provider_event_id.json")

    assert issubclass(UnsupportedEarningsCalendarIdentityError, EarningsCalendarPayloadError)


def test_blank_provider_event_id_is_unsupported_identity() -> None:
    with pytest.raises(UnsupportedEarningsCalendarIdentityError, match="blank"):
        _parse_events([_event(provider_event_id="   ")])


def test_non_string_provider_event_id_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="provider_event_id"):
        _parse_events([_event(provider_event_id=123)])


def test_missing_identity_error_does_not_echo_payload_values() -> None:
    with pytest.raises(UnsupportedEarningsCalendarIdentityError) as excinfo:
        _parse_document(_envelope([{"ticker": "Bearer fixture-secret-token"}]))

    assert "fixture-secret-token" not in str(excinfo.value)


def test_provider_event_ids_are_preserved_and_not_canonical_identity() -> None:
    result = _parse_fixture("complete_payload.json")

    assert result.records[0].provider_event_id == "fixture-evt-1001"
    assert result.records[1].provider_event_id == "fixture-evt-1002"
    assert not hasattr(result.records[0], "identity_key")
    assert not hasattr(result.records[0], "company_id")


def test_parser_module_has_no_domain_writer_or_network_imports() -> None:
    tree = ast.parse(PARSER_MODULE_PATH.read_text(encoding="utf-8"))
    imported_modules: list[str] = []
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)
            imported_names.update(alias.name for alias in node.names)

    for prefix in FORBIDDEN_MODULE_PREFIXES:
        assert not any(
            module == prefix or module.startswith(f"{prefix}.") for module in imported_modules
        )
    assert imported_names.isdisjoint(FORBIDDEN_IMPORTED_NAMES)


@pytest.mark.django_db
def test_parse_executes_no_database_queries(
    django_assert_num_queries: Callable[[int], AbstractContextManager[None]],
) -> None:
    with django_assert_num_queries(0):
        result = _parse_fixture("complete_payload.json")

    assert len(result.records) == 2


@pytest.mark.django_db
def test_parse_does_not_change_domain_or_audit_row_counts() -> None:
    before = _row_counts()

    _parse_fixture("complete_payload.json")

    assert _row_counts() == before


def test_parse_does_not_open_network_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("parser must not open network connections")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", fail_connect)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", fail_connect)

    result = _parse_fixture("complete_payload.json")

    assert len(result.records) == 2


def test_invalid_utf8_payload_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="UTF-8"):
        _parser().parse(
            b"\xff\xfe",
            provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        )


def test_invalid_json_payload_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="JSON"):
        _parser().parse(
            b"{invalid-json",
            provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        )


def test_non_object_root_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="JSON object"):
        _parser().parse(
            json.dumps([]).encode(),
            provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        )


def test_missing_events_is_rejected() -> None:
    document = {
        "fixture_version": FIXTURE_EARNINGS_CALENDAR_FORMAT_VERSION,
        "provider_key": FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
        "provider_version": FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
    }

    with pytest.raises(EarningsCalendarPayloadError, match="events"):
        _parse_document(document)


def test_non_list_events_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="events"):
        _parse_events({"provider_event_id": "fixture-evt-test"})


def test_non_object_record_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="raw_position 1"):
        _parse_events([["not-an-object"]])


def test_fixture_version_mismatch_is_rejected() -> None:
    document = _envelope([])
    document["fixture_version"] = "v2"

    with pytest.raises(EarningsCalendarPayloadError, match="fixture_version"):
        _parse_document(document)


def test_provider_key_mismatch_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="provider_key"):
        _parser().parse(
            (FIXTURE_DIR / "empty_payload.json").read_bytes(),
            provider_key="other-fixture-provider",
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        )


def test_provider_version_mismatch_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="provider_version"):
        _parser().parse(
            (FIXTURE_DIR / "empty_payload.json").read_bytes(),
            provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            provider_version="fixture-v2",
        )


def test_parser_context_rejects_non_bytes_payload() -> None:
    with pytest.raises(EarningsCalendarParserContextError, match="bytes"):
        _parser().parse(
            cast(bytes, "not-bytes"),
            provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        )


def test_parser_context_rejects_empty_provider_metadata() -> None:
    with pytest.raises(EarningsCalendarParserContextError, match="provider_key"):
        _parser().parse(
            b"{}",
            provider_key="",
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        )
    with pytest.raises(EarningsCalendarParserContextError, match="provider_version"):
        _parser().parse(
            b"{}",
            provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            provider_version="",
        )


def test_parser_context_rejects_empty_parser_version() -> None:
    class EmptyVersionParser(FixtureEarningsCalendarParser):
        parser_version = ""

    with pytest.raises(EarningsCalendarParserContextError, match="parser_version"):
        EmptyVersionParser().parse(
            (FIXTURE_DIR / "empty_payload.json").read_bytes(),
            provider_key=FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY,
            provider_version=FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION,
        )


def test_duplicate_provider_event_id_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="duplicate"):
        _parse_fixture("duplicate_provider_event_id.json")


def test_partially_malformed_payload_is_rejected_atomically() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="raw_position 2"):
        _parse_fixture("partially_malformed.json")


def test_malformed_single_record_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="fiscal_year"):
        _parse_fixture("malformed_record.json")


@pytest.mark.parametrize(
    ("field_name", "value", "match"),
    (
        ("estimated_release_date", "2026-04-22T00:00:00+00:00", "YYYY-MM-DD"),
        ("estimated_release_at", "2026-04-22T20:30:00", "timezone-aware"),
        ("source_observed_at", "2026-04-22T20:30:00", "timezone-aware"),
        ("release_session", "lunch", "release_session"),
        ("fiscal_calendar_type", "lunar", "fiscal_calendar_type"),
        ("period_length_weeks", 51, "52 or 53"),
        ("cik", "ABC", "cik"),
        ("fiscal_year", True, "fiscal_year"),
    ),
)
def test_invalid_optional_values_are_rejected(
    field_name: str,
    value: object,
    match: str,
) -> None:
    with pytest.raises(EarningsCalendarPayloadError, match=match):
        _parse_events([_event(**{field_name: value})])


def test_invalid_explicit_period_type_is_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="period_type"):
        _parse_events([_event(period_type="Q5")])


def test_both_estimated_release_representations_are_rejected() -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="both"):
        _parse_events(
            [
                _event(
                    estimated_release_date="2026-04-22",
                    estimated_release_at="2026-04-22T20:30:00+00:00",
                )
            ]
        )


@pytest.mark.parametrize("value", (True, -0.1, 1.1, "not-a-number"))
def test_invalid_confidence_is_rejected(value: object) -> None:
    with pytest.raises(EarningsCalendarPayloadError, match="confidence"):
        _parse_events([_event(confidence=value)])


def test_unknown_event_fields_are_ignored() -> None:
    result = _parse_events([_event(provider_event_note="ignored")])

    assert result.records[0].provider_event_id == "fixture-evt-test"


def test_error_messages_do_not_echo_payload_values() -> None:
    with pytest.raises(EarningsCalendarPayloadError) as excinfo:
        _parse_events([_event(fiscal_year="Bearer fixture-secret-token")])

    assert "fixture-secret-token" not in str(excinfo.value)
