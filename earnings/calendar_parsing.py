"""Provider-neutral parser contract for earnings calendar payloads.

This module is intentionally side-effect free: it parses and normalizes raw
payload bytes without reading the database, resolving companies, creating
canonical identity, or writing audit/domain rows.  Raw lineage binding
(``raw_data_record_id``) and persistence belong to the ingestion orchestration
introduced in a later 4.2C sub-stage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast, runtime_checkable

from companies.services import CompanyServiceError, normalize_cik
from earnings.identity import normalize_period_type
from earnings.models import (
    ALLOWED_FISCAL_CALENDAR_TYPES,
    ALLOWED_PERIOD_TYPES,
    ALLOWED_RELEASE_SESSIONS,
)

FIXTURE_EARNINGS_CALENDAR_PROVIDER_KEY = "fixture-earnings-calendar"
FIXTURE_EARNINGS_CALENDAR_PROVIDER_VERSION = "fixture-v1"
FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION = "fixture-earnings-calendar-parser-v1"
FIXTURE_EARNINGS_CALENDAR_FORMAT_VERSION = "v1"

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONFIDENCE_QUANTUM = Decimal("0.0001")


class EarningsCalendarParserContextError(ValueError):
    """Raised when parser arguments or parser identity are invalid."""


class EarningsCalendarParseError(ValueError):
    """Base class for deterministic payload parse failures."""


class EarningsCalendarPayloadError(EarningsCalendarParseError):
    """Raised when a payload or record is malformed or internally inconsistent."""


class UnsupportedEarningsCalendarIdentityError(EarningsCalendarPayloadError):
    """Raised when a record lacks a stable upstream provider_event_id."""


@dataclass(frozen=True, slots=True)
class NormalizedEarningsCalendarRecord:
    """One provider-neutral earnings calendar record from a raw payload."""

    parser_version: str
    provider_event_id: str
    raw_position: int
    cik: str = ""
    ticker: str = ""
    exchange: str = ""
    provider_symbol: str = ""
    company_name: str = ""
    fiscal_label_raw: str = ""
    fiscal_year: int | None = None
    period_end_date: date | None = None
    period_type: str | None = None
    fiscal_calendar_type: str | None = None
    period_length_weeks: int | None = None
    estimated_release_date: date | None = None
    estimated_release_at: datetime | None = None
    estimated_release_precision: str = "unknown"
    release_session: str = "unknown"
    source_observed_at: datetime | None = None
    confidence: Decimal | None = None


@dataclass(frozen=True, slots=True)
class EarningsCalendarParseResult:
    """Structured result of parsing one raw payload/page."""

    provider_key: str
    provider_version: str
    parser_version: str
    records: tuple[NormalizedEarningsCalendarRecord, ...]


@runtime_checkable
class EarningsCalendarParser(Protocol):
    """Structural contract implemented by provider-specific parsers."""

    parser_version: str

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> EarningsCalendarParseResult: ...


class FixtureEarningsCalendarParser:
    """Parse the synthetic fixture envelope into normalized records.

    The fixture JSON format is a test-only schema; it is not a contract for any
    real third-party earnings calendar provider.  A page is parsed atomically:
    any malformed record or unsupported identity fails the whole page so raw
    lineage can be retained and replayed by the future ingestion layer.
    """

    parser_version = FIXTURE_EARNINGS_CALENDAR_PARSER_VERSION

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> EarningsCalendarParseResult:
        normalized_provider_key = _require_context_text(
            provider_key,
            value_name="provider_key",
            maximum_length=64,
        )
        normalized_provider_version = _require_context_text(
            provider_version,
            value_name="provider_version",
            maximum_length=100,
        )
        normalized_parser_version = _require_context_text(
            self.parser_version,
            value_name="parser_version",
            maximum_length=100,
        )
        if not isinstance(raw_content, bytes):
            raise EarningsCalendarParserContextError("raw_content must be bytes.")

        document = _load_document(raw_content)
        raw_events = _validate_envelope(
            document,
            provider_key=normalized_provider_key,
            provider_version=normalized_provider_version,
        )

        records: list[NormalizedEarningsCalendarRecord] = []
        first_positions: dict[str, int] = {}
        for raw_position, raw_event in enumerate(raw_events, start=1):
            record = _parse_record(
                raw_event,
                raw_position=raw_position,
                parser_version=normalized_parser_version,
            )
            first_position = first_positions.get(record.provider_event_id)
            if first_position is not None:
                raise EarningsCalendarPayloadError(
                    f"duplicate provider_event_id at raw_position {raw_position}; "
                    f"first seen at raw_position {first_position}."
                )
            first_positions[record.provider_event_id] = raw_position
            records.append(record)

        return EarningsCalendarParseResult(
            provider_key=normalized_provider_key,
            provider_version=normalized_provider_version,
            parser_version=normalized_parser_version,
            records=tuple(records),
        )


def _load_document(raw_content: bytes) -> dict[str, object]:
    try:
        decoded = raw_content.decode("utf-8")
    except UnicodeDecodeError:
        raise EarningsCalendarPayloadError("fixture payload is not valid UTF-8.") from None
    try:
        loaded = json.loads(decoded)
    except json.JSONDecodeError:
        raise EarningsCalendarPayloadError("fixture payload is not valid JSON.") from None
    if not isinstance(loaded, dict):
        raise EarningsCalendarPayloadError("fixture payload must be a JSON object.")
    return cast(dict[str, object], loaded)


def _validate_envelope(
    document: dict[str, object],
    *,
    provider_key: str,
    provider_version: str,
) -> list[object]:
    format_version = document.get("fixture_version")
    if format_version != FIXTURE_EARNINGS_CALENDAR_FORMAT_VERSION:
        raise EarningsCalendarPayloadError(
            f"fixture_version must be {FIXTURE_EARNINGS_CALENDAR_FORMAT_VERSION!r}."
        )
    if document.get("provider_key") != provider_key:
        raise EarningsCalendarPayloadError("payload provider_key does not match parser context.")
    if document.get("provider_version") != provider_version:
        raise EarningsCalendarPayloadError(
            "payload provider_version does not match parser context."
        )
    events = document.get("events")
    if not isinstance(events, list):
        raise EarningsCalendarPayloadError("fixture payload 'events' must be a JSON array.")
    return cast(list[object], events)


def _parse_record(
    raw_event: object,
    *,
    raw_position: int,
    parser_version: str,
) -> NormalizedEarningsCalendarRecord:
    if not isinstance(raw_event, dict):
        raise EarningsCalendarPayloadError(
            f"record at raw_position {raw_position} must be a JSON object."
        )
    event = cast(dict[str, object], raw_event)
    estimated_release_date, estimated_release_at, estimated_release_precision = (
        _parse_estimated_release(event, raw_position=raw_position)
    )
    return NormalizedEarningsCalendarRecord(
        parser_version=parser_version,
        provider_event_id=_require_provider_event_id(event, raw_position=raw_position),
        raw_position=raw_position,
        cik=_parse_cik(event, raw_position=raw_position),
        ticker=_optional_text(
            event,
            "ticker",
            maximum_length=32,
            raw_position=raw_position,
        ),
        exchange=_optional_text(
            event,
            "exchange",
            maximum_length=32,
            raw_position=raw_position,
        ),
        provider_symbol=_optional_text(
            event,
            "provider_symbol",
            maximum_length=64,
            raw_position=raw_position,
        ),
        company_name=_optional_text(
            event,
            "company_name",
            maximum_length=255,
            raw_position=raw_position,
        ),
        fiscal_label_raw=_optional_text(
            event,
            "fiscal_label_raw",
            maximum_length=64,
            raw_position=raw_position,
        ),
        fiscal_year=_optional_int(event, "fiscal_year", raw_position=raw_position),
        period_end_date=_optional_date(event, "period_end_date", raw_position=raw_position),
        period_type=_parse_period_type(event, raw_position=raw_position),
        fiscal_calendar_type=_parse_fiscal_calendar_type(
            event,
            raw_position=raw_position,
        ),
        period_length_weeks=_parse_period_length_weeks(
            event,
            raw_position=raw_position,
        ),
        estimated_release_date=estimated_release_date,
        estimated_release_at=estimated_release_at,
        estimated_release_precision=estimated_release_precision,
        release_session=_parse_release_session(event, raw_position=raw_position),
        source_observed_at=_optional_datetime(
            event,
            "source_observed_at",
            raw_position=raw_position,
        ),
        confidence=_parse_confidence(event, raw_position=raw_position),
    )


def _require_context_text(value: object, *, value_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise EarningsCalendarParserContextError(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise EarningsCalendarParserContextError(f"{value_name} must not be empty.")
    if len(normalized) > maximum_length:
        raise EarningsCalendarParserContextError(
            f"{value_name} must contain at most {maximum_length} characters."
        )
    return normalized


def _require_provider_event_id(event: dict[str, object], *, raw_position: int) -> str:
    if "provider_event_id" not in event or event["provider_event_id"] is None:
        raise UnsupportedEarningsCalendarIdentityError(
            f"record at raw_position {raw_position} is missing stable provider_event_id."
        )
    value = event["provider_event_id"]
    if not isinstance(value, str):
        raise EarningsCalendarPayloadError(
            f"provider_event_id must be a string at raw_position {raw_position}."
        )
    normalized = value.strip()
    if not normalized:
        raise UnsupportedEarningsCalendarIdentityError(
            f"record at raw_position {raw_position} has a blank provider_event_id."
        )
    if len(normalized) > 255:
        raise EarningsCalendarPayloadError(
            f"provider_event_id exceeds 255 characters at raw_position {raw_position}."
        )
    return normalized


def _optional_text(
    event: dict[str, object],
    field_name: str,
    *,
    maximum_length: int,
    raw_position: int,
) -> str:
    value = event.get(field_name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EarningsCalendarPayloadError(
            f"{field_name} must be a string at raw_position {raw_position}."
        )
    normalized = value.strip()
    if len(normalized) > maximum_length:
        raise EarningsCalendarPayloadError(
            f"{field_name} must contain at most {maximum_length} characters "
            f"at raw_position {raw_position}."
        )
    return normalized


def _optional_int(
    event: dict[str, object],
    field_name: str,
    *,
    raw_position: int,
) -> int | None:
    value = event.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise EarningsCalendarPayloadError(
            f"{field_name} must be an integer at raw_position {raw_position}."
        )
    return value


def _optional_date(
    event: dict[str, object],
    field_name: str,
    *,
    raw_position: int,
) -> date | None:
    value = event.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_ONLY_RE.fullmatch(value):
        raise EarningsCalendarPayloadError(
            f"{field_name} must use YYYY-MM-DD at raw_position {raw_position}."
        )
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise EarningsCalendarPayloadError(
            f"{field_name} is not a valid calendar date at raw_position {raw_position}."
        ) from None


def _optional_datetime(
    event: dict[str, object],
    field_name: str,
    *,
    raw_position: int,
) -> datetime | None:
    value = event.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise EarningsCalendarPayloadError(
            f"{field_name} must be an ISO-8601 datetime string at raw_position {raw_position}."
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise EarningsCalendarPayloadError(
            f"{field_name} is not a valid ISO-8601 datetime at raw_position {raw_position}."
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EarningsCalendarPayloadError(
            f"{field_name} must be timezone-aware at raw_position {raw_position}."
        )
    return parsed


def _parse_cik(event: dict[str, object], *, raw_position: int) -> str:
    value = event.get("cik")
    if value is None or value == "":
        return ""
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise EarningsCalendarPayloadError(
            f"cik must be a digit string at raw_position {raw_position}."
        )
    try:
        normalized = normalize_cik(value)
    except CompanyServiceError:
        raise EarningsCalendarPayloadError(
            f"cik is invalid at raw_position {raw_position}."
        ) from None
    return normalized or ""


def _parse_period_type(event: dict[str, object], *, raw_position: int) -> str | None:
    label_raw = _optional_text(
        event,
        "fiscal_label_raw",
        maximum_length=64,
        raw_position=raw_position,
    )
    label_type, _ = normalize_period_type(label_raw)
    explicit = event.get("period_type")
    if explicit is None:
        return label_type
    if not isinstance(explicit, str):
        raise EarningsCalendarPayloadError(
            f"period_type must be a string at raw_position {raw_position}."
        )
    normalized = explicit.strip().upper()
    if normalized not in ALLOWED_PERIOD_TYPES:
        raise EarningsCalendarPayloadError(
            f"period_type must use the normalized period enum at raw_position {raw_position}."
        )
    if label_type is not None and normalized != label_type:
        raise EarningsCalendarPayloadError(
            f"period_type conflicts with fiscal_label_raw at raw_position {raw_position}."
        )
    return normalized


def _parse_fiscal_calendar_type(
    event: dict[str, object],
    *,
    raw_position: int,
) -> str | None:
    value = event.get("fiscal_calendar_type")
    if value is None:
        return None
    if not isinstance(value, str):
        raise EarningsCalendarPayloadError(
            f"fiscal_calendar_type must be a string at raw_position {raw_position}."
        )
    normalized = value.strip().lower()
    if normalized not in ALLOWED_FISCAL_CALENDAR_TYPES:
        raise EarningsCalendarPayloadError(
            f"fiscal_calendar_type must use the supported enum at raw_position {raw_position}."
        )
    return normalized


def _parse_period_length_weeks(
    event: dict[str, object],
    *,
    raw_position: int,
) -> int | None:
    value = event.get("period_length_weeks")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise EarningsCalendarPayloadError(
            f"period_length_weeks must be an integer at raw_position {raw_position}."
        )
    if value not in (52, 53):
        raise EarningsCalendarPayloadError(
            f"period_length_weeks must be 52 or 53 at raw_position {raw_position}."
        )
    return value


def _parse_estimated_release(
    event: dict[str, object],
    *,
    raw_position: int,
) -> tuple[date | None, datetime | None, str]:
    raw_date_value = event.get("estimated_release_date")
    raw_at_value = event.get("estimated_release_at")
    if raw_date_value is not None and raw_at_value is not None:
        raise EarningsCalendarPayloadError(
            f"estimated release must not define both date and datetime "
            f"at raw_position {raw_position}."
        )
    if raw_date_value is not None:
        return (
            _optional_date(event, "estimated_release_date", raw_position=raw_position),
            None,
            "date_only",
        )
    if raw_at_value is not None:
        return (
            None,
            _optional_datetime(event, "estimated_release_at", raw_position=raw_position),
            "exact_datetime",
        )
    return None, None, "unknown"


def _parse_release_session(event: dict[str, object], *, raw_position: int) -> str:
    value = event.get("release_session")
    if value is None:
        return "unknown"
    if not isinstance(value, str):
        raise EarningsCalendarPayloadError(
            f"release_session must be a string at raw_position {raw_position}."
        )
    normalized = value.strip().lower()
    if normalized not in ALLOWED_RELEASE_SESSIONS:
        raise EarningsCalendarPayloadError(
            f"release_session must use the supported enum at raw_position {raw_position}."
        )
    return normalized


def _parse_confidence(event: dict[str, object], *, raw_position: int) -> Decimal | None:
    value = event.get("confidence")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise EarningsCalendarPayloadError(
            f"confidence must be a number between 0 and 1 at raw_position {raw_position}."
        )
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise EarningsCalendarPayloadError(
            f"confidence must be a number between 0 and 1 at raw_position {raw_position}."
        ) from None
    if not normalized.is_finite() or not Decimal("0") <= normalized <= Decimal("1"):
        raise EarningsCalendarPayloadError(
            f"confidence must be a number between 0 and 1 at raw_position {raw_position}."
        )
    return normalized.quantize(_CONFIDENCE_QUANTUM)
