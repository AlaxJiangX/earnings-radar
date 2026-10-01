"""Fixture-first Investor Relations parser contract (Stage 4.5B).

This module is intentionally side-effect free: it parses persisted raw bytes
into provider-neutral normalized IR records without reading the database,
resolving companies, deciding authority, or writing any domain/audit row.

The fixture JSON format is a synthetic test contract.  It is not a contract
for any real IR page, feed or vendor.  No real IR source is approved and the
live gate remains BLOCKED (ADR-022).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast, runtime_checkable

from earnings.models import (
    ALLOWED_INVESTOR_RELATIONS_CANCELLATION_SCOPES,
    ALLOWED_INVESTOR_RELATIONS_ITEM_TYPES,
    ALLOWED_PERIOD_TYPES,
    ALLOWED_RELEASE_SESSIONS,
    EarningsDatePrecision,
)

IR_SOURCE_EVENT_IDENTITY_VERSION = "ir-source-event-identity-v1"
IR_SOURCE_EVENT_IDENTITY_PREFIX = "internal:ir:v1:"

FIXTURE_IR_PROVIDER_KEY = "fixture-ir"
FIXTURE_IR_PROVIDER_VERSION = "fixture-v1"
FIXTURE_IR_PARSER_VERSION = "fixture-ir-parser-v1"
FIXTURE_IR_FORMAT_VERSION = "ir-fixture-v1"

FAILURE_MISSING_COMPANY = "MISSING_COMPANY"
FAILURE_INVALID_COMPANY = "INVALID_COMPANY"
FAILURE_INCOMPLETE_PERIOD_IDENTITY = "INCOMPLETE_PERIOD_IDENTITY"
FAILURE_INVALID_ITEM_TYPE = "INVALID_ITEM_TYPE"
FAILURE_INVALID_FACT = "INVALID_FACT"
FAILURE_INVALID_ITEM_SHAPE = "INVALID_ITEM_SHAPE"
FAILURE_INVALID_CANCELLATION = "INVALID_CANCELLATION"
FAILURE_INVALID_SOURCE_IDENTITY = "INVALID_SOURCE_IDENTITY"
FAILURE_DUPLICATE_SOURCE_IDENTITY = "DUPLICATE_SOURCE_IDENTITY"

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONFIDENCE_QUANTUM = Decimal("0.0001")


class InvestorRelationsParserContextError(ValueError):
    """Raised when parser arguments or parser identity are invalid."""


class InvestorRelationsParseError(ValueError):
    """Base class for deterministic IR payload parse failures."""


class InvestorRelationsPayloadError(InvestorRelationsParseError):
    """Raised when the payload envelope is malformed or inconsistent."""


class InvestorRelationsItemError(InvestorRelationsParseError):
    """Expected per-item data failure; isolated from sibling items."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class IRDateFact:
    """One optional date/datetime fact with explicit precision."""

    date_value: date | None = None
    datetime_value: datetime | None = None
    precision: str = EarningsDatePrecision.UNKNOWN

    @property
    def is_known(self) -> bool:
        return self.precision != EarningsDatePrecision.UNKNOWN

    def as_schedule_change_value(self) -> date | datetime | None:
        if self.precision == EarningsDatePrecision.DATE_ONLY:
            return self.date_value
        if self.precision == EarningsDatePrecision.EXACT_DATETIME:
            return self.datetime_value
        return None

    def as_canonical_value(self) -> dict[str, str] | None:
        if self.precision == EarningsDatePrecision.DATE_ONLY and self.date_value is not None:
            return {
                "kind": "date",
                "precision": EarningsDatePrecision.DATE_ONLY,
                "value": self.date_value.isoformat(),
            }
        if (
            self.precision == EarningsDatePrecision.EXACT_DATETIME
            and self.datetime_value is not None
        ):
            return {
                "kind": "datetime",
                "precision": EarningsDatePrecision.EXACT_DATETIME,
                "value": self.datetime_value.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
        return None


UNKNOWN_IR_DATE_FACT = IRDateFact()


@dataclass(frozen=True, slots=True)
class NormalizedInvestorRelationsRecord:
    """One provider-neutral normalized IR record from persisted raw bytes."""

    parser_version: str
    source_event_identity: str
    raw_position: int
    company_id: uuid.UUID
    period_end_date: date
    period_type: str
    item_type: str
    estimated_release: IRDateFact = UNKNOWN_IR_DATE_FACT
    confirmed_release: IRDateFact = UNKNOWN_IR_DATE_FACT
    earnings_release: IRDateFact = UNKNOWN_IR_DATE_FACT
    conference_call: IRDateFact = UNKNOWN_IR_DATE_FACT
    release_session: str | None = None
    cancellation: dict[str, object] | None = None
    source_observed_at: datetime | None = None
    confidence: Decimal | None = None


@dataclass(frozen=True, slots=True)
class InvestorRelationsItemFailure:
    """One malformed item that was retained as raw lineage only."""

    raw_position: int
    reason_code: str
    message: str


@dataclass(frozen=True, slots=True)
class InvestorRelationsParseResult:
    provider_key: str
    provider_version: str
    parser_version: str
    source_key: str
    records: tuple[NormalizedInvestorRelationsRecord, ...]
    failures: tuple[InvestorRelationsItemFailure, ...] = ()


@runtime_checkable
class InvestorRelationsParser(Protocol):
    """Structural contract implemented by IR parsers."""

    parser_version: str

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> InvestorRelationsParseResult: ...


def derive_ir_source_event_identity(
    *,
    source_key: str,
    company_id: uuid.UUID,
    period_end_date: date,
    period_type: str,
    item_type: str,
) -> str:
    """Derive the internal IR source event identity from stable facts only.

    Mutable announced dates, fetched_at, raw positions, parser versions and
    database identifiers are deliberately excluded (ADR-022 §10.1).
    """

    normalized_source_key = _require_text(source_key, value_name="source_key", maximum_length=64)
    normalized_period_type = _normalize_period_type(period_type)
    normalized_item_type = _normalize_item_type(item_type)
    if not isinstance(company_id, uuid.UUID):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_COMPANY,
            message="company_id must be a UUID.",
        )
    if not isinstance(period_end_date, date) or isinstance(period_end_date, datetime):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INCOMPLETE_PERIOD_IDENTITY,
            message="period_end_date must be a date.",
        )

    payload = {
        "company_id": str(company_id),
        "item_type": normalized_item_type,
        "period_end_date": period_end_date.isoformat(),
        "period_type": normalized_period_type,
        "source_event_identity_version": IR_SOURCE_EVENT_IDENTITY_VERSION,
        "source_key": normalized_source_key,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    return f"{IR_SOURCE_EVENT_IDENTITY_PREFIX}{digest}"


class FixtureInvestorRelationsParser:
    """Parse the synthetic IR fixture envelope into normalized records.

    Item failures are isolated: one malformed item never blocks sibling items.
    The ingestion layer keeps the raw payload and records the item failures in
    the parse attempt / run counts.
    """

    parser_version = FIXTURE_IR_PARSER_VERSION

    def parse(
        self,
        raw_content: bytes,
        *,
        provider_key: str,
        provider_version: str,
    ) -> InvestorRelationsParseResult:
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
            raise InvestorRelationsParserContextError("raw_content must be bytes.")

        document = _load_document(raw_content)
        _validate_envelope(
            document,
            provider_key=normalized_provider_key,
            provider_version=normalized_provider_version,
        )
        source_key = _require_text(
            document.get("source_key"),
            value_name="source_key",
            maximum_length=64,
        )
        raw_items = document.get("items")
        if not isinstance(raw_items, list):
            raise InvestorRelationsPayloadError("items must be a JSON array.")

        records: list[NormalizedInvestorRelationsRecord] = []
        failures: list[InvestorRelationsItemFailure] = []
        first_positions: dict[str, int] = {}
        for raw_position, raw_item in enumerate(raw_items, start=1):
            if not isinstance(raw_item, dict):
                failures.append(
                    InvestorRelationsItemFailure(
                        raw_position=raw_position,
                        reason_code=FAILURE_INVALID_FACT,
                        message="item must be a JSON object.",
                    )
                )
                continue
            try:
                record = _parse_record(
                    raw_item,
                    raw_position=raw_position,
                    parser_version=normalized_parser_version,
                    source_key=source_key,
                )
            except InvestorRelationsItemError as error:
                failures.append(
                    InvestorRelationsItemFailure(
                        raw_position=raw_position,
                        reason_code=error.reason_code,
                        message=str(error),
                    )
                )
                continue
            first_position = first_positions.get(record.source_event_identity)
            if first_position is not None:
                failures.append(
                    InvestorRelationsItemFailure(
                        raw_position=raw_position,
                        reason_code=FAILURE_DUPLICATE_SOURCE_IDENTITY,
                        message=(
                            "duplicate source_event_identity at raw_position "
                            f"{raw_position}; first seen at raw_position {first_position}."
                        ),
                    )
                )
                continue
            first_positions[record.source_event_identity] = raw_position
            records.append(record)

        return InvestorRelationsParseResult(
            provider_key=normalized_provider_key,
            provider_version=normalized_provider_version,
            parser_version=normalized_parser_version,
            source_key=source_key,
            records=tuple(records),
            failures=tuple(failures),
        )


def _load_document(raw_content: bytes) -> dict[str, object]:
    try:
        decoded = raw_content.decode("utf-8")
    except UnicodeDecodeError:
        raise InvestorRelationsPayloadError("fixture payload is not valid UTF-8.") from None
    try:
        loaded = json.loads(decoded)
    except json.JSONDecodeError:
        raise InvestorRelationsPayloadError("fixture payload is not valid JSON.") from None
    if not isinstance(loaded, dict):
        raise InvestorRelationsPayloadError("fixture payload must be a JSON object.")
    return cast(dict[str, object], loaded)


def _validate_envelope(
    document: dict[str, object],
    *,
    provider_key: str,
    provider_version: str,
) -> None:
    if document.get("fixture_version") != FIXTURE_IR_FORMAT_VERSION:
        raise InvestorRelationsPayloadError(
            f"fixture_version must be {FIXTURE_IR_FORMAT_VERSION!r}."
        )
    if document.get("provider_key") != provider_key:
        raise InvestorRelationsPayloadError("payload provider_key does not match parser context.")
    if document.get("provider_version") != provider_version:
        raise InvestorRelationsPayloadError(
            "payload provider_version does not match parser context."
        )


def _parse_record(
    item: dict[str, object],
    *,
    raw_position: int,
    parser_version: str,
    source_key: str,
) -> NormalizedInvestorRelationsRecord:
    company_id = _parse_company_id(item)
    period_end_date = _parse_period_end_date(item)
    period_type = _normalize_period_type(item.get("period_type"))
    item_type = _normalize_item_type(item.get("item_type"))

    estimated_release = _parse_optional_fact(item, "estimated_release")
    confirmed_release = _parse_optional_fact(item, "confirmed_release")
    earnings_release = _parse_optional_fact(item, "earnings_release")
    conference_call = _parse_optional_fact(item, "conference_call")
    release_session = _parse_release_session(item)
    cancellation = _parse_cancellation(item, item_type=item_type)
    source_observed_at = _parse_source_observed_at(item)
    confidence = _parse_confidence(item)
    _validate_item_shape(
        item_type=item_type,
        estimated_release=estimated_release,
        confirmed_release=confirmed_release,
        earnings_release=earnings_release,
        conference_call=conference_call,
        cancellation=cancellation,
    )

    source_event_identity = _parse_source_event_identity(item)
    if source_event_identity is None:
        source_event_identity = derive_ir_source_event_identity(
            source_key=source_key,
            company_id=company_id,
            period_end_date=period_end_date,
            period_type=period_type,
            item_type=item_type,
        )

    return NormalizedInvestorRelationsRecord(
        parser_version=parser_version,
        source_event_identity=source_event_identity,
        raw_position=raw_position,
        company_id=company_id,
        period_end_date=period_end_date,
        period_type=period_type,
        item_type=item_type,
        estimated_release=estimated_release,
        confirmed_release=confirmed_release,
        earnings_release=earnings_release,
        conference_call=conference_call,
        release_session=release_session,
        cancellation=cancellation,
        source_observed_at=source_observed_at,
        confidence=confidence,
    )


def _parse_company_id(item: dict[str, object]) -> uuid.UUID:
    raw_value = item.get("company_id")
    if raw_value is None:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_MISSING_COMPANY,
            message="company_id is required for an exact IR observation.",
        )
    if not isinstance(raw_value, str):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_COMPANY,
            message="company_id must be a UUID string.",
        )
    try:
        return uuid.UUID(raw_value.strip())
    except ValueError:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_COMPANY,
            message="company_id must be a UUID string.",
        ) from None


