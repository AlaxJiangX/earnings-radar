"""Small HTTPS transport with bounded reads and send-boundary query credentials."""

from __future__ import annotations

import http.client
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

from providers.exceptions import ProviderResponseTooLargeError, ProviderValidationError
from providers.http import TransportRequest, TransportResponse


class BoundedHttpsTransport:
    """Send to one approved HTTPS host; never follow a redirect."""

    def __init__(
        self,
        *,
        allowed_host: str,
        credential_name: str,
        credential_value: str,
    ) -> None:
        if not allowed_host or not credential_name or not credential_value:
            raise ProviderValidationError("HTTPS transport configuration is incomplete.")
        self._host = allowed_host.lower()
        self._credential_name = credential_name
        self._credential_value = credential_value

    def send(self, request: TransportRequest) -> TransportResponse:
        try:
            parts = urlsplit(request.url)
            if (
                parts.scheme != "https"
                or parts.hostname != self._host
                or parts.port not in (None, 443)
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
                or request.method != "GET"
            ):
                raise ProviderValidationError("Provider request has an unapproved HTTPS origin.")
            query = parse_qsl(parts.query, keep_blank_values=True)
            if any(name.lower() == self._credential_name.lower() for name, _ in query):
                raise ProviderValidationError("Provider request already contains a credential.")
        except ValueError:
            raise ProviderValidationError("Provider request URL is invalid.") from None

        query.append((self._credential_name, self._credential_value))
        target = (parts.path or "/") + "?" + urlencode(query)
        connection = http.client.HTTPSConnection(
            self._host, port=443, timeout=request.timeouts.connect_seconds
        )
        try:
            connection.connect()
            if connection.sock is None:
                raise OSError("Provider HTTPS connection is unavailable.")
            connection.sock.settimeout(request.timeouts.read_seconds)
            connection.request(request.method, target, headers=dict(request.headers))
            response = connection.getresponse()
            headers = dict(response.getheaders())
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
                headers=headers,
                body=body,
                fetched_at=datetime.now(UTC),
            )
        except TimeoutError:
            raise TimeoutError("Provider HTTPS request timed out.") from None
        except ProviderResponseTooLargeError:
            raise
        except Exception:
            raise OSError("Provider HTTPS transport failed.") from None
        finally:
            connection.close()
