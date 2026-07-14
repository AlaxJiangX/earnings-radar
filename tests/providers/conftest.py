import http.client

import pytest


@pytest.fixture(autouse=True)
def block_real_provider_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_network_is_used(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Provider tests must not open real network connections.")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", fail_if_network_is_used)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", fail_if_network_is_used)