def _parse_period_end_date(item: dict[str, object]) -> date:
    raw_value = item.get("period_end_date")
    if raw_value is None:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INCOMPLETE_PERIOD_IDENTITY,
            message="period_end_date is required for an IR observation.",
        )
    if not isinstance(raw_value, str) or not _DATE_ONLY_RE.fullmatch(raw_value.strip()):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INCOMPLETE_PERIOD_IDENTITY,
            message="period_end_date must use YYYY-MM-DD.",
        )
    try:
        return date.fromisoformat(raw_value.strip())
    except ValueError:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INCOMPLETE_PERIOD_IDENTITY,
            message="period_end_date must be a valid calendar date.",
        ) from None


def _normalize_period_type(raw_value: object) -> str:
    if raw_value is None:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INCOMPLETE_PERIOD_IDENTITY,
            message="period_type is required for an IR observation.",
        )
    if not isinstance(raw_value, str):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INCOMPLETE_PERIOD_IDENTITY,
            message="period_type must be a string.",
        )
    normalized = raw_value.strip().upper()
    if normalized not in ALLOWED_PERIOD_TYPES:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INCOMPLETE_PERIOD_IDENTITY,
            message="period_type must use the normalized earnings period enum.",
        )
    return normalized


def _normalize_item_type(raw_value: object) -> str:
    if not isinstance(raw_value, str):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_ITEM_TYPE,
            message="item_type must be a string.",
        )
    normalized = raw_value.strip().lower()
    if normalized not in ALLOWED_INVESTOR_RELATIONS_ITEM_TYPES:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_ITEM_TYPE,
            message="item_type must use the supported IR item enum.",
        )
    return normalized


