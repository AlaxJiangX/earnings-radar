import runpy
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

SETTINGS_PATH = Path(__file__).resolve().parents[1] / "config" / "settings.py"
ENV_EXAMPLE_PATH = Path(__file__).resolve().parents[1] / ".env.example"
DEVELOPMENT_AUDIT_IP_HASH_KEY = "unsafe-development-and-test-only-audit-ip-hash-key"


def _load_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    django_env: str,
    django_secret_key: str,
    audit_ip_hash_key: str | None,
    django_debug: bool | str | None = None,
) -> dict[str, object]:
    monkeypatch.setenv("DJANGO_ENV", django_env)
    if django_debug is None:
        django_debug = django_env == "development"
    monkeypatch.setenv(
        "DJANGO_DEBUG",
        django_debug if isinstance(django_debug, str) else ("true" if django_debug else "false"),
    )
    monkeypatch.setenv("DJANGO_SECRET_KEY", django_secret_key)
    if audit_ip_hash_key is None:
        monkeypatch.delenv("AUDIT_IP_HASH_KEY", raising=False)
    else:
        monkeypatch.setenv("AUDIT_IP_HASH_KEY", audit_ip_hash_key)
    return runpy.run_path(str(SETTINGS_PATH))


@pytest.mark.parametrize("django_debug", ("true", "True", "1", "yes", "on", " ON "))
def test_production_rejects_debug_even_with_distinct_keys(
    monkeypatch: pytest.MonkeyPatch,
    django_debug: str,
) -> None:
    with pytest.raises(ImproperlyConfigured, match="DJANGO_DEBUG"):
        _load_settings(
            monkeypatch,
            django_env="production",
            django_secret_key="fixture-production-django-secret",
            audit_ip_hash_key="fixture-production-audit-key",
            django_debug=django_debug,
        )


@pytest.mark.parametrize(
    "django_secret_key",
    (
        "unsafe-development-only-key",
        "unsafe-local-development-key",
        "replace-with-a-local-development-key",
        " unsafe-local-development-key ",
        " replace-with-a-local-development-key ",
        "",
    ),
)
def test_production_rejects_known_development_django_secrets(
    monkeypatch: pytest.MonkeyPatch,
    django_secret_key: str,
) -> None:
    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
        _load_settings(
            monkeypatch,
            django_env="production",
            django_secret_key=django_secret_key,
            audit_ip_hash_key="fixture-production-audit-key",
        )


def test_production_requires_independent_audit_ip_hash_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    django_secret = "fixture-django-secret-must-not-be-reused"

    with pytest.raises(ImproperlyConfigured) as error:
        _load_settings(
            monkeypatch,
            django_env="production",
            django_secret_key=django_secret,
            audit_ip_hash_key=None,
        )

    assert "AUDIT_IP_HASH_KEY must be set" in str(error.value)
    assert django_secret not in str(error.value)


def test_production_rejects_django_secret_as_audit_ip_hash_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared_secret = "fixture-shared-secret-must-be-rejected"

    with pytest.raises(ImproperlyConfigured) as error:
        _load_settings(
            monkeypatch,
            django_env="production",
            django_secret_key=shared_secret,
            audit_ip_hash_key=shared_secret,
        )

    assert "must be distinct" in str(error.value)
    assert shared_secret not in str(error.value)


@pytest.mark.parametrize(
    "audit_ip_hash_key",
    (
        DEVELOPMENT_AUDIT_IP_HASH_KEY,
        "replace-with-a-local-audit-ip-hash-key",
    ),
)
def test_production_rejects_development_audit_ip_hash_key(
    monkeypatch: pytest.MonkeyPatch,
    audit_ip_hash_key: str,
) -> None:
    with pytest.raises(ImproperlyConfigured) as error:
        _load_settings(
            monkeypatch,
            django_env="production",
            django_secret_key="fixture-production-django-secret",
            audit_ip_hash_key=audit_ip_hash_key,
        )

    assert "development placeholder or default" in str(error.value)
    assert audit_ip_hash_key not in str(error.value)


@pytest.mark.parametrize("django_env", ("development", "test"))
def test_local_environments_may_use_explicitly_unsafe_default(
    monkeypatch: pytest.MonkeyPatch,
    django_env: str,
) -> None:
    loaded = _load_settings(
        monkeypatch,
        django_env=django_env,
        django_secret_key="fixture-local-django-secret",
        audit_ip_hash_key=None,
    )

    assert loaded["AUDIT_IP_HASH_KEY"] == DEVELOPMENT_AUDIT_IP_HASH_KEY


def test_production_reads_audit_ip_hash_key_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_key = "fixture-independent-production-audit-ip-hash-key"
    loaded = _load_settings(
        monkeypatch,
        django_env="production",
        django_secret_key="fixture-independent-production-django-secret",
        audit_ip_hash_key=audit_key,
    )

    assert loaded["AUDIT_IP_HASH_KEY"] == audit_key
    assert loaded["AUDIT_IP_HASH_KEY"] != loaded["SECRET_KEY"]


@pytest.mark.parametrize("raw_threshold", ("0", "-1", "invalid", "86401"))
def test_calendar_stale_threshold_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_threshold: str,
) -> None:
    monkeypatch.setenv("EARNINGS_CALENDAR_STALE_AFTER_SECONDS", raw_threshold)
    with pytest.raises(ImproperlyConfigured, match="EARNINGS_CALENDAR_STALE_AFTER_SECONDS"):
        _load_settings(
            monkeypatch,
            django_env="production",
            django_secret_key="fixture-production-django-secret",
            audit_ip_hash_key="fixture-production-audit-key",
        )


def test_calendar_stale_threshold_defaults_to_half_an_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EARNINGS_CALENDAR_STALE_AFTER_SECONDS", raising=False)
    loaded = _load_settings(
        monkeypatch,
        django_env="production",
        django_secret_key="fixture-production-django-secret",
        audit_ip_hash_key="fixture-production-audit-key",
    )
    assert loaded["EARNINGS_CALENDAR_STALE_AFTER_SECONDS"] == 1800


def test_env_example_contains_only_audit_key_placeholder() -> None:
    values = dict(
        line.split("=", maxsplit=1)
        for line in ENV_EXAMPLE_PATH.read_text().splitlines()
        if line and not line.startswith("#")
    )

    assert values["AUDIT_IP_HASH_KEY"] == "replace-with-a-local-audit-ip-hash-key"
    assert DEVELOPMENT_AUDIT_IP_HASH_KEY not in ENV_EXAMPLE_PATH.read_text()
