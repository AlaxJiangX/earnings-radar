import re

import pytest

from audit.constants import RAW_DATA_PAYLOAD_DB_LIMIT_BYTES
from providers.exceptions import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderPermanentError,
    ProviderRateLimitError,
    ProviderResponseTooLargeError,
    ProviderTemporaryError,
    ProviderTimeoutError,
    ProviderValidationError,
)
from providers.http import HttpClientConfig, HttpTimeouts, RetryPolicy
from providers.testing import FakeProvider, FakeProviderScenario, make_fake_provider_request


@pytest.mark.parametrize(
    ("scenario", "error_type", "retryable", "http_status"),
    (
        (FakeProviderScenario.TIMEOUT, ProviderTimeoutError, True, None),
        (FakeProviderScenario.RATE_LIMIT, ProviderRateLimitError, True, 429),
        (FakeProviderScenario.SERVER_ERROR, ProviderTemporaryError, True, 500),
        (FakeProviderScenario.NOT_FOUND, ProviderPermanentError, False, 404),
        (
            FakeProviderScenario.AUTHENTICATION_ERROR,
            ProviderAuthenticationError,
            False,
            401,
        ),
    ),
)
def test_http_failures_have_explicit_retry_classification(
    scenario: FakeProviderScenario,
    error_type: type[Exception],
    retryable: bool,
    http_status: int | None,
) -> None:
    provider = FakeProvider(scenario)

    with pytest.raises(error_type) as error:
        provider.fetch(make_fake_provider_request())

    provider_error = error.value
    assert isinstance(provider_error, error_type)
    assert isinstance(provider_error, ProviderError)
    assert provider_error.retryable is retryable
    assert provider_error.http_status == http_status


def test_rate_limit_keeps_only_safe_retry_after_seconds() -> None:
    provider = FakeProvider(FakeProviderScenario.RATE_LIMIT)

    with pytest.raises(ProviderRateLimitError) as error:
        provider.fetch(make_fake_provider_request())

    assert error.value.retry_after_seconds == 60
    assert "fixture" not in str(error.value).lower()


def test_invalid_json_becomes_non_retryable_validation_error() -> None:
    provider = FakeProvider(FakeProviderScenario.INVALID_JSON)

    with pytest.raises(ProviderValidationError) as error:
        provider.fetch(make_fake_provider_request())

    assert error.value.retryable is False
    assert "invalid-json" not in str(error.value)


def test_response_larger_than_limit_is_rejected() -> None:
    config = HttpClientConfig(max_response_bytes=64)
    provider = FakeProvider(FakeProviderScenario.RESPONSE_TOO_LARGE, config=config)

    with pytest.raises(ProviderResponseTooLargeError) as error:
        provider.fetch(make_fake_provider_request())

    assert error.value.retryable is False
    assert error.value.limit_bytes == 64
    assert error.value.observed_bytes == 65


def test_http_config_rejects_size_above_audit_database_limit() -> None:
    with pytest.raises(ValueError):
        HttpClientConfig(max_response_bytes=RAW_DATA_PAYLOAD_DB_LIMIT_BYTES + 1)


def test_transport_receives_distinct_timeouts_limit_and_explicit_user_agent() -> None:
    config = HttpClientConfig(
        timeouts=HttpTimeouts(connect_seconds=2.5, read_seconds=9.5),
        max_response_bytes=512,
        user_agent="EarningsRadarFixture/1.0 (+https://example.test/contact)",
    )
    provider = FakeProvider(config=config)

    provider.fetch(make_fake_provider_request())

    assert provider.transport.last_connect_timeout_seconds == 2.5
    assert provider.transport.last_read_timeout_seconds == 9.5
    assert provider.transport.last_max_response_bytes == 512
    assert provider.transport.last_user_agent == config.user_agent


def test_temporary_errors_retry_only_up_to_configured_limit() -> None:
    config = HttpClientConfig(
        retry_policy=RetryPolicy(
            max_attempts=3,
            base_delay_seconds=0,
            max_delay_seconds=0,
        )
    )
    provider = FakeProvider(FakeProviderScenario.TIMEOUT, config=config)

    with pytest.raises(ProviderTimeoutError):
        provider.fetch(make_fake_provider_request())

    assert provider.transport.call_count == 3


def test_permanent_errors_are_never_retried() -> None:
    config = HttpClientConfig(
        retry_policy=RetryPolicy(
            max_attempts=3,
            base_delay_seconds=0,
            max_delay_seconds=0,
        )
    )
    provider = FakeProvider(FakeProviderScenario.NOT_FOUND, config=config)

    with pytest.raises(ProviderPermanentError):
        provider.fetch(make_fake_provider_request())

    assert provider.transport.call_count == 1


def test_sensitive_query_values_are_removed_from_result_and_fingerprint() -> None:
    first = FakeProvider(
        FakeProviderScenario.SENSITIVE_URL,
        api_key="fixture-header-key-one",
    ).fetch(make_fake_provider_request(api_key_in_url="fixture-query-key-one"))
    second = FakeProvider(
        FakeProviderScenario.SENSITIVE_URL,
        api_key="fixture-header-key-two",
    ).fetch(make_fake_provider_request(api_key_in_url="fixture-query-key-two"))

    assert "fixture-query-key-one" not in first.source_url
    assert "fixture-query-key-two" not in second.source_url
    assert "REDACTED" in first.source_url
    assert first.request_fingerprint == second.request_fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", first.request_fingerprint)
    assert "fixture-header-key-one" not in repr(first.request_identity)
    assert "fixture-header-key-two" not in repr(second.request_identity)


def test_non_sensitive_pagination_changes_request_fingerprint() -> None:
    first = FakeProvider().fetch(make_fake_provider_request(page=1))
    second = FakeProvider().fetch(make_fake_provider_request(page=2))

    assert first.request_fingerprint != second.request_fingerprint


@pytest.mark.parametrize(
    "scenario",
    (
        FakeProviderScenario.TIMEOUT,
        FakeProviderScenario.TRANSPORT_ERROR_WITH_TOKEN,
        FakeProviderScenario.SENSITIVE_RESPONSE,
        FakeProviderScenario.SENSITIVE_CONTENT_TYPE,
    ),
)
def test_transport_and_response_secrets_never_appear_in_errors(
    scenario: FakeProviderScenario,
) -> None:
    provider = FakeProvider(scenario)

    with pytest.raises((ProviderTemporaryError, ProviderValidationError)) as error:
        provider.fetch(make_fake_provider_request())

    message = f"{error.value!s} {error.value!r}"
    assert "fixture-timeout-token" not in message
    assert "fixture-transport-token" not in message
    assert "fixture-response-token" not in message
    assert "fixture-content-type-token" not in message


def test_sensitive_metadata_is_rejected_before_transport_without_echoing_secret() -> None:
    provider = FakeProvider()
    request = make_fake_provider_request()

    with pytest.raises(ProviderValidationError) as error:
        provider.http_client.fetch(
            provider_key=provider.provider_key,
            provider_version=provider.provider_version,
            request=request,
            metadata={"nested": {"Authorization": "Bearer fixture-metadata-token"}},
        )

    assert "fixture-metadata-token" not in str(error.value)
    assert provider.transport.call_count == 0
