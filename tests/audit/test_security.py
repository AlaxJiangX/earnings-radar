import pytest

from audit.security import (
    SensitiveAuditData,
    contains_authentication_credential,
    is_sensitive_field_name,
    normalize_json_without_credentials,
)


@pytest.mark.parametrize(
    "field_name",
    (
        "key",
        "api_key",
        "apikey",
        "api-key",
        "token",
        "access_token",
        "access-token",
        "auth",
        "authentication",
        "authorization",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "client-secret",
        "basic_auth",
        "basic-auth",
        "x-api-key",
        "AUTHORIZATION",
        "Access_Token",
    ),
)
def test_sensitive_field_aliases_are_case_and_separator_insensitive(field_name: str) -> None:
    assert is_sensitive_field_name(field_name) is True


@pytest.mark.parametrize(
    "value",
    (
        "Basic dXNlcjpwYXNz",
        "Bearer x",
        "Bearer opaque-token",
        "Authorization: Bearer opaque",
    ),
)
def test_authentication_formats_are_detected(value: str) -> None:
    assert contains_authentication_credential(value) is True


@pytest.mark.parametrize("value", ("basic", "bearer", "basic validation"))
def test_plain_authentication_words_are_not_credentials(value: str) -> None:
    assert contains_authentication_credential(value) is False


def test_nested_tuple_and_list_credentials_are_rejected_without_echoing_secret() -> None:
    value = {"items": [("safe",), {"nested": {"ToKeN": "fixture-nested-secret"}}]}

    with pytest.raises(SensitiveAuditData) as error:
        normalize_json_without_credentials(value, value_name="fixture")

    assert "fixture-nested-secret" not in str(error.value)
