import http.client
import json

import pytest

from providers.testing import FakeProvider, FakeProviderScenario, make_fake_provider_request


def test_fake_provider_success_returns_fully_fictitious_fixture() -> None:
    result = FakeProvider(FakeProviderScenario.SUCCESS).fetch(make_fake_provider_request())
    payload = json.loads(result.raw_content)

    assert payload == {"items": [{"company": "Example Test Corp", "symbol": "FAKE"}]}


def test_fake_provider_empty_response_is_supported() -> None:
    result = FakeProvider(FakeProviderScenario.EMPTY).fetch(make_fake_provider_request())

    assert result.http_status == 204
    assert result.raw_content == b""
    assert result.content_type == ""


def test_provider_test_suite_blocks_stdlib_http_connections() -> None:
    connection = http.client.HTTPSConnection("example.test")

    with pytest.raises(AssertionError, match="must not open real network"):
        connection.connect()