def _parse_optional_fact(item: dict[str, object], key: str) -> IRDateFact:
    raw_value = item.get(key)
    if raw_value is None:
        return UNKNOWN_IR_DATE_FACT
    if isinstance(raw_value, str):
        if not _DATE_ONLY_RE.fullmatch(raw_value.strip()):
            raise InvestorRelationsItemError(
                reason_code=FAILURE_INVALID_FACT,
                message=f"{key} string values must use YYYY-MM-DD.",
            )
        return IRDateFact(
            date_value=_parse_iso_date(raw_value, key=key),
            precision=EarningsDatePrecision.DATE_ONLY,
        )
    if not isinstance(raw_value, dict):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message=f"{key} must be a date string or an explicit value mapping.",
        )
    if set(raw_value) != {"value", "precision"}:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message=f"{key} mapping must contain exactly 'value' and 'precision'.",
        )
    precision = raw_value["precision"]
    value = raw_value["value"]
    if not isinstance(precision, str):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message=f"{key} precision must be a string.",
        )
    normalized_precision = precision.strip().lower()
    if normalized_precision == EarningsDatePrecision.DATE_ONLY:
        if not isinstance(value, str) or not _DATE_ONLY_RE.fullmatch(value.strip()):
            raise InvestorRelationsItemError(
                reason_code=FAILURE_INVALID_FACT,
                message=f"{key} date_only values must use YYYY-MM-DD.",
            )
        return IRDateFact(
            date_value=_parse_iso_date(value, key=key),
            precision=EarningsDatePrecision.DATE_ONLY,
        )
    if normalized_precision == EarningsDatePrecision.EXACT_DATETIME:
        if not isinstance(value, str):
            raise InvestorRelationsItemError(
                reason_code=FAILURE_INVALID_FACT,
                message=f"{key} exact_datetime values must be ISO-8601 strings.",
            )
        return IRDateFact(
            datetime_value=_parse_aware_datetime(value, value_name=key),
            precision=EarningsDatePrecision.EXACT_DATETIME,
        )
    raise InvestorRelationsItemError(
        reason_code=FAILURE_INVALID_FACT,
        message=f"{key} precision must be date_only or exact_datetime.",
    )


