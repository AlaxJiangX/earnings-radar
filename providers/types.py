from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import cast

from audit.security import (
    AuditSecurityError,
    build_provider_request_context_descriptor,
    ensure_payload_has_no_credentials,
    normalize_json_without_credentials,
)
from providers.exceptions import ProviderValidationError

_PROVIDER_KEY_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProviderCapability(StrEnum):
    EARNINGS_CALENDAR = "earnings_calendar"
    INVESTOR_RELATIONS = "investor_relations"
    SEC_EDGAR = "sec_edgar"
    INDEX_CONSTITUENTS = "index_constituents"


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    capability: ProviderCapability
    scope: Mapping[str, object]
    request_started_at: datetime
    source_url: str = field(repr=False)
    method: str = "GET"
    request_identity: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.capability, ProviderCapability):
            raise ProviderValidationError("Provider capability must use the supported enum.")
        if not _is_aware(self.request_started_at):
            raise ProviderValidationError("Provider request_started_at must be timezone-aware.")
        try:
            descriptor = build_provider_request_context_descriptor(
                capability=self.capability.value,
                scope=self.scope,
                method=self.method,
                source_url=self.source_url,
                request_identity=self.request_identity,
            )
        except AuditSecurityError as error:
            raise ProviderValidationError(str(error)) from None
        object.__setattr__(self, "method", descriptor.method)
        object.__setattr__(self, "scope", descriptor.scope)
        object.__setattr__(self, "request_identity", descriptor.identity)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider_key: str
    provider_version: str
    capability: ProviderCapability
    scope: Mapping[str, object]
    request_started_at: datetime
    source_url: str
    request_method: str
    request_fingerprint: str
    request_identity: Mapping[str, object]
    http_status: int
    content_type: str
    raw_content: bytes = field(repr=False)
    fetched_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _PROVIDER_KEY_RE.fullmatch(self.provider_key):
            raise ProviderValidationError("Provider key must be a stable lowercase identifier.")
        normalized_version = self.provider_version.strip()
        if not normalized_version or len(normalized_version) > 100:
            raise ProviderValidationError("Provider version must contain 1 to 100 characters.")
        if not isinstance(self.capability, ProviderCapability):
            raise ProviderValidationError("Provider capability must use the supported enum.")
        if not _is_aware(self.request_started_at) or not _is_aware(self.fetched_at):
            raise ProviderValidationError("Provider result timestamps must be timezone-aware.")
        if self.fetched_at < self.request_started_at:
            raise ProviderValidationError(
                "Provider fetched_at must not precede request_started_at."
            )
        if not isinstance(self.raw_content, bytes):
            raise ProviderValidationError("Provider raw content must be bytes.")
        try:
            descriptor = build_provider_request_context_descriptor(
                capability=self.capability.value,
                scope=self.scope,
                method=self.request_method,
                source_url=self.source_url,
                request_identity=self.request_identity,
            )
            normalized_metadata = _normalize_secure_mapping(
                self.metadata,
                value_name="Provider metadata",
            )
            ensure_payload_has_no_credentials(self.raw_content)
        except AuditSecurityError as error:
            raise ProviderValidationError(str(error)) from None
        if self.source_url not in {
            descriptor.source_url.stored,
            descriptor.source_url.canonical,
        }:
            raise ProviderValidationError("Provider result source_url must already be sanitized.")
        if not _SHA256_RE.fullmatch(self.request_fingerprint):
            raise ProviderValidationError("Provider request fingerprint must be lowercase SHA-256.")
        if self.request_fingerprint != descriptor.fingerprint:
            raise ProviderValidationError(
                "Provider request fingerprint does not match its echoed request context."
            )
        if (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or not 100 <= self.http_status <= 599
        ):
            raise ProviderValidationError("Provider HTTP status must be between 100 and 599.")
        normalized_content_type = self.content_type.strip()
        if len(normalized_content_type) > 255:
            raise ProviderValidationError("Provider content type is too long.")
        try:
            safe_content_type = normalize_json_without_credentials(
                normalized_content_type,
                value_name="Provider content type",
            )
        except AuditSecurityError as error:
            raise ProviderValidationError(str(error)) from None
        if not isinstance(safe_content_type, str):
            raise ProviderValidationError("Provider content type must be text.")
        object.__setattr__(self, "provider_version", normalized_version)
        object.__setattr__(self, "scope", descriptor.scope)
        object.__setattr__(self, "request_method", descriptor.method)
        object.__setattr__(self, "request_identity", descriptor.identity)
        object.__setattr__(self, "content_type", safe_content_type)
        object.__setattr__(self, "metadata", normalized_metadata)


def _normalize_secure_mapping(
    value: Mapping[str, object],
    *,
    value_name: str,
) -> dict[str, object]:
    normalized = normalize_json_without_credentials(dict(value), value_name=value_name)
    if not isinstance(normalized, dict):
        raise ProviderValidationError(f"{value_name} must be a JSON object.")
    return cast(dict[str, object], normalized)


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
