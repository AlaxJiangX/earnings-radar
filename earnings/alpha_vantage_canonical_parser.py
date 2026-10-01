"""Pure, versioned parser for Alpha Vantage canonical calendar CSV.

The parser only produces provisional provider-neutral rows.  It never resolves
Companies, generates source identities, or writes to the database.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

ALPHA_VANTAGE_CANONICAL_PARSER_VERSION = "alpha-vantage-canonical-parser-v1"

_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_REQUIRED_COLUMNS = frozenset({"symbol", "reportDate", "fiscalDateEnding", "timeOfTheDay"})
_SESSION_MAP = {
    "bmo": "pre_market",
    "pre-market": "pre_market",
    "amc": "after_market",
    "post-market": "after_market",
}


class AlphaVantageCanonicalParseStatus(StrEnum):
    PAGE_ERROR = "PAGE_ERROR"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    EMPTY = "EMPTY"


@dataclass(frozen=True, slots=True)
class AlphaVantageCanonicalRow:
    raw_position: int
    provider_symbol: str
    company_name: str
    report_date: date
    period_end_date: date
    release_session: str
    parser_version: str = ALPHA_VANTAGE_CANONICAL_PARSER_VERSION


@dataclass(frozen=True, slots=True)
class AlphaVantageCanonicalParseResult:
    status: AlphaVantageCanonicalParseStatus
    rows: tuple[AlphaVantageCanonicalRow, ...]
    invalid_row_count: int
    total_row_count: int
    error_code: str = ""


def parse_alpha_vantage_canonical_calendar(
    payload: bytes,
) -> AlphaVantageCanonicalParseResult:
    """Parse provider facts without consulting a database, clock, or network."""

    try:
        source = io.StringIO(payload.decode("utf-8-sig"), newline="")
        reader = csv.reader(source, strict=True)
        header = next(reader)
        if len(header) != len(set(header)) or not _REQUIRED_COLUMNS.issubset(header):
            return _page_error("invalid_header")
        positions = {name: index for index, name in enumerate(header)}
        valid: list[AlphaVantageCanonicalRow] = []
        invalid_count = 0
        total_count = 0
        for raw_position, values in enumerate(reader, start=1):
            total_count += 1
            if len(values) != len(header):
                return _page_error("invalid_structure")
            symbol = values[positions["symbol"]].strip()
            report_date_text = values[positions["reportDate"]].strip()
            period_end_text = values[positions["fiscalDateEnding"]].strip()
            if (
                not symbol
                or not _DATE_RE.fullmatch(report_date_text)
                or not _DATE_RE.fullmatch(period_end_text)
            ):
                invalid_count += 1
                continue
            try:
                report_date = date.fromisoformat(report_date_text)
                period_end_date = date.fromisoformat(period_end_text)
            except ValueError:
                invalid_count += 1
                continue
            session_text = values[positions["timeOfTheDay"]].strip().lower()
            valid.append(
                AlphaVantageCanonicalRow(
                    raw_position=raw_position,
                    provider_symbol=symbol,
                    company_name=(values[positions["name"]].strip() if "name" in positions else ""),
                    report_date=report_date,
                    period_end_date=period_end_date,
                    release_session=_SESSION_MAP.get(session_text, "unknown"),
                )
            )
    except (UnicodeDecodeError, csv.Error, StopIteration):
        return _page_error("invalid_csv")

    if total_count == 0:
        status = AlphaVantageCanonicalParseStatus.EMPTY
    elif invalid_count:
        status = AlphaVantageCanonicalParseStatus.PARTIAL
    else:
        status = AlphaVantageCanonicalParseStatus.COMPLETE
    return AlphaVantageCanonicalParseResult(
        status=status,
        rows=tuple(valid),
        invalid_row_count=invalid_count,
        total_row_count=total_count,
    )


def _page_error(code: str) -> AlphaVantageCanonicalParseResult:
    return AlphaVantageCanonicalParseResult(
        status=AlphaVantageCanonicalParseStatus.PAGE_ERROR,
        rows=(),
        invalid_row_count=0,
        total_row_count=0,
        error_code=code,
    )