def _parse_iso_date(raw_value: str, *, key: str) -> date:
    try:
        return date.fromisoformat(raw_value.strip())
    except ValueError:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message=f"{key} must be a valid calendar date.",
        ) from None


def _parse_aware_datetime(raw_value: str, *, value_name: str) -> datetime:
    candidate = raw_value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message=f"{value_name} must be a valid ISO-8601 datetime.",
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message=f"{value_name} must be timezone-aware.",
        )
    return parsed.astimezone(UTC)


def _parse_release_session(item: dict[str, object]) -> str | None:
    raw_value = item.get("release_session")
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message="release_session must be a string or null.",
        )
    normalized = raw_value.strip().lower()
    if normalized not in ALLOWED_RELEASE_SESSIONS:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message="release_session must use the supported session enum.",
        )
    return normalized


def _parse_cancellation(
    item: dict[str, object],
    *,
    item_type: str,
) -> dict[str, object] | None:
    raw_value = item.get("cancellation")
    if item_type != "cancellation":
        if raw_value is not None:
            raise InvestorRelationsItemError(
                reason_code=FAILURE_INVALID_CANCELLATION,
                message="cancellation facts are only allowed on cancellation items.",
            )
        return None
    if not isinstance(raw_value, dict):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_CANCELLATION,
            message="cancellation items require an explicit cancellation object.",
        )
    if set(raw_value) != {"scope", "reason_code"}:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_CANCELLATION,
            message="cancellation must contain exactly 'scope' and 'reason_code'.",
        )
    scope = raw_value["scope"]
    reason_code = raw_value["reason_code"]
    if not isinstance(scope, str) or scope.strip().lower() not in (
        ALLOWED_INVESTOR_RELATIONS_CANCELLATION_SCOPES
    ):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_CANCELLATION,
            message="cancellation scope must be event or conference_call.",
        )
    if not isinstance(reason_code, str) or not reason_code.strip():
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_CANCELLATION,
            message="cancellation reason_code must not be blank.",
        )
    normalized_reason = reason_code.strip()
    if len(normalized_reason) > 100:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_CANCELLATION,
            message="cancellation reason_code must contain at most 100 characters.",
        )
    return {"scope": scope.strip().lower(), "reason_code": normalized_reason}


