import os
from pathlib import Path

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


DJANGO_ENV = os.getenv("DJANGO_ENV", "development")
DEBUG = env_bool("DJANGO_DEBUG", default=DJANGO_ENV == "development")

_development_secret = "unsafe-development-only-key"
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", _development_secret)
if not DEBUG and SECRET_KEY == _development_secret:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set outside development.")

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

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

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
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

RAW_DATA_MAX_PAYLOAD_BYTES = env_positive_int(
    "RAW_DATA_MAX_PAYLOAD_BYTES",
    default=RAW_DATA_PAYLOAD_DB_LIMIT_BYTES,
    maximum=RAW_DATA_PAYLOAD_DB_LIMIT_BYTES,
)
