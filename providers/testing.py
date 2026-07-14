from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum

from providers.base import Provider
from providers.exceptions import ProviderValidationError
from providers.http import (
    HttpClientConfig,
    ProviderHttpClient,
    RetryPolicy,
    TransportRequest,
    TransportResponse,
)
from providers.types import ProviderCapability, ProviderRequest, ProviderResult

FIXTURE_REQUEST_STARTED_AT = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
FIXTURE_FETCHED_AT = datetime(2026, 7, 14, 12, 0, 1, tzinfo=UTC)


class FakeProviderScenario(StrEnum):
    SUCCESS = "success"
    EMPTY = "empty"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    NOT_FOUND = "not_found"
    AUTHENTICATION_ERROR = "authentication_error"
    INVALID_JSON = "invalid_json"
    RESPONSE_TOO_LARGE = "response_too_large"
    SENSITIVE_URL = "sensitive_url"
    SENSITIVE_RESPONSE = "sensitive_response"
    SENSITIVE_CONTENT_TYPE = "sensitive_content_type"
    TRANSPORT_ERROR_WITH_TOKEN = "transport_error_with_token"


class FakeHttpTransport:
    def __init__(self, scenario: FakeProviderScenario) -> None:
        self.scenario = scenario
        self.call_count = 0
        self.last_connect_timeout_seconds: float | None = None
        self.last_read_timeout_seconds: float | None = None
        self.last_max_response_bytes: int | None = None
        self.last_user_agent = ""

    def send(self, request: TransportRequest) -> TransportResponse:
        self.call_count += 1
        self.last_connect_timeout_seconds = request.timeouts.connect_seconds
        self.last_read_timeout_seconds = request.timeouts.read_seconds
        self.last_max_response_bytes = request.max_response_bytes
        self.last_user_agent = request.headers.get("User-Agent", "")

        if self.scenario is FakeProviderScenario.TIMEOUT:
            raise TimeoutError("Authorization: Bearer fixture-timeout-token")
        if self.scenario is FakeProviderScenario.TRANSPORT_ERROR_WITH_TOKEN:
            raise OSError("api_key=fixture-transport-token")

        status_code = 200
        headers: dict[str, str] = {"Content-Type": "application/json"}
        body = b'{"items":[{"company":"Example Test Corp","symbol":"FAKE"}]}'

        if self.scenario is FakeProviderScenario.EMPTY:
            status_code = 204
            headers = {}
            body = b""
        elif self.scenario is FakeProviderScenario.RATE_LIMIT:
            status_code = 429
            headers["Retry-After"] = "60"
            body = b'{"error":"fixture rate limit"}'
        elif self.scenario is FakeProviderScenario.SERVER_ERROR:
            status_code = 500
            body = b'{"error":"fixture temporary failure"}'
        elif self.scenario is FakeProviderScenario.NOT_FOUND:
            status_code = 404
            body = b'{"error":"fixture not found"}'
        elif self.scenario is FakeProviderScenario.AUTHENTICATION_ERROR:
            status_code = 401
            body = b'{"error":"fixture authentication failure"}'
        elif self.scenario is FakeProviderScenario.INVALID_JSON:
            body = b"{invalid-json"
        elif self.scenario is FakeProviderScenario.RESPONSE_TOO_LARGE:
            body = b"x" * (request.max_response_bytes + 1)
            headers["Content-Length"] = str(len(body))
        elif self.scenario is FakeProviderScenario.SENSITIVE_RESPONSE:
            body = b'{"headers":{"Authorization":"Bearer fixture-response-token"}}'
        elif self.scenario is FakeProviderScenario.SENSITIVE_CONTENT_TYPE:
            headers["Content-Type"] = "Bearer fixture-content-type-token"

        return TransportResponse(
            status_code=status_code,
            headers=headers,
            body=body,
            fetched_at=FIXTURE_FETCHED_AT,
        )


class FakeProvider(Provider):
    provider_key = "fixture-provider"
    provider_version = "fixture-v1"
    capabilities = frozenset(ProviderCapability)

    def __init__(
        self,
        scenario: FakeProviderScenario = FakeProviderScenario.SUCCESS,
        *,
        api_key: str = "fixture-api-key-not-a-secret",
        config: HttpClientConfig | None = None,
    ) -> None:
        self.scenario = scenario
        self.api_key = api_key
        self.transport = FakeHttpTransport(scenario)
        deterministic_config = config or HttpClientConfig(
            retry_policy=RetryPolicy(
                max_attempts=1,
                base_delay_seconds=0,
                max_delay_seconds=0,
            )
        )
        self.http_client = ProviderHttpClient(
            transport=self.transport,
            config=deterministic_config,
            sleeper=lambda _seconds: None,
        )

    def _fetch(self, request: ProviderRequest) -> ProviderResult:
        result = self.http_client.fetch(
            provider_key=self.provider_key,
            provider_version=self.provider_version,
            request=request,
            headers={"Accept": "application/json", "X-API-Key": self.api_key},
            metadata={"fixture_scenario": self.scenario.value},
        )
        if result.raw_content:
            try:
                parsed = json.loads(result.raw_content)
            except json.JSONDecodeError:
                raise ProviderValidationError(
                    "Provider response does not contain valid JSON."
                ) from None
            if not isinstance(parsed, dict):
                raise ProviderValidationError("Provider response JSON must be an object.")
        return result


def make_fake_provider_request(
    *,
    capability: ProviderCapability = ProviderCapability.EARNINGS_CALENDAR,
    page: int = 1,
    api_key_in_url: str | None = None,
    source_url: str | None = None,
    request_started_at: datetime = FIXTURE_REQUEST_STARTED_AT,
) -> ProviderRequest:
    if source_url is None:
        source_url = f"https://provider.example.test/{capability.value}?page={page}"
        if api_key_in_url is not None:
            source_url = f"{source_url}&api_key={api_key_in_url}"
    return ProviderRequest(
        capability=capability,
        scope={"fixture": True, "page": page},
        request_started_at=request_started_at,
        source_url=source_url,
        request_identity={"page": page},
    )