def _parse_source_observed_at(item: dict[str, object]) -> datetime | None:
    raw_value = item.get("source_observed_at")
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message="source_observed_at must be an ISO-8601 string or null.",
        )
    return _parse_aware_datetime(raw_value, value_name="source_observed_at")


def _parse_confidence(item: dict[str, object]) -> Decimal | None:
    raw_value = item.get("confidence")
    if raw_value is None:
        return None
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, str)):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message="confidence must be a number between 0 and 1.",
        )
    try:
        normalized = Decimal(str(raw_value))
    except (InvalidOperation, ValueError):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message="confidence must be a number between 0 and 1.",
        ) from None
    if not normalized.is_finite() or not Decimal("0") <= normalized <= Decimal("1"):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_FACT,
            message="confidence must be a number between 0 and 1.",
        )
    return normalized.quantize(_CONFIDENCE_QUANTUM)


def _parse_source_event_identity(item: dict[str, object]) -> str | None:
    raw_value = item.get("source_event_identity")
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_SOURCE_IDENTITY,
            message="source_event_identity must be a string or null.",
        )
    normalized = raw_value.strip()
    if not normalized or len(normalized) > 255:
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_SOURCE_IDENTITY,
            message="source_event_identity must contain 1 to 255 characters.",
        )
    if normalized.startswith("internal:"):
        raise InvestorRelationsItemError(
            reason_code=FAILURE_INVALID_SOURCE_IDENTITY,
            message="provider-native source_event_identity must not use the internal namespace.",
        )
    return normalized


