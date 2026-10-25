import runpy
from pathlib import Path
from typing import Any

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
    database_url: str | None = None,
) -> dict[str, Any]:
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
    if database_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", database_url)
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


def _production_settings(monkeypatch: pytest.MonkeyPatch, **kwargs: str | None) -> dict[str, Any]:
    return _load_settings(
        monkeypatch,
        django_env="production",
        django_secret_key="fixture-production-django-secret",
        audit_ip_hash_key="fixture-production-audit-key",
        **kwargs,
    )


def test_production_database_url_parses_into_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = (
        "postgresql://postgres:p%40ssw0rd-abc@postgres.railway.internal:6543"
        "/railway?sslmode=require"
    )
    loaded = _production_settings(monkeypatch, database_url=url)

    database = loaded["DATABASES"]["default"]
    assert database["NAME"] == "railway"
    assert database["USER"] == "postgres"
    assert database["PASSWORD"] == "p@ssw0rd-abc"
    assert database["HOST"] == "postgres.railway.internal"
    assert database["PORT"] == "6543"
    assert database["OPTIONS"] == {"sslmode": "require"}
    assert database["CONN_MAX_AGE"] == 60


def test_database_url_accepts_short_postgres_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _production_settings(
        monkeypatch,
        database_url="postgres://u:p@db.internal:5432/earnings_radar",
    )

    assert loaded["DATABASES"]["default"]["NAME"] == "earnings_radar"
    assert loaded["DATABASES"]["default"]["PORT"] == "5432"


def test_database_url_overrides_component_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_DB", "component-db")
    loaded = _production_settings(
        monkeypatch,
        database_url="postgresql://u:p@db.internal:5432/url-db",
    )

    assert loaded["DATABASES"]["default"]["NAME"] == "url-db"


def test_empty_database_url_falls_back_to_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _production_settings(monkeypatch, database_url="   ")

    assert loaded["DATABASES"]["default"]["NAME"] == "earnings_radar"


@pytest.mark.parametrize(
    "url",
    (
        "mysql://u:p@db.internal:3306/app",
        "postgresql://db.internal:5432/app",
        "postgresql://u:p@db.internal:5432/",
        "postgresql://u:p@db.internal:not-a-port/app",
    ),
)
def test_database_url_rejects_unusable_urls(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="DATABASE_URL"):
        _production_settings(monkeypatch, database_url=url)


def test_database_url_rejects_duplicate_query_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ImproperlyConfigured, match="exactly once"):
        _production_settings(
            monkeypatch,
            database_url="postgresql://u:p@db.internal:5432/app?sslmode=require&sslmode=require",
        )


@pytest.mark.parametrize(
    "url",
    (
        "postgresql://postgres:p%40ssw0rd-abc@db.internal:5432/",
        "mysql://postgres:p%40ssw0rd-abc@db.internal:5432/app",
        "postgresql://u:p@db.internal:not-a-port/app",
    ),
)
def test_database_url_errors_never_contain_the_password(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    with pytest.raises(ImproperlyConfigured) as error:
        _production_settings(monkeypatch, database_url=url)

    assert "p@ssw0rd-abc" not in str(error.value)
    assert "p%40ssw0rd-abc" not in str(error.value)


def test_whitenoise_defaults_to_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DJANGO_USE_WHITENOISE", raising=False)
    loaded = _production_settings(monkeypatch)

    assert loaded["STORAGES"]["staticfiles"]["BACKEND"] == (
        "django.contrib.staticfiles.storage.StaticFilesStorage"
    )
    assert not any("whitenoise" in str(item) for item in loaded["MIDDLEWARE"])


def test_whitenoise_flag_requires_the_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    monkeypatch.setenv("DJANGO_USE_WHITENOISE", "true")
    if importlib.util.find_spec("whitenoise") is None:
        with pytest.raises(ImproperlyConfigured, match="whitenoise"):
            _production_settings(monkeypatch)
    else:
        loaded = _production_settings(monkeypatch)
        assert "whitenoise.middleware.WhiteNoiseMiddleware" in loaded["MIDDLEWARE"]
        assert loaded["STORAGES"]["staticfiles"]["BACKEND"] == (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )


def test_production_csrf_trusted_origins_are_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        " https://app.up.railway.app , https://radar.example.com,",
    )
    loaded = _production_settings(monkeypatch)

    assert loaded["CSRF_TRUSTED_ORIGINS"] == [
        "https://app.up.railway.app",
        "https://radar.example.com",
    ]


def test_local_csrf_trusted_origins_default_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DJANGO_CSRF_TRUSTED_ORIGINS", raising=False)
    loaded = _load_settings(
        monkeypatch,
        django_env="development",
        django_secret_key="fixture-local-django-secret",
        audit_ip_hash_key=None,
    )

    assert loaded["CSRF_TRUSTED_ORIGINS"] == []


def test_production_enables_behind_proxy_security_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _production_settings(monkeypatch)

    assert loaded["SECURE_PROXY_SSL_HEADER"] == ("HTTP_X_FORWARDED_PROTO", "https")
    assert loaded["SECURE_SSL_REDIRECT"] is False
    assert loaded["SESSION_COOKIE_SECURE"] is True
    assert loaded["CSRF_COOKIE_SECURE"] is True
    assert loaded["SECURE_HSTS_SECONDS"] == 3600
    assert loaded["SECURE_HSTS_INCLUDE_SUBDOMAINS"] is True


def test_local_environment_has_no_proxy_security_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _load_settings(
        monkeypatch,
        django_env="development",
        django_secret_key="fixture-local-django-secret",
        audit_ip_hash_key=None,
    )

    assert "SECURE_PROXY_SSL_HEADER" not in loaded
    assert "SESSION_COOKIE_SECURE" not in loaded
    assert "SECURE_HSTS_SECONDS" not in loaded


def test_static_root_points_at_gitignored_collectstatic_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _production_settings(monkeypatch)

    assert str(loaded["STATIC_ROOT"]).endswith("staticfiles")
