from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from audit.constants import RAW_DATA_PAYLOAD_DB_LIMIT_BYTES
from filings.parsing import SecMetadataError, parse_filing_index, parse_submissions
from providers.exceptions import (
    ProviderRateLimitError,
    ProviderResponseTooLargeError,
    ProviderTemporaryError,
    ProviderTimeoutError,
    ProviderValidationError,
)
from providers.http import HttpTimeouts, TransportRequest, TransportResponse
from providers.sec_edgar import SecEdgarProvider, filing_index_request, submissions_request
from providers.sec_http import SecHttpsTransport

NOW = datetime(2026, 10, 1, tzinfo=UTC)
CIK = "0000001234"
ACCESSION = "0000001234-26-000001"


def submissions_body(*, form: str = "10-Q", accession: str = ACCESSION) -> bytes:
    return json.dumps(
        {
            "cik": 1234,
            "filings": {
                "recent": {
                    "accessionNumber": [accession, "0000001234-26-000002"],
                    "form": [form, "4"],
                    "acceptanceDateTime": ["2026-03-09T16:30:00", "not-a-date"],
                    "reportDate": ["", ""],
                    "primaryDocument": ["quarter.htm", "ignored.htm"],
                }
            },
        }
    ).encode()


def index_body() -> bytes:
    return json.dumps(
        {
            "directory": {
                "name": "/Archives/edgar/data/1234/000000123426000001",
                "item": [
                    {"name": "quarter.htm", "type": "text/html"},
                    {"name": "exhibit.htm", "type": "text/html"},
                ],
            }
        }
    ).encode()


