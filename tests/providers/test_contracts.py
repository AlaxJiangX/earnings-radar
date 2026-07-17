from dataclasses import replace
from datetime import UTC, datetime

import pytest

from providers.base import Provider
from providers.exceptions import ProviderValidationError
from providers.testing import FakeProvider, make_fake_provider_request
from providers.types import ProviderCapability, ProviderRequest, ProviderResult


def test_capability_enum_contains_only_planned_mvp_provider_types() -> None:
    assert {capability.value for capability in ProviderCapability} == {
        "earnings_calendar",
        "investor_relations",
        "sec_edgar",
        "index_constituents",
    }


def test_fake_provider_satisfies_the_provider_contract() -> None:
    provider = FakeProvider()

    assert isinstance(provider, Provider)
    assert provider.provider_key == "fixture-provider"
    assert provider.provider_version == "fixture-v1"
    assert provider.capabilities == frozenset(ProviderCapability)


def test_provider_request_rejects_naive_timestamp() -> None:
    with pytest.raises(ProviderValidationError):
        ProviderRequest(
            capability=ProviderCapability.EARNINGS_CALENDAR,
            scope={"fixture": True},
            request_started_at=datetime(2026, 7, 14, 12, 0),
            source_url="https://provider.example.test/calendar",
        )


def test_provider_request_rejects_url_userinfo_without_echoing_secret() -> None:
    with pytest.raises(ProviderValidationError) as error:
        ProviderRequest(
            capability=ProviderCapability.EARNINGS_CALENDAR,
            scope={"fixture": True},
            request_started_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            source_url=(
                "https://fixture-user:fixture-userinfo-secret@provider.example.test/calendar"
            ),
        )

    assert "fixture-user" not in str(error.value)
    assert "fixture-userinfo-secret" not in str(error.value)


def test_provider_request_rejects_sensitive_scope_without_echoing_secret() -> None:
    with pytest.raises(ProviderValidationError) as error:
        ProviderRequest(
            capability=ProviderCapability.EARNINGS_CALENDAR,
            scope={"nested": {"ToKeN": "fixture-scope-secret"}},
            request_started_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            source_url="https://provider.example.test/calendar",
        )

    assert "fixture-scope-secret" not in str(error.value)


def test_provider_request_repr_hides_unsanitized_source_url() -> None:
    request = make_fake_provider_request(api_key_in_url="fixture-query-token")

    assert "fixture-query-token" not in repr(request)


def test_provider_result_fields_are_timezone_aware_and_complete() -> None:
    request = make_fake_provider_request()
    result = FakeProvider().fetch(request)

    assert isinstance(result, ProviderResult)
    assert result.provider_key == "fixture-provider"
    assert result.provider_version == "fixture-v1"
    assert result.capability is ProviderCapability.EARNINGS_CALENDAR
    assert result.scope == {"fixture": True, "page": 1}
    assert result.request_started_at.tzinfo is not None
    assert result.fetched_at.tzinfo is not None
    assert result.fetched_at >= result.request_started_at
    assert result.http_status == 200
    assert result.content_type == "application/json"
    assert result.raw_content
    assert result.metadata == {"attempt_count": 1, "fixture_scenario": "success"}


def test_provider_rejects_unsupported_capability_before_transport() -> None:
    class EarningsOnlyFakeProvider(FakeProvider):
        capabilities = frozenset({ProviderCapability.EARNINGS_CALENDAR})

    provider = EarningsOnlyFakeProvider()
    request = make_fake_provider_request(capability=ProviderCapability.SEC_EDGAR)

    with pytest.raises(ProviderValidationError):
        provider.fetch(request)

    assert provider.transport.call_count == 0


@pytest.mark.parametrize(
    ("result_change", "changed_value"),
    [("request_method", "POST"), ("source_url", "https://other.example.test/calendar")],
)
def test_provider_rejects_result_from_different_request_context(
    result_change: str,
    changed_value: str,
) -> None:
    class MismatchedResultProvider(FakeProvider):
        def _fetch(self, request: ProviderRequest) -> ProviderResult:
            result = super()._fetch(request)
            if result_change == "request_method":
                return replace(result, request_method=changed_value)
            return replace(result, source_url=changed_value)

    with pytest.raises(ProviderValidationError, match="request context"):
        MismatchedResultProvider().fetch(make_fake_provider_request())
