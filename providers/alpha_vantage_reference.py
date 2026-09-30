"""Alpha Vantage Free market-wide reference calendar adapter."""

from __future__ import annotations

import os

from providers.base import Provider
from providers.exceptions import ProviderValidationError
from providers.http import HttpClientConfig, HttpTransport, ProviderHttpClient, RetryPolicy
from providers.live_http import BoundedHttpsTransport
from providers.types import ProviderCapability, ProviderRequest, ProviderResult

ALPHA_VANTAGE_REFERENCE_URL = (
    "https://www.alphavantage.co/query?function=EARNINGS_CALENDAR&horizon=3month"
)


class AlphaVantageReferenceProvider(Provider):
    provider_key = "alpha-vantage-free"
    provider_version = "alpha-vantage-reference-v1"
    capabilities = frozenset({ProviderCapability.EARNINGS_CALENDAR})

    def __init__(self, *, api_key: str, transport: HttpTransport | None = None) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderValidationError("ALPHA_VANTAGE_API_KEY is required.")
        self._http = ProviderHttpClient(
            transport=transport
            or BoundedHttpsTransport(
                allowed_host="www.alphavantage.co",
                credential_name="apikey",
                credential_value=api_key,
            ),
            config=HttpClientConfig(
                retry_policy=RetryPolicy(max_attempts=2, retry_rate_limits=False)
            ),
        )

    @classmethod
    def from_environment(cls) -> AlphaVantageReferenceProvider:
        return cls(api_key=os.environ.get("ALPHA_VANTAGE_API_KEY", ""))

    def _fetch(self, request: ProviderRequest) -> ProviderResult:
        if request.source_url != ALPHA_VANTAGE_REFERENCE_URL or request.method != "GET":
            raise ProviderValidationError("Alpha Vantage reference request must be market-wide.")
        if request.request_identity != {"horizon": "3month"}:
            raise ProviderValidationError("Alpha Vantage reference request identity is invalid.")
        return self._http.fetch(
            provider_key=self.provider_key,
            provider_version=self.provider_version,
            request=request,
        )
