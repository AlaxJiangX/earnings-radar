import base64
import binascii
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

REDACTED_VALUE = "[REDACTED]"

_URL_VALIDATOR = URLValidator(schemes=("http", "https"))
_NON_ALPHANUMERIC_RE = re.compile(r"[^a-z0-9]")
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "key",
        "apikey",
        "token",
        "accesstoken",
        "refreshtoken",
        "auth",
        "authentication",
        "authorization",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "basicauth",
        "xapikey",
        "cookie",
        "credential",
        "session",
        "sessionid",
        "signature",
    }
)
_SENSITIVE_FIELD_MARKERS = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "authentication",
    "authorization",
    "clientsecret",
    "basicauth",
    "xapikey",
)
_SENSITIVE_FIELD_SUFFIXES = (
    "token",
    "auth",
    "password",
    "passwd",
    "secret",
    "cookie",
    "credential",
    "session",
    "sessionid",
    "signature",
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<label>(?<![a-z0-9_-])[\"']?(?:"
    r"x[-_]?api[-_]?key|api[-_]?key|apikey|access[-_]?token|refresh[-_]?token|"
    r"client[-_]?secret|basic[-_]?auth|authentication|authorization|password|passwd|"
    r"secret|token|auth|key"
    r")(?![a-z0-9_-])[\"']?\s*[:=]\s*)"
    r"(?:(?:basic|bearer)\s+)?[\"']?[^\s,;}&\]\"']+[\"']?"
)
_BASIC_CREDENTIAL_RE = re.compile(
    r"(?i)(?<![a-z0-9])basic\s+(?P<token>[a-z0-9+/=_-]{8,})(?![a-z0-9+/=_-])"
)
_BEARER_CREDENTIAL_RE = re.compile(
    r"(?i)(?<![a-z0-9])bearer\s+(?P<token>[a-z0-9._~+/=-]+)(?![a-z0-9._~+/=-])"
)
_URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^\s/@]+@")


class AuditSecurityError(ValueError):
    pass


class InvalidAuditValue(AuditSecurityError):
    pass


class SensitiveAuditData(AuditSecurityError):
    pass


@dataclass(frozen=True, slots=True)
class SanitizedUrl:
    stored: str
    canonical: str


def is_sensitive_field_name(name: str) -> bool:
    normalized = _NON_ALPHANUMERIC_RE.sub("", name.lower())
    if normalized in _SENSITIVE_FIELD_NAMES:
        return True
    if any(marker in normalized for marker in _SENSITIVE_FIELD_MARKERS):
        return True
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_FIELD_SUFFIXES)


def contains_authentication_credential(value: str) -> bool:
    if _SENSITIVE_ASSIGNMENT_RE.search(value):
        return True
    if any(
        _is_basic_credential(match.group("token")) for match in _BASIC_CREDENTIAL_RE.finditer(value)
    ):
        return True
    return any(
        _is_bearer_credential(match.group("token"))
        for match in _BEARER_CREDENTIAL_RE.finditer(value)
    )


def redact_sensitive_text(value: str) -> str:
    redacted = _SENSITIVE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('label')}{REDACTED_VALUE}",
        value,
    )
    redacted = _BASIC_CREDENTIAL_RE.sub(_redact_basic_match, redacted)
    redacted = _BEARER_CREDENTIAL_RE.sub(_redact_bearer_match, redacted)
    return _URL_USERINFO_RE.sub(rf"\1{REDACTED_VALUE}@", redacted)


def sanitize_error_summary(summary: str, *, maximum_length: int = 2000) -> str:
    compact = " ".join(summary.split())
    return redact_sensitive_text(compact)[:maximum_length]


def sanitize_url(value: str) -> SanitizedUrl:
    parts, query_pairs = _parse_url(value)
    sanitized_pairs = [
        (
            key,
            REDACTED_VALUE
            if is_sensitive_field_name(key) or contains_authentication_credential(item)
            else item,
        )
        for key, item in query_pairs
    ]
    normalized_base = (parts.scheme.lower(), parts.netloc.lower(), parts.path)
    stored = urlunsplit((*normalized_base, urlencode(sanitized_pairs), ""))
    canonical = urlunsplit((*normalized_base, urlencode(sorted(sanitized_pairs)), ""))
    return SanitizedUrl(stored=stored, canonical=canonical)


