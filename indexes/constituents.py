from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

ALLOWED_INDEX_CODES = frozenset({"SP500", "NASDAQ100", "DJIA", "RUSSELL2000"})


class InvalidIndexConstituentSnapshot(ValueError):
    """Raised when raw index constituent data cannot be parsed or validated.

    Error messages include field or row position context but never echo the
    full raw payload, credentials, or real external data.
    """


@dataclass(frozen=True, slots=True)
class IndexConstituentEntry:
    """A single normalized constituent in a provider-agnostic index snapshot.

    Each entry represents one security in the index as observed at the
    snapshot date.  It does *not* carry announcement dates, membership
    status, or any database foreign keys — those belong to later
    orchestration layers.
    """

    ticker: str
    exchange: str
    company_name: str
    share_class: str | None = None
    provider_security_id: str | None = None
    raw_position: int = field(default=0, compare=False)


@dataclass(frozen=True, slots=True)
class IndexConstituentSnapshot:
    """A normalized index snapshot at a single point in time.

    This is the provider-agnostic contract that every
    ``IndexConstituentProvider`` must eventually normalize into, regardless
    of the underlying JSON/XML/CSV format.

    ``entries`` are deterministically ordered by ``(ticker, exchange)``.
    """

    index_code: str
    as_of_date: date
    entries: tuple[IndexConstituentEntry, ...]


def parse_index_constituent_snapshot(
    raw_content: bytes,
    *,
    expected_index_code: str,
) -> IndexConstituentSnapshot:
    """Parse and validate raw index constituent JSON into a normalized snapshot.

    Args:
        raw_content: Raw UTF-8 JSON bytes from a ProviderResult.
        expected_index_code: The index code the caller expects to see
            (e.g. ``"SP500"``).  Must match the ``index_code`` in the JSON.

    Returns:
        A fully validated :class:`IndexConstituentSnapshot` with entries
        deterministically ordered by ``(ticker, exchange)``.

    Raises:
        InvalidIndexConstituentSnapshot: For any structural, value, or
            duplicate error.  Error messages include field/row context but
            never echo the full raw payload.
    """
    # ---- 0a. Runtime type guard -------------------------------------------
    if not isinstance(raw_content, bytes):
        raise InvalidIndexConstituentSnapshot("'raw_content' must be bytes.")

    # ---- 0b. Validate expected_index_code ---------------------------------
    _validate_expected_index_code(expected_index_code)

    # ---- 1. Parse JSON ---------------------------------------------------
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise InvalidIndexConstituentSnapshot("Index constituent data is not valid JSON.") from exc
    except UnicodeDecodeError as exc:
        raise InvalidIndexConstituentSnapshot("Index constituent data is not valid UTF-8.") from exc

    if not isinstance(data, dict):
        raise InvalidIndexConstituentSnapshot("Index constituent data must be a JSON object.")

    # ---- 2. index_code ---------------------------------------------------
    index_code = _validate_index_code(data, expected_index_code)

    # ---- 3. as_of_date ---------------------------------------------------
    as_of_date = _validate_as_of_date(data)

    # ---- 4. constituents list --------------------------------------------
    raw_constituents = data.get("constituents")
    if raw_constituents is None:
        raise InvalidIndexConstituentSnapshot("Missing required field 'constituents'.")
    if not isinstance(raw_constituents, list):
        raise InvalidIndexConstituentSnapshot("'constituents' must be a JSON array.")

    # ---- 5. Parse and validate each row ----------------------------------
    entries: list[IndexConstituentEntry] = []
    seen: set[tuple[str, str]] = set()

    for raw_position, raw_entry in enumerate(raw_constituents, start=1):
        entry = _parse_constituent_entry(
            raw_entry,
            raw_position=raw_position,
        )
        identity = (entry.ticker, entry.exchange)
        if identity in seen:
            raise InvalidIndexConstituentSnapshot(
                f"Duplicate constituent {entry.ticker}/{entry.exchange} at row {raw_position}."
            )
        seen.add(identity)
        entries.append(entry)

    # ---- 6. Deterministic sort -------------------------------------------
    entries.sort(key=lambda e: (e.ticker, e.exchange))

    return IndexConstituentSnapshot(
        index_code=index_code,
        as_of_date=as_of_date,
        entries=tuple(entries),
    )


