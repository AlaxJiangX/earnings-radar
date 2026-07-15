from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from indexes.constituents import (
    ALLOWED_INDEX_CODES,
    IndexConstituentEntry,
    IndexConstituentSnapshot,
    InvalidIndexConstituentSnapshot,
    parse_index_constituent_snapshot,
)
from providers.base import Provider
from providers.exceptions import ProviderValidationError
from providers.testing import (
    FIXTURE_REQUEST_STARTED_AT,
    FixtureIndexConstituentProvider,
    make_fake_provider_request,
)
from providers.types import ProviderCapability, ProviderRequest

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "providers" / "index_constituents"


def _read_fixture(index_code: str) -> bytes:
    return (FIXTURE_DIR / f"{index_code.lower()}.json").read_bytes()


def _load_fixtures() -> dict[str, bytes]:
    """Load all four fixture JSON files into a mapping for the Provider."""
    return {code: _read_fixture(code) for code in ALLOWED_INDEX_CODES}


def _make_index_request(index_code: str) -> ProviderRequest:
    return ProviderRequest(
        capability=ProviderCapability.INDEX_CONSTITUENTS,
        scope={"index_code": index_code},
        request_started_at=FIXTURE_REQUEST_STARTED_AT,
        source_url=(f"https://fixture-index.test/{index_code}/constituents?date=2026-07-15"),
        request_identity={"index_code": index_code},
    )


_ENTRY_COUNTS = {"SP500": 4, "NASDAQ100": 3, "DJIA": 3, "RUSSELL2000": 5}


@pytest.mark.parametrize("index_code", sorted(ALLOWED_INDEX_CODES))
def test_parse_fixture_returns_valid_snapshot(index_code: str) -> None:
    raw = _read_fixture(index_code)
    snapshot = parse_index_constituent_snapshot(raw, expected_index_code=index_code)
    assert isinstance(snapshot, IndexConstituentSnapshot)
    assert snapshot.index_code == index_code
    assert isinstance(snapshot.as_of_date, date)
    assert snapshot.as_of_date == date(2026, 7, 15)
    assert isinstance(snapshot.entries, tuple)
    assert len(snapshot.entries) == _ENTRY_COUNTS[index_code]
    for entry in snapshot.entries:
        assert isinstance(entry, IndexConstituentEntry)
        assert entry.ticker and entry.ticker.isupper()
        assert entry.exchange and entry.exchange.isupper()
        assert entry.company_name
        assert entry.share_class is None or isinstance(entry.share_class, str)
        assert entry.provider_security_id is None or isinstance(entry.provider_security_id, str)
        assert isinstance(entry.raw_position, int) and entry.raw_position >= 1


@pytest.mark.parametrize("index_code", sorted(ALLOWED_INDEX_CODES))
def test_same_bytes_parsed_twice_produces_equal_snapshots(index_code: str) -> None:
    raw = _read_fixture(index_code)
    s1 = parse_index_constituent_snapshot(raw, expected_index_code=index_code)
    s2 = parse_index_constituent_snapshot(raw, expected_index_code=index_code)
    assert s1 == s2
    assert hash(s1) == hash(s2)


class TestFixtureIndexConstituentProvider:
    def test_satisfies_provider_contract(self) -> None:
        provider = FixtureIndexConstituentProvider(fixtures=_load_fixtures())
        assert isinstance(provider, Provider)
        assert provider.provider_key == "fixture-index-constituents"
        assert provider.provider_version == "fixture-v1"
        assert provider.capabilities == frozenset({ProviderCapability.INDEX_CONSTITUENTS})

    def test_rejects_unsupported_capability(self) -> None:
        provider = FixtureIndexConstituentProvider(fixtures=_load_fixtures())
        request = make_fake_provider_request(capability=ProviderCapability.EARNINGS_CALENDAR)
        with pytest.raises(ProviderValidationError, match="does not support"):
            provider.fetch(request)

    @pytest.mark.parametrize("index_code", sorted(ALLOWED_INDEX_CODES))
    def test_returns_valid_provider_result(self, index_code: str) -> None:
        provider = FixtureIndexConstituentProvider(fixtures=_load_fixtures())
        result = provider.fetch(_make_index_request(index_code))
        assert result.provider_key == provider.provider_key
        assert result.capability == ProviderCapability.INDEX_CONSTITUENTS
        assert result.http_status == 200
        assert isinstance(result.raw_content, bytes)
        assert len(result.raw_content) > 0

    def test_rejects_missing_index_code_in_scope(self) -> None:
        provider = FixtureIndexConstituentProvider(fixtures=_load_fixtures())
        request = ProviderRequest(
            capability=ProviderCapability.INDEX_CONSTITUENTS,
            scope={},
            request_started_at=FIXTURE_REQUEST_STARTED_AT,
            source_url="https://fixture-index.test/no-scope",
        )
        with pytest.raises(ProviderValidationError, match="index_code"):
            provider.fetch(request)

    def test_raw_content_is_parseable_json(self) -> None:
        provider = FixtureIndexConstituentProvider(fixtures=_load_fixtures())
        result = provider.fetch(_make_index_request("SP500"))
        parsed = json.loads(result.raw_content)
        assert isinstance(parsed, dict)
        assert parsed["index_code"] == "SP500"


