import importlib
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

from audit.constants import RAW_DATA_PAYLOAD_DB_LIMIT_BYTES

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_positive_int(name: str, *, default: int, maximum: int | None = None) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ImproperlyConfigured(f"{name} must be an integer.") from error
    if value <= 0 or (maximum is not None and value > maximum):
        maximum_note = f" and at most {maximum}" if maximum is not None else ""
        raise ImproperlyConfigured(f"{name} must be positive{maximum_note}.")
    return value


DJANGO_ENV = os.getenv("DJANGO_ENV", "development").strip().lower()
DEBUG = env_bool("DJANGO_DEBUG", default=DJANGO_ENV == "development")

_local_environments = frozenset({"development", "test"})
if DJANGO_ENV not in _local_environments and DEBUG:
    raise ImproperlyConfigured("DJANGO_DEBUG must be disabled outside development and test.")

_development_secret = "unsafe-development-only-key"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", _development_secret)
_normalized_secret = SECRET_KEY.strip()
_development_secrets = {
    _development_secret,
    "unsafe-local-development-key",  # Docker Compose default
    "replace-with-a-local-development-key",  # .env.example placeholder
}
if (
    DJANGO_ENV not in _local_environments
    and (not _normalized_secret or _normalized_secret in _development_secrets)
) or (not DEBUG and _normalized_secret == _development_secret):
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must use a non-development value.")

_development_audit_ip_hash_key = "unsafe-development-and-test-only-audit-ip-hash-key"
_audit_ip_hash_key_placeholder = "replace-with-a-local-audit-ip-hash-key"
_configured_audit_ip_hash_key = os.getenv("AUDIT_IP_HASH_KEY")
if _configured_audit_ip_hash_key is None or not _configured_audit_ip_hash_key.strip():
    if DJANGO_ENV not in _local_environments:
        raise ImproperlyConfigured("AUDIT_IP_HASH_KEY must be set outside development and test.")
    AUDIT_IP_HASH_KEY = _development_audit_ip_hash_key
else:
    AUDIT_IP_HASH_KEY = _configured_audit_ip_hash_key

if DJANGO_ENV not in _local_environments:
    if AUDIT_IP_HASH_KEY in {
        _development_audit_ip_hash_key,
        _audit_ip_hash_key_placeholder,
    }:
        raise ImproperlyConfigured(
            "AUDIT_IP_HASH_KEY must not use a development placeholder or default "
            "outside development and test."
        )
    if AUDIT_IP_HASH_KEY == SECRET_KEY:
        raise ImproperlyConfigured(
            "AUDIT_IP_HASH_KEY must be distinct from DJANGO_SECRET_KEY "
            "outside development and test."
        )

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts.apps.AccountsConfig",
    "audit.apps.AuditConfig",
    "companies.apps.CompaniesConfig",
    "indexes.apps.IndexesConfig",
    "watchlists.apps.WatchlistsConfig",
    "earnings.apps.EarningsConfig",
    "filings.apps.FilingsConfig",
    "notifications.apps.NotificationsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Stage 8.1: serving collectstatic output (currently only Django Admin
# assets) from a single web container requires whitenoise.  The dependency
# itself is pending merge-gate approval (docs/deployment.md section 0), so
# the flag defaults to off and fails loudly when enabled without the
# package installed -- no silent fallback.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
_USE_WHITENOISE = env_bool("DJANGO_USE_WHITENOISE", default=False)
if _USE_WHITENOISE:
    try:
        importlib.import_module("whitenoise")
    except ImportError as error:
        raise ImproperlyConfigured(
            "DJANGO_USE_WHITENOISE=true requires the whitenoise package; "
            "add it to requirements.txt first (docs/deployment.md section 0)."
        ) from error
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
    STORAGES["staticfiles"]["BACKEND"] = "whitenoise.storage.CompressedManifestStaticFilesStorage"

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "config.context.site_globals",
            ],
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