class FakeTransport:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.requests: list[TransportRequest] = []

    def send(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        status = self.statuses.pop(0)
        return TransportResponse(
            status_code=status,
            headers={"Content-Type": "application/json"},
            body=submissions_body(),
            fetched_at=NOW,
        )


def test_canonical_sec_request_and_identifiable_user_agent_without_credential() -> None:
    request = submissions_request(cik=CIK, started_at=NOW)
    assert request.source_url == "https://data.sec.gov/submissions/CIK0000001234.json"
    assert filing_index_request(
        cik=CIK, accession_number=ACCESSION, started_at=NOW
    ).source_url.endswith("/1234/000000123426000001/index.json")
    transport = FakeTransport([200])
    provider = SecEdgarProvider(user_agent="Earnings Radar test@example.org", transport=transport)
    result = provider.fetch(request)
    assert result.raw_content == submissions_body()
    assert transport.requests[0].headers == {
        "Accept": "application/json",
        "User-Agent": "Earnings Radar test@example.org",
    }
    with pytest.raises(ProviderValidationError):
        SecEdgarProvider(user_agent="")
    with pytest.raises(ProviderValidationError):
        SecEdgarProvider(user_agent="Earnings Radar")
    with pytest.raises(ProviderValidationError):
        SecEdgarProvider(user_agent="test@example.org")
    with pytest.raises(ProviderValidationError):
        SecEdgarProvider(user_agent="Earnings Radar test@example.org", max_requests_per_second=10)


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_sec_temporary_restrictions_have_finite_retry(status: int) -> None:
    transport = FakeTransport([status, 200])
    provider = SecEdgarProvider(user_agent="Earnings Radar test@example.org", transport=transport)
    delays: list[float] = []
    provider._http._sleeper = delays.append
    provider.fetch(submissions_request(cik=CIK, started_at=NOW))
    assert len(transport.requests) == 2
    assert delays == [2.0]


@pytest.mark.parametrize(
    "status,error_type", [(403, ProviderRateLimitError), (500, ProviderTemporaryError)]
)
def test_sec_retries_stop_after_two_attempts(status: int, error_type: type[Exception]) -> None:
    transport = FakeTransport([status, status])
    provider = SecEdgarProvider(user_agent="Earnings Radar test@example.org", transport=transport)
    provider._http._sleeper = lambda _delay: None
    with pytest.raises(error_type):
        provider.fetch(submissions_request(cik=CIK, started_at=NOW))
    assert len(transport.requests) == 2


def test_sec_timeout_retries_once_then_fails() -> None:
    class TimeoutTransport:
        attempts = 0

        def send(self, _request: TransportRequest) -> TransportResponse:
            self.attempts += 1
            raise TimeoutError("secret details should not surface")

    transport = TimeoutTransport()
    provider = SecEdgarProvider(user_agent="Earnings Radar test@example.org", transport=transport)
    provider._http._sleeper = lambda _delay: None
    with pytest.raises(ProviderTimeoutError, match="Provider request timed out"):
        provider.fetch(submissions_request(cik=CIK, started_at=NOW))
    assert transport.attempts == 2


@pytest.mark.parametrize(
    "size,too_large",
    [(RAW_DATA_PAYLOAD_DB_LIMIT_BYTES, False), (RAW_DATA_PAYLOAD_DB_LIMIT_BYTES + 1, True)],
)
def test_sec_transport_reads_limit_plus_one_without_truncation(
    monkeypatch: pytest.MonkeyPatch, size: int, too_large: bool
) -> None:
    body = b"x" * size
    reads: list[int] = []

    class FakeResponse:
        status = 200

        def getheader(self, _name: str) -> None:
            return None

        def getheaders(self) -> list[tuple[str, str]]:
            return []

        def read(self, amount: int) -> bytes:
            reads.append(amount)
            return body[:amount]

    class FakeConnection:
        sock = SimpleNamespace(settimeout=lambda _value: None)

        def __init__(self, _host: str, *, port: int, timeout: float) -> None:
            assert port == 443
            assert timeout == 5.0

        def connect(self) -> None:
            pass

        def request(self, _method: str, _path: str, *, headers: object) -> None:
            assert headers == {"User-Agent": "Earnings Radar test@example.org"}

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr("providers.sec_http.http.client.HTTPSConnection", FakeConnection)
    monkeypatch.setattr("providers.sec_http.time.sleep", lambda _delay: None)
    transport = SecHttpsTransport(max_requests_per_second=9)
    request = TransportRequest(
        method="GET",
        url=submissions_request(cik=CIK, started_at=NOW).source_url,
        headers={"User-Agent": "Earnings Radar test@example.org"},
        timeouts=HttpTimeouts(),
        max_response_bytes=RAW_DATA_PAYLOAD_DB_LIMIT_BYTES,
    )
    if too_large:
        with pytest.raises(ProviderResponseTooLargeError):
            transport.send(request)
    else:
        assert transport.send(request).body == body
    assert reads == [RAW_DATA_PAYLOAD_DB_LIMIT_BYTES + 1]


def test_sec_parser_filters_forms_and_uses_eastern_time_with_dst() -> None:
    rows = parse_submissions(submissions_body(), cik=CIK)
    assert len(rows) == 1
    assert rows[0].accession_number == ACCESSION
    assert rows[0].period_of_report is None
    assert rows[0].accepted_at == datetime(2026, 3, 9, 20, 30, tzinfo=UTC)
    assert rows[0].filing_url.endswith("/1234/000000123426000001/quarter.htm")
    documents = parse_filing_index(
        index_body(), cik=CIK, accession_number=ACCESSION, primary_document="quarter.htm"
    )
    assert {document.filename for document in documents} == {"quarter.htm", "exhibit.htm"}
    assert all(document.url.startswith("https://www.sec.gov/Archives/") for document in documents)


def test_non_target_forms_do_not_create_filing_metadata() -> None:
    assert parse_submissions(submissions_body(form="4"), cik=CIK) == ()


def test_sec_parser_rejects_bad_target_accession_and_bad_index_identity() -> None:
    with pytest.raises(SecMetadataError):
        parse_submissions(submissions_body(accession="bad"), cik=CIK)
    with pytest.raises(SecMetadataError):
        parse_submissions(submissions_body(), cik="0000009999")
    with pytest.raises(SecMetadataError):
        parse_filing_index(
            index_body(), cik=CIK, accession_number=ACCESSION, primary_document="absent.htm"
        )