def validate_safe_base_url(value: str) -> None:
    if not value:
        return
    try:
        parts, query_pairs = _parse_url(value)
    except AuditSecurityError as error:
        raise ValidationError(str(error), code="unsafe_base_url") from None

    if any(
        (is_sensitive_field_name(key) and item not in {"", REDACTED_VALUE})
        or contains_authentication_credential(item)
        for key, item in query_pairs
    ):
        raise ValidationError(
            "Base URL must not contain credential query values.",
            code="unsafe_base_url_query",
        )
    if parts.fragment and contains_authentication_credential(parts.fragment):
        raise ValidationError(
            "Base URL fragment must not contain credentials.",
            code="unsafe_base_url_fragment",
        )


def normalize_request_identity(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidAuditValue("Request identity keys must be strings.")
            result[key] = (
                REDACTED_VALUE if is_sensitive_field_name(key) else normalize_request_identity(item)
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [normalize_request_identity(item) for item in value]
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidAuditValue("Request identity must contain finite JSON numbers.")
        return value
    if isinstance(value, str):
        if value.lower().startswith(("http://", "https://")):
            try:
                return sanitize_url(value).canonical
            except AuditSecurityError:
                return REDACTED_VALUE
        return REDACTED_VALUE if contains_authentication_credential(value) else value
    raise InvalidAuditValue("Request identity must contain JSON-compatible data.")


def normalize_json_without_credentials(value: object, *, value_name: str) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidAuditValue(f"{value_name} JSON object keys must be strings.")
            if is_sensitive_field_name(key):
                raise SensitiveAuditData(f"{value_name} contains credential-like data.")
            result[key] = normalize_json_without_credentials(item, value_name=value_name)
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [normalize_json_without_credentials(item, value_name=value_name) for item in value]
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidAuditValue(f"{value_name} must not contain NaN or infinity.")
        return value
    if isinstance(value, str):
        if contains_authentication_credential(value):
            raise SensitiveAuditData(f"{value_name} contains credential-like data.")
        return value
    raise InvalidAuditValue(f"{value_name} must contain JSON-compatible data.")


def ensure_payload_has_no_credentials(payload: bytes) -> None:
    text = payload.decode("utf-8", errors="ignore")
    if not text:
        return
    try:
        parsed: object = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None
    if parsed is not None:
        try:
            normalize_json_without_credentials(parsed, value_name="Raw payload")
        except SensitiveAuditData:
            raise SensitiveAuditData("Raw payload contains credential-like data.") from None
        except InvalidAuditValue:
            pass
    if contains_authentication_credential(text):
        raise SensitiveAuditData("Raw payload contains credential-like data.")


def _parse_url(value: str) -> tuple[SplitResult, list[tuple[str, str]]]:
    try:
        _URL_VALIDATOR(value)
        parts = urlsplit(value)
        if parts.username is not None or parts.password is not None:
            raise SensitiveAuditData("URL must not contain user credentials.")
        query_pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=1000)
    except ValidationError as error:
        raise InvalidAuditValue("URL must be a valid HTTP(S) URL.") from error
    except ValueError as error:
        raise InvalidAuditValue("URL is invalid or contains too many query fields.") from error
    return parts, query_pairs


def _is_basic_credential(token: str) -> bool:
    try:
        padded = token + ("=" * (-len(token) % 4))
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return False
    return b":" in decoded


def _is_bearer_credential(token: str) -> bool:
    return bool(token)


def _redact_basic_match(match: re.Match[str]) -> str:
    if not _is_basic_credential(match.group("token")):
        return match.group(0)
    return f"Basic {REDACTED_VALUE}"


def _redact_bearer_match(match: re.Match[str]) -> str:
    if not _is_bearer_credential(match.group("token")):
        return match.group(0)
    return f"Bearer {REDACTED_VALUE}"
