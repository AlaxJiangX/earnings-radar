"""No-credential, bounded HTTPS transport for public SEC metadata endpoints."""

from __future__ import annotations

import http.client
import threading
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from providers.exceptions import ProviderResponseTooLargeError, ProviderValidationError
from providers.http import TransportRequest, TransportResponse


class SecHttpsTransport:
    _lock = threading.Lock()
    _next_request_at = 0.0

    def __init__(self, *, max_requests_per_second: int = 4) -> None:
        if isinstance(max_requests_per_second, bool) or not 1 <= max_requests_per_second <= 9:
            raise ProviderValidationError("SEC rate must be between 1 and 9 requests per second.")
        self._interval = 1.0 / max_requests_per_second

    def send(self, request: TransportRequest) -> TransportResponse:
        parts = urlsplit(request.url)
        if (
            request.method != "GET"
            or parts.scheme != "https"
            or parts.hostname not in {"data.sec.gov", "www.sec.gov"}
            or parts.port not in {None, 443}
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ProviderValidationError("SEC request must use an approved public HTTPS endpoint.")
        path = parts.path
        if not (
            (parts.hostname == "data.sec.gov" and path.startswith("/submissions/CIK"))
            or (parts.hostname == "www.sec.gov" and path.startswith("/Archives/edgar/data/"))
        ):
            raise ProviderValidationError("SEC request path is not approved.")

        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_request_at - now)
            if delay:
                time.sleep(delay)
            type(self)._next_request_at = time.monotonic() + self._interval

        connection = http.client.HTTPSConnection(
            parts.hostname, port=443, timeout=request.timeouts.connect_seconds
        )
        try:
            connection.connect()
            if connection.sock is None:
                raise OSError("SEC HTTPS connection is unavailable.")
            connection.sock.settimeout(request.timeouts.read_seconds)
            connection.request("GET", path, headers=dict(request.headers))
            response = connection.getresponse()
            declared = response.getheader("Content-Length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > request.max_response_bytes
            ):
                raise ProviderResponseTooLargeError(limit_bytes=request.max_response_bytes)
            body = response.read(request.max_response_bytes + 1)
            if len(body) > request.max_response_bytes:
                raise ProviderResponseTooLargeError(
                    limit_bytes=request.max_response_bytes, observed_bytes=len(body)
                )
            return TransportResponse(
                status_code=response.status,
                headers=dict(response.getheaders()),
                body=body,
                fetched_at=datetime.now(UTC),
            )
        except TimeoutError:
            raise TimeoutError("SEC HTTPS request timed out.") from None
        except ProviderResponseTooLargeError:
            raise
        except Exception:
            raise OSError("SEC HTTPS transport failed.") from None
        finally:
            connection.close()