# ---------------------------------------------------------------------------
# Parser — validation edge cases
# ---------------------------------------------------------------------------


class TestParserNonBytes:
    @pytest.mark.parametrize(
        "raw_content",
        [
            "not-bytes",
            bytearray(b"{}"),
            memoryview(b"{}"),
        ],
    )
    def test_rejects_non_bytes_raw_content(self, raw_content: object) -> None:
        with pytest.raises(InvalidIndexConstituentSnapshot, match="must be bytes"):
            _parse_bytes_guard(raw_content, "SP500")


class TestParserInvalidJson:
    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(InvalidIndexConstituentSnapshot, match="JSON"):
            parse_index_constituent_snapshot(b"{invalid", expected_index_code="SP500")

    def test_rejects_non_utf8_bytes(self) -> None:
        raw = b'{"index_code":"SP500"}\xff\xfe'
        with pytest.raises(InvalidIndexConstituentSnapshot, match="UTF-8"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")


class TestParserRootStructure:
    def test_rejects_non_object(self) -> None:
        with pytest.raises(InvalidIndexConstituentSnapshot, match="object"):
            parse_index_constituent_snapshot(b"42", expected_index_code="SP500")


class TestParserIndexCode:
    def test_rejects_missing(self) -> None:
        raw = b'{"as_of_date":"2026-07-15","constituents":[]}'
        with pytest.raises(InvalidIndexConstituentSnapshot, match="index_code"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_rejects_unknown(self) -> None:
        raw = b'{"index_code":"FTSE100","as_of_date":"2026-07-15","constituents":[]}'
        with pytest.raises(InvalidIndexConstituentSnapshot, match="Unknown"):
            parse_index_constituent_snapshot(raw, expected_index_code="FTSE100")

    def test_rejects_expected_mismatch(self) -> None:
        raw = b'{"index_code":"NASDAQ100","as_of_date":"2026-07-15","constituents":[]}'
        with pytest.raises(InvalidIndexConstituentSnapshot, match="mismatch"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")


class TestParserAsOfDate:
    def test_rejects_missing(self) -> None:
        raw = b'{"index_code":"SP500","constituents":[]}'
        with pytest.raises(InvalidIndexConstituentSnapshot, match="as_of_date"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_rejects_illegal_iso(self) -> None:
        raw = b'{"index_code":"SP500","as_of_date":"not-a-date","constituents":[]}'
        with pytest.raises(InvalidIndexConstituentSnapshot, match="YYYY-MM-DD"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    @pytest.mark.parametrize(
        "as_of_date",
        [
            " 2026-07-15",
            "2026-07-15 ",
            "20260715",
            "2026-W29-3",
            "2026-07-15T00:00:00",
        ],
    )
    def test_rejects_non_exact_date_format(self, as_of_date: str) -> None:
        raw = json.dumps(
            {
                "index_code": "SP500",
                "as_of_date": as_of_date,
                "constituents": [],
            }
        ).encode()
        with pytest.raises(InvalidIndexConstituentSnapshot, match="YYYY-MM-DD"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_accepts_exact_calendar_date(self) -> None:
        raw = b'{"index_code":"SP500","as_of_date":"2026-07-15","constituents":[]}'
        snapshot = parse_index_constituent_snapshot(raw, expected_index_code="SP500")
        assert snapshot.as_of_date == date(2026, 7, 15)


class TestParserConstituentsList:
    def test_rejects_missing(self) -> None:
        raw = b'{"index_code":"SP500","as_of_date":"2026-07-15"}'
        with pytest.raises(InvalidIndexConstituentSnapshot, match="constituents"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_rejects_not_a_list(self) -> None:
        raw = b'{"index_code":"SP500","as_of_date":"2026-07-15","constituents":"nope"}'
        with pytest.raises(InvalidIndexConstituentSnapshot, match="array"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")


class TestParserConstituentRows:
    def test_rejects_row_not_object(self) -> None:
        raw = b'{"index_code":"SP500","as_of_date":"2026-07-15","constituents":["not-an-object"]}'
        with pytest.raises(InvalidIndexConstituentSnapshot, match="row 1"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_rejects_missing_ticker(self) -> None:
        raw = (
            b'{"index_code":"SP500","as_of_date":"2026-07-15",'
            b'"constituents":[{"exchange":"NYSE","company_name":"X"}]}'
        )
        with pytest.raises(InvalidIndexConstituentSnapshot, match="ticker"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_rejects_missing_exchange(self) -> None:
        raw = (
            b'{"index_code":"SP500","as_of_date":"2026-07-15",'
            b'"constituents":[{"ticker":"A","company_name":"X"}]}'
        )
        with pytest.raises(InvalidIndexConstituentSnapshot, match="exchange"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_rejects_missing_company_name(self) -> None:
        raw = (
            b'{"index_code":"SP500","as_of_date":"2026-07-15",'
            b'"constituents":[{"ticker":"A","exchange":"NYSE"}]}'
        )
        with pytest.raises(InvalidIndexConstituentSnapshot, match="company_name"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_rejects_non_string_share_class(self) -> None:
        raw = (
            b'{"index_code":"SP500","as_of_date":"2026-07-15",'
            b'"constituents":[{"ticker":"A","exchange":"NYSE",'
            b'"company_name":"X","share_class":42}]}'
        )
        with pytest.raises(InvalidIndexConstituentSnapshot, match="share_class"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_rejects_non_string_provider_security_id(self) -> None:
        raw = (
            b'{"index_code":"SP500","as_of_date":"2026-07-15",'
            b'"constituents":[{"ticker":"A","exchange":"NYSE",'
            b'"company_name":"X","provider_security_id":42}]}'
        )
        with pytest.raises(InvalidIndexConstituentSnapshot, match="provider_security_id"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")


class TestParserDuplicate:
    def test_rejects_duplicate_ticker_exchange(self) -> None:
        raw = (
            b'{"index_code":"SP500","as_of_date":"2026-07-15",'
            b'"constituents":['
            b'{"ticker":"ALPH","exchange":"NYSE","company_name":"A"},'
            b'{"ticker":"ALPH","exchange":"NYSE","company_name":"B"}'
            b"]}"
        )
        with pytest.raises(InvalidIndexConstituentSnapshot, match="Duplicate"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_same_ticker_exchange_different_share_classes_is_duplicate(
        self,
    ) -> None:
        raw = (
            b'{"index_code":"SP500",'
            b'"as_of_date":"2026-07-15",'
            b'"constituents":['
            b'{"ticker":"ALPH","exchange":"NYSE",'
            b'"company_name":"Alpha Class A","share_class":"A"},'
            b'{"ticker":"ALPH","exchange":"NYSE",'
            b'"company_name":"Alpha Class B","share_class":"B"}'
            b"]}"
        )
        with pytest.raises(InvalidIndexConstituentSnapshot, match="Duplicate"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")

    def test_case_normalized_duplicate(self) -> None:
        raw = (
            b'{"index_code":"SP500","as_of_date":"2026-07-15",'
            b'"constituents":['
            b'{"ticker":"alph","exchange":"nyse","company_name":"A"},'
            b'{"ticker":"ALPH","exchange":"NYSE","company_name":"B"}'
            b"]}"
        )
        with pytest.raises(InvalidIndexConstituentSnapshot, match="Duplicate"):
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")


class TestParserEmptyConstituents:
    def test_empty_passes(self) -> None:
        raw = b'{"index_code":"SP500","as_of_date":"2026-07-15","constituents":[]}'
        snapshot = parse_index_constituent_snapshot(raw, expected_index_code="SP500")
        assert snapshot.entries == ()
        assert snapshot.index_code == "SP500"

    def test_empty_is_deterministic(self) -> None:
        raw = b'{"index_code":"SP500","as_of_date":"2026-07-15","constituents":[]}'
        s1 = parse_index_constituent_snapshot(raw, expected_index_code="SP500")
        s2 = parse_index_constituent_snapshot(raw, expected_index_code="SP500")
        assert s1 == s2


class TestParserDeterministicOutput:
    def test_input_order_does_not_change_output(self) -> None:
        raw = (
            b'{"index_code":"SP500","as_of_date":"2026-07-15",'
            b'"constituents":['
            b'{"ticker":"DELT","exchange":"NASDAQ","company_name":"D"},'
            b'{"ticker":"ALPH","exchange":"NYSE","company_name":"A"}'
            b"]}"
        )
        snapshot = parse_index_constituent_snapshot(raw, expected_index_code="SP500")
        assert snapshot.entries[0].ticker == "ALPH"
        assert snapshot.entries[1].ticker == "DELT"
        assert snapshot.entries[0].raw_position == 2
        assert snapshot.entries[1].raw_position == 1


class TestParserDoesNotModifyRaw:
    def test_raw_bytes_unchanged_after_parse(self) -> None:
        raw = _read_fixture("SP500")
        raw_copy = raw[:]
        parse_index_constituent_snapshot(raw, expected_index_code="SP500")
        assert raw == raw_copy


class TestAllowedIndexCodeDrift:
    def test_constituent_codes_match_market_index_codes(self) -> None:
        from indexes.models import ALLOWED_CODES

        assert ALLOWED_INDEX_CODES == ALLOWED_CODES


class TestSecurity:
    def test_fixture_data_is_fictional(self) -> None:
        real_tickers = {"AAPL", "MSFT", "GOOGL", "NVDA", "TSLA", "AMZN", "META", "JPM"}
        for index_code in ALLOWED_INDEX_CODES:
            raw = _read_fixture(index_code)
            snapshot = parse_index_constituent_snapshot(raw, expected_index_code=index_code)
            for entry in snapshot.entries:
                assert entry.ticker not in real_tickers
                assert (
                    entry.provider_security_id is None or "fixture-" in entry.provider_security_id
                )

    def test_fixtures_contain_no_credentials(self) -> None:
        for index_code in ALLOWED_INDEX_CODES:
            text = _read_fixture(index_code).decode("utf-8")
            assert "api_key" not in text.lower()
            assert "Authorization" not in text
            assert "Bearer" not in text

    def test_fixtures_contain_no_credential_keys(self) -> None:
        sensitive_keys = {"password", "token", "api_key", "authorization", "cookie", "secret"}
        import json

        for index_code in ALLOWED_INDEX_CODES:
            text = _read_fixture(index_code).decode("utf-8")
            data = json.loads(text)
            _scan_keys(data, sensitive_keys, ())

    def test_error_does_not_echo_full_payload(self) -> None:
        raw = json.dumps(
            {
                "index_code": "SP500",
                "as_of_date": "not-a-date",
                "constituents": [{"ticker": "A", "exchange": "NYSE", "company_name": "X" * 2000}],
            }
        ).encode()
        with pytest.raises(InvalidIndexConstituentSnapshot) as exc_info:
            parse_index_constituent_snapshot(raw, expected_index_code="SP500")
        assert "X" * 2000 not in str(exc_info.value)


def _parse_bytes_guard(raw: object, expected_index_code: str) -> None:
    """Intentionally bypass the static bytes annotation to exercise the runtime
    isinstance guard in parse_index_constituent_snapshot."""
    parse_index_constituent_snapshot(raw, expected_index_code=expected_index_code)  # type: ignore[arg-type]


def _scan_keys(obj: object, forbidden: set[str], path: tuple[str, ...]) -> None:
    if isinstance(obj, dict):
        for key in obj:
            if any(fob in key.lower() for fob in forbidden):
                raise AssertionError(f"Credential-like key {key!r} found at {'.'.join(path)}")
            _scan_keys(obj[key], forbidden, (*path, key))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _scan_keys(item, forbidden, (*path, str(i)))