def _validate_item_shape(
    *,
    item_type: str,
    estimated_release: IRDateFact,
    confirmed_release: IRDateFact,
    earnings_release: IRDateFact,
    conference_call: IRDateFact,
    cancellation: dict[str, object] | None,
) -> None:
    if item_type == "release_confirmation":
        if estimated_release.is_known == confirmed_release.is_known:
            raise InvestorRelationsItemError(
                reason_code=FAILURE_INVALID_ITEM_SHAPE,
                message=(
                    "release_confirmation requires exactly one explicit "
                    "confirmed_release or estimated_release fact."
                ),
            )
        if earnings_release.is_known:
            raise InvestorRelationsItemError(
                reason_code=FAILURE_INVALID_ITEM_SHAPE,
                message="release_confirmation must not declare an earnings release fact.",
            )
    elif item_type == "results_release":
        if (
            not earnings_release.is_known
            or estimated_release.is_known
            or confirmed_release.is_known
        ):
            raise InvestorRelationsItemError(
                reason_code=FAILURE_INVALID_ITEM_SHAPE,
                message=(
                    "results_release requires exactly one explicit earnings_release fact "
                    "and no schedule facts."
                ),
            )
    elif item_type == "call_notice":
        if (
            not conference_call.is_known
            or earnings_release.is_known
            or (estimated_release.is_known and confirmed_release.is_known)
        ):
            raise InvestorRelationsItemError(
                reason_code=FAILURE_INVALID_ITEM_SHAPE,
                message=(
                    "call_notice requires an explicit conference_call fact, no earnings "
                    "release fact, and at most one release schedule fact."
                ),
            )
    elif item_type == "cancellation":
        if cancellation is None:
            raise InvestorRelationsItemError(
                reason_code=FAILURE_INVALID_ITEM_SHAPE,
                message="cancellation requires an explicit cancellation structure.",
            )
        if any(
            fact.is_known
            for fact in (
                estimated_release,
                confirmed_release,
                earnings_release,
                conference_call,
            )
        ):
            raise InvestorRelationsItemError(
                reason_code=FAILURE_INVALID_ITEM_SHAPE,
                message="cancellation items must not declare schedule or release facts.",
            )


def _require_context_text(value: object, *, value_name: str, maximum_length: int) -> str:
    if not isinstance(value, str):
        raise InvestorRelationsParserContextError(f"{value_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise InvestorRelationsParserContextError(f"{value_name} must not be empty.")
    if len(normalized) > maximum_length:
        raise InvestorRelationsParserContextError(
            f"{value_name} must contain at most {maximum_length} characters."
        )
    return normalized


def _require_text(value: object, *, value_name: str, maximum_length: int) -> str:
    try:
        return _require_context_text(
            value,
            value_name=value_name,
            maximum_length=maximum_length,
        )
    except InvestorRelationsParserContextError as error:
        raise InvestorRelationsPayloadError(str(error)) from None