def _database_url_config(raw_url: str) -> dict[str, object]:
    # Stage 8.1: managed-PaaS connection strings (Railway/Render style) are
    # accepted verbatim; error messages never embed the URL because it
    # contains the database password.
    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL must use the postgres:// or postgresql:// scheme.")
    database_name = parsed.path.lstrip("/")
    if not parsed.hostname or parsed.username is None or not database_name:
        raise ImproperlyConfigured("DATABASE_URL must include user, host and database name.")
    try:
        port = str(parsed.port) if parsed.port else "5432"
    except ValueError as error:
        raise ImproperlyConfigured("DATABASE_URL has an invalid port.") from error
    options: dict[str, str] = {}
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        if len(values) != 1:
            raise ImproperlyConfigured(
                f"DATABASE_URL query parameter {key!r} must appear exactly once."
            )
        options[key] = values[0]
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(database_name),
        "USER": unquote(parsed.username),
        "PASSWORD": unquote(parsed.password) if parsed.password else "",
        "HOST": parsed.hostname,
        "PORT": port,
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": options,
    }


_database_url = os.getenv("DATABASE_URL", "").strip()
if _database_url:
    DATABASES = {"default": _database_url_config(_database_url)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "earnings_radar"),
            "USER": os.getenv("POSTGRES_USER", "earnings_radar"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", "local-development-password"),
            "HOST": os.getenv("POSTGRES_HOST", "localhost"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Stage 8.1: collectstatic target for production deploys (gitignored).
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

# Stage 5.2 login-required pages redirect anonymous visitors to the Django
# Admin login screen.  Stage 1.4 shipped auth plumbing without a standalone
# login page; for the personal/friends scale baseline the admin screen is
# the existing, CSRF-protected login entry point.
LOGIN_URL = "/admin/login/"

# Stage 7.1 public GitHub entry point (nav, footer and the home page block).
# Environment-overridable so self-hosted forks can point at their own repo;
# an empty value falls back to the upstream repository URL.
GITHUB_URL = os.getenv("GITHUB_URL", "").strip() or "https://github.com/AlaxJiangX/earnings-radar"

RAW_DATA_MAX_PAYLOAD_BYTES = env_positive_int(
    "RAW_DATA_MAX_PAYLOAD_BYTES",
    default=RAW_DATA_PAYLOAD_DB_LIMIT_BYTES,
    maximum=RAW_DATA_PAYLOAD_DB_LIMIT_BYTES,
)

SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip()
SEC_MAX_REQUESTS_PER_SECOND = env_positive_int("SEC_MAX_REQUESTS_PER_SECOND", default=4, maximum=9)

EARNINGS_CALENDAR_STALE_AFTER_SECONDS = env_positive_int(
    "EARNINGS_CALENDAR_STALE_AFTER_SECONDS",
    default=1800,
    maximum=86400,
)

REFERENCE_CALENDAR_STALE_AFTER_HOURS = env_positive_int(
    "REFERENCE_CALENDAR_STALE_AFTER_HOURS",
    default=48,
    maximum=8760,
)

# Stage 6.4 email delivery (ADR-027 D1): the generic SMTP adapter rides on
# Django's native mail stack with zero new dependencies; every value comes
# from the environment only.  The send command refuses to claim anything
# when the SMTP sender lacks EMAIL_HOST / DEFAULT_FROM_EMAIL.
EMAIL_HOST = os.getenv("EMAIL_HOST", "").strip()
EMAIL_PORT = env_positive_int("EMAIL_PORT", default=587, maximum=65535)
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", default=False)
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ImproperlyConfigured("EMAIL_USE_TLS and EMAIL_USE_SSL are mutually exclusive.")
EMAIL_TIMEOUT = env_positive_int("EMAIL_TIMEOUT", default=15, maximum=300)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "").strip()

# Stage 8.1 production deployment hardening.  PaaS edge proxies terminate
# TLS, so Django must trust the forwarded-protocol header and secure the
# session/CSRF cookies; SSL redirect itself stays opt-in because the
# Railway/Render edge already forces HTTPS and internal platform probes
# speak plain HTTP without the forwarded header.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

if DJANGO_ENV not in _local_environments:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=False)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = env_positive_int(
        "DJANGO_SECURE_HSTS_SECONDS", default=3600, maximum=31536000
    )
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
