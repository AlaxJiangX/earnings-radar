"""Zero-network fixture Investor Relations provider (Stage 4.5B).

This provider is intentionally limited to synthetic payloads injected by the
test/orchestration caller.  It never performs a real network request, never
knows any real company IR URL, and never writes database rows.  Live IR
ingestion remains BLOCKED (ADR-022) until the per-source legal checklist is
approved and a separate live transport is implemented.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

from providers.base import Provider
from providers.exceptions import ProviderValidationError
from providers.http import (
    HttpClientConfig,
    ProviderHttpClient,
    RetryPolicy,
    TransportRequest,
    TransportResponse,
)
from providers.testing import FakeHttpTransport, FakeProviderScenario
from providers.types import ProviderCapability, ProviderRequest, ProviderResult

FIXTURE_IR_PROVIDER_KEY = "fixture-ir"
FIXTURE_IR_PROVIDER_VERSION = "fixture-v1"


class FixtureInvestorRelationsProvider(Provider):
    """Provider that returns curated IR fixture bytes for a frozen scope."""

    provider_key = FIXTURE_IR_PROVIDER_KEY
    provider_version = FIXTURE_IR_PROVIDER_VERSION
    capabilities = frozenset({ProviderCapability.INVESTOR_RELATIONS})

    def __init__(
        self,
        *,
        fixtures: Mapping[tuple[str, str], bytes] | None = None,
        scenario: FakeProviderScenario = FakeProviderScenario.SUCCESS,
        fetched_at: datetime | None = None,
    ) -> None:
        self._fixtures = dict(fixtures or {})
        self.scenario = scenario
        self._fetched_at = fetched_at
        self.transport: _FetchedAtTransport | None = None

    def _fetch(self, request: ProviderRequest) -> ProviderResult:
        source_key, company_id = _require_fixture_scope(request)
        body = self._fixture_bytes(source_key=source_key, company_id=company_id)
        fetched_at = self._fetched_at or datetime.now(UTC)
        if fetched_at < request.request_started_at:
            fetched_at = request.request_started_at
        transport = _FetchedAtTransport(
            FakeHttpTransport(self.scenario, body=body),
            fetched_at=fetched_at,
        )
        self.transport = transport
        client = ProviderHttpClient(
            transport=transport,
            config=HttpClientConfig(
                retry_policy=RetryPolicy(
                    max_attempts=1,
                    base_delay_seconds=0,
                    max_delay_seconds=0,
                )
            ),
            sleeper=lambda _seconds: None,
        )
        result = client.fetch(
            provider_key=self.provider_key,
            provider_version=self.provider_version,
            request=request,
            headers={"Accept": "application/json"},
            metadata={"fixture_provider": "FixtureInvestorRelationsProvider"},
        )
        return result

    def _fixture_bytes(self, *, source_key: str, company_id: str) -> bytes:
        try:
            return self._fixtures[(source_key, company_id)]
        except KeyError:
            raise ProviderValidationError(
                f"No fixture registered for source {source_key!r} and company {company_id!r}."
            ) from None


def _require_fixture_scope(request: ProviderRequest) -> tuple[str, str]:
    scope = request.scope
    if request.capability is not ProviderCapability.INVESTOR_RELATIONS:
        raise ProviderValidationError(
            "FixtureInvestorRelationsProvider only supports investor_relations."
        )
    source_key = scope.get("source_key")
    company_id = scope.get("company_id")
    if not isinstance(source_key, str) or not source_key.strip():
        raise ProviderValidationError("IR fixture scope requires a source_key.")
    if not isinstance(company_id, str) or not company_id.strip():
        raise ProviderValidationError("IR fixture scope requires a company_id.")
    return source_key.strip(), company_id.strip()


class ExplodingIRTransport:
    """Transport that fails the test if any IR network call is attempted."""

    def send(self, request: TransportRequest) -> object:
        raise AssertionError(
            f"fixture-first IR code attempted a transport call to {request.url!r}."
        )


class _FetchedAtTransport:
    """Wrap the fixture transport and stamp a deterministic fetched_at."""

    def __init__(self, transport: FakeHttpTransport, *, fetched_at: datetime | None) -> None:
        self._transport = transport
        self._fetched_at = fetched_at

    def send(self, request: TransportRequest) -> TransportResponse:
        response = self._transport.send(request)
        return replace(response, fetched_at=self._fetched_at or response.fetched_at)
