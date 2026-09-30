"""Pure, versioned parser for Alpha Vantage reference calendar CSV."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

REFERENCE_CALENDAR_PARSER_VERSION = "reference-earnings-calendar-parser-v1"
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_REQUIRED_COLUMNS = frozenset({"symbol", "reportDate", "timeOfTheDay"})
_SESSION_MAP = {
    "bmo": "pre_market",
    "pre-market": "pre_market",
    "amc": "post_market",
    "post-market": "post_market",
}


class ReferenceParseStatus(StrEnum):
    PAGE_ERROR = "PAGE_ERROR"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    EMPTY = "EMPTY"


@dataclass(frozen=True, slots=True)
class ReferenceParsedRow:
    raw_position: int
    provider_symbol: str
    company_name: str
    report_date: date
    session: str
    parser_version: str = REFERENCE_CALENDAR_PARSER_VERSION


@dataclass(frozen=True, slots=True)
class ReferenceParseResult:
    status: ReferenceParseStatus
    rows: tuple[ReferenceParsedRow, ...]
    invalid_row_count: int
    total_row_count: int
    error_code: str = ""


def parse_reference_calendar(payload: bytes) -> ReferenceParseResult:
    """Parse only reference facts; never consult a database or the clock."""

    try:
        source = io.StringIO(payload.decode("utf-8-sig"), newline="")
        reader = csv.reader(source, strict=True)
        header = next(reader)
        if len(header) != len(set(header)) or not _REQUIRED_COLUMNS.issubset(header):
            return _page_error("invalid_header")
        positions = {name: index for index, name in enumerate(header)}
        valid: list[ReferenceParsedRow] = []
        invalid_count = 0
        total_count = 0
        for raw_position, values in enumerate(reader, start=1):
            total_count += 1
            if len(values) != len(header):
                return _page_error("invalid_structure")
            symbol = values[positions["symbol"]].strip()
            date_text = values[positions["reportDate"]].strip()
            if not symbol or not _DATE_RE.fullmatch(date_text):
                invalid_count += 1
                continue
            try:
                report_date = date.fromisoformat(date_text)
            except ValueError:
                invalid_count += 1
                continue
            valid.append(
                ReferenceParsedRow(
                    raw_position=raw_position,
                    provider_symbol=symbol,
                    company_name=values[positions["name"]].strip() if "name" in positions else "",
                    report_date=report_date,
                    session=_SESSION_MAP.get(
                        values[positions["timeOfTheDay"]].strip().lower(), "unknown"
                    ),
                )
            )
    except (UnicodeDecodeError, csv.Error, StopIteration):
        return _page_error("invalid_csv")
    if total_count == 0:
        status = ReferenceParseStatus.EMPTY
    elif invalid_count:
        status = ReferenceParseStatus.PARTIAL
    else:
        status = ReferenceParseStatus.COMPLETE
    return ReferenceParseResult(status, tuple(valid), invalid_count, total_count)


def _page_error(code: str) -> ReferenceParseResult:
    return ReferenceParseResult(ReferenceParseStatus.PAGE_ERROR, (), 0, 0, code)
