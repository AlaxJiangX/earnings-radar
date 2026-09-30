from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from providers.alpha_vantage_reference import (
    ALPHA_VANTAGE_REFERENCE_URL,
    AlphaVantageReferenceProvider,
)
from providers.exceptions import (
    ProviderAuthenticationError,
    ProviderPermanentError,
    ProviderRateLimitError,
    ProviderResponseTooLargeError,
    ProviderTemporaryError,
)
from providers.http import HttpTimeouts, TransportRequest, TransportResponse
from providers.live_http import BoundedHttpsTransport
from providers.types import ProviderCapability, ProviderRequest


def _request(*, limit: int = 64) -> TransportRequest:
    return TransportRequest(
        method="GET",
        url="https://www.alphavantage.co/query?function=EARNINGS_CALENDAR&horizon=3month",
        headers={"User-Agent": "EarningsRadarTest/1"},
        timeouts=HttpTimeouts(connect_seconds=2, read_seconds=3),
        max_response_bytes=limit,
    )


def test_live_transport_bounds_read_and_injects_key_only_at_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Mock()
    response.status = 200
    response.getheaders.return_value = [("Content-Type", "text/csv")]
    response.getheader.return_value = None
    response.read.return_value = b"x" * 65
    connection = Mock()
    connection.sock = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        "providers.live_http.http.client.HTTPSConnection", lambda *a, **k: connection
    )
    transport = BoundedHttpsTransport(
        allowed_host="www.alphavantage.co",
        credential_name="apikey",
        credential_value="fixture-secret",
    )
    with pytest.raises(ProviderResponseTooLargeError):
        transport.send(_request())
    assert response.read.call_args.args == (65,)
    assert "apikey=fixture-secret" in connection.request.call_args.args[1]
    assert "apikey" not in _request().url
    connection.sock.settimeout.assert_called_once_with(3)


def test_live_transport_does_not_follow_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock()
    response.status = 302
    response.getheaders.return_value = [("Location", "https://untrusted.example/path")]
    response.getheader.return_value = None
    response.read.return_value = b""
    connection = Mock()
    connection.sock = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        "providers.live_http.http.client.HTTPSConnection", lambda *a, **k: connection
    )
    transport = BoundedHttpsTransport(
        allowed_host="www.alphavantage.co", credential_name="apikey", credential_value="test-key"
    )
    result = transport.send(_request())
    assert result.status_code == 302
    assert connection.request.call_count == 1
    assert "untrusted.example" not in connection.request.call_args.args[1]


def test_live_transport_rejects_unapproved_origin() -> None:
    transport = BoundedHttpsTransport(
        allowed_host="www.alphavantage.co", credential_name="apikey", credential_value="test-key"
    )
    bad_request = TransportRequest(
        method="GET",
        url="https://untrusted.example/query",
        headers={},
        timeouts=HttpTimeouts(),
        max_response_bytes=64,
    )
    with pytest.raises(ProviderPermanentError):
        transport.send(bad_request)


def test_transport_error_cannot_expose_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Mock()
    connection.sock = Mock()
    connection.request.side_effect = ValueError("apikey=fixture-secret")
    monkeypatch.setattr(
        "providers.live_http.http.client.HTTPSConnection", lambda *a, **k: connection
    )
    transport = BoundedHttpsTransport(
        allowed_host="www.alphavantage.co",
        credential_name="apikey",
        credential_value="fixture-secret",
    )
    with pytest.raises(OSError) as error:
        transport.send(_request())
    assert "fixture-secret" not in str(error.value)


def test_alpha_vantage_429_uses_one_attempt() -> None:
    class RateLimitedTransport:
        calls = 0

        def send(self, request: TransportRequest) -> TransportResponse:
            self.calls += 1
            return TransportResponse(429, {}, b"", datetime.now(UTC))

    transport = RateLimitedTransport()
    provider = AlphaVantageReferenceProvider(api_key="fixture-key", transport=transport)
    request = ProviderRequest(
        capability=ProviderCapability.EARNINGS_CALENDAR,
        scope={"reference": True},
        request_started_at=datetime.now(UTC),
        source_url=ALPHA_VANTAGE_REFERENCE_URL,
        request_identity={"horizon": "3month"},
    )
    with pytest.raises(ProviderRateLimitError):
        provider.fetch(request)
    assert transport.calls == 1


@pytest.mark.parametrize("status", [401, 403])
def test_alpha_vantage_authentication_failure_is_terminal(status: int) -> None:
    class AuthTransport:
        calls = 0

        def send(self, request: TransportRequest) -> TransportResponse:
            self.calls += 1
            return TransportResponse(status, {}, b"", datetime.now(UTC))

    transport = AuthTransport()
    provider = AlphaVantageReferenceProvider(api_key="fixture-key", transport=transport)
    request = ProviderRequest(
        capability=ProviderCapability.EARNINGS_CALENDAR,
        scope={"reference": True},
        request_started_at=datetime.now(UTC),
        source_url=ALPHA_VANTAGE_REFERENCE_URL,
        request_identity={"horizon": "3month"},
    )
    with pytest.raises(ProviderAuthenticationError):
        provider.fetch(request)
    assert transport.calls == 1


def test_alpha_vantage_5xx_has_two_attempt_budget() -> None:
    class ServerErrorTransport:
        calls = 0

        def send(self, request: TransportRequest) -> TransportResponse:
            self.calls += 1
            return TransportResponse(503, {}, b"", datetime.now(UTC))

    transport = ServerErrorTransport()
    provider = AlphaVantageReferenceProvider(api_key="fixture-key", transport=transport)
    request = ProviderRequest(
        capability=ProviderCapability.EARNINGS_CALENDAR,
        scope={"reference": True},
        request_started_at=datetime.now(UTC),
        source_url=ALPHA_VANTAGE_REFERENCE_URL,
        request_identity={"horizon": "3month"},
    )
    with pytest.raises(ProviderTemporaryError):
        provider.fetch(request)
    assert transport.calls == 2
