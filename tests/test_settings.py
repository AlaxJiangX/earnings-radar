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
) -> dict[str, object]:
    monkeypatch.setenv("DJANGO_ENV", django_env)
    monkeypatch.setenv("DJANGO_DEBUG", "true" if django_env == "development" else "false")
    monkeypatch.setenv("DJANGO_SECRET_KEY", django_secret_key)
    if audit_ip_hash_key is None:
        monkeypatch.delenv("AUDIT_IP_HASH_KEY", raising=False)
    else:
        monkeypatch.setenv("AUDIT_IP_HASH_KEY", audit_ip_hash_key)
    return runpy.run_path(str(SETTINGS_PATH))


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


def test_env_example_contains_only_audit_key_placeholder() -> None:
    values = dict(
        line.split("=", maxsplit=1)
        for line in ENV_EXAMPLE_PATH.read_text().splitlines()
        if line and not line.startswith("#")
    )

    assert values["AUDIT_IP_HASH_KEY"] == "replace-with-a-local-audit-ip-hash-key"
    assert DEVELOPMENT_AUDIT_IP_HASH_KEY not in ENV_EXAMPLE_PATH.read_text()