# ---- Internal validators ------------------------------------------------


def _validate_expected_index_code(expected: str) -> None:
    if not isinstance(expected, str) or not expected.strip():
        raise InvalidIndexConstituentSnapshot("'expected_index_code' must be a non-empty string.")
    expected = expected.strip().upper()
    if expected not in ALLOWED_INDEX_CODES:
        raise InvalidIndexConstituentSnapshot(
            f"Unknown expected_index_code {expected!r}.  "
            f"Allowed: {', '.join(sorted(ALLOWED_INDEX_CODES))}."
        )


def _validate_index_code(data: dict[str, object], expected: str) -> str:
    raw = data.get("index_code")
    if raw is None:
        raise InvalidIndexConstituentSnapshot("Missing required field 'index_code'.")
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidIndexConstituentSnapshot("'index_code' must be a non-empty string.")
    code = raw.strip().upper()
    if code not in ALLOWED_INDEX_CODES:
        raise InvalidIndexConstituentSnapshot(
            f"Unknown index_code {raw!r}.  Allowed: {', '.join(sorted(ALLOWED_INDEX_CODES))}."
        )
    if code != expected.strip().upper():
        raise InvalidIndexConstituentSnapshot(
            f"index_code mismatch: expected {expected!r}, got {raw!r}."
        )
    return code


_AS_OF_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_as_of_date(data: dict[str, object]) -> date:
    raw = data.get("as_of_date")
    if raw is None:
        raise InvalidIndexConstituentSnapshot("Missing required field 'as_of_date'.")
    if not isinstance(raw, str) or not raw:
        raise InvalidIndexConstituentSnapshot("'as_of_date' must be a non-empty string.")
    if not _AS_OF_DATE_RE.fullmatch(raw):
        raise InvalidIndexConstituentSnapshot(
            f"'as_of_date' must be exactly YYYY-MM-DD, got {raw!r}."
        )
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidIndexConstituentSnapshot(
            f"'as_of_date' {raw!r} is not a valid ISO date."
        ) from exc


def _parse_constituent_entry(
    raw: object,
    *,
    raw_position: int,
) -> IndexConstituentEntry:
    if not isinstance(raw, dict):
        raise InvalidIndexConstituentSnapshot(
            f"Constituent at row {raw_position} must be a JSON object."
        )

    ticker = _require_nonempty_str(raw, "ticker", raw_position)
    ticker = ticker.strip().upper()

    exchange = _require_nonempty_str(raw, "exchange", raw_position)
    exchange = exchange.strip().upper()

    company_name = _require_nonempty_str(raw, "company_name", raw_position)
    company_name = company_name.strip()

    share_class = _optional_str_or_none(raw, "share_class", raw_position)
    provider_security_id = _optional_str_or_none(raw, "provider_security_id", raw_position)

    return IndexConstituentEntry(
        ticker=ticker,
        exchange=exchange,
        company_name=company_name,
        share_class=share_class,
        provider_security_id=provider_security_id,
        raw_position=raw_position,
    )


def _require_nonempty_str(
    raw: dict[str, object],
    key: str,
    raw_position: int,
) -> str:
    value = raw.get(key)
    if value is None:
        raise InvalidIndexConstituentSnapshot(
            f"Missing required field {key!r} at row {raw_position}."
        )
    if not isinstance(value, str) or not value.strip():
        raise InvalidIndexConstituentSnapshot(
            f"Field {key!r} must be a non-empty string at row {raw_position}."
        )
    return value


def _optional_str_or_none(
    raw: dict[str, object],
    key: str,
    raw_position: int,
) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    raise InvalidIndexConstituentSnapshot(
        f"Field {key!r} must be a string or null at row {raw_position}."
    )
