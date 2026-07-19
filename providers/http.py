from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, cast

from audit.constants import RAW_DATA_PAYLOAD_DB_LIMIT_BYTES
from audit.security import (
    AuditSecurityError,
    build_provider_request_context_descriptor,
    ensure_payload_has_no_credentials,
    normalize_json_without_credentials,
)
from providers.exceptions import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderPermanentError,
    ProviderRateLimitError,
    ProviderResponseTooLargeError,
    ProviderTemporaryError,
    ProviderTimeoutError,
    ProviderValidationError,
)
from providers.types import ProviderRequest, ProviderResult

DEFAULT_PROVIDER_USER_AGENT = (
    "EarningsRadar/0.1 ProviderClient (+https://github.com/AlaxJiangX/earnings-radar)"
)


@dataclass(frozen=True, slots=True)
class HttpTimeouts:
    connect_seconds: float = 5.0
    read_seconds: float = 15.0

    def __post_init__(self) -> None:
        _require_positive_finite(self.connect_seconds, value_name="connect timeout")
        _require_positive_finite(self.read_seconds, value_name="read timeout")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 5.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer.")
        _require_non_negative_finite(self.base_delay_seconds, value_name="base retry delay")
        _require_non_negative_finite(self.max_delay_seconds, value_name="maximum retry delay")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must not be less than base_delay_seconds.")


@dataclass(frozen=True, slots=True)
class HttpClientConfig:
    timeouts: HttpTimeouts = field(default_factory=HttpTimeouts)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    user_agent: str = DEFAULT_PROVIDER_USER_AGENT
    max_response_bytes: int = RAW_DATA_PAYLOAD_DB_LIMIT_BYTES

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes < 1
            or self.max_response_bytes > RAW_DATA_PAYLOAD_DB_LIMIT_BYTES
        ):
            raise ValueError(
                "max_response_bytes must be positive and no greater than the audit payload limit."
            )
        normalized_user_agent = self.user_agent.strip()
        if (
            not normalized_user_agent
            or "\n" in normalized_user_agent
            or "\r" in normalized_user_agent
        ):
            raise ValueError("user_agent must be explicit and contain no line breaks.")
        try:
            normalize_json_without_credentials(
                normalized_user_agent,
                value_name="Provider User-Agent",
            )
        except AuditSecurityError as error:
            raise ValueError(str(error)) from None
        object.__setattr__(self, "user_agent", normalized_user_agent)


@dataclass(frozen=True, slots=True)
class TransportRequest:
    method: str
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    timeouts: HttpTimeouts
    max_response_bytes: int

    def __post_init__(self) -> None:
        normalized_headers: dict[str, str] = {}
        for name, value in self.headers.items():
            normalized_name = name.strip()
            if (
                not normalized_name
                or "\n" in normalized_name
                or "\r" in normalized_name
                or "\n" in value
                or "\r" in value
            ):
                raise ProviderValidationError("Provider HTTP headers contain invalid line breaks.")
            normalized_headers[normalized_name] = value.strip()
        object.__setattr__(self, "headers", normalized_headers)


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    fetched_at: datetime

    def __post_init__(self) -> None:
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or not 100 <= self.status_code <= 599
        ):
            raise ProviderValidationError("Transport HTTP status must be between 100 and 599.")
        if not isinstance(self.body, bytes):
            raise ProviderValidationError("Transport response body must be bytes.")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ProviderValidationError("Transport fetched_at must be timezone-aware.")
        object.__setattr__(self, "headers", dict(self.headers))


class HttpTransport(Protocol):
    def send(self, request: TransportRequest) -> TransportResponse: ...


class ProviderHttpClient:
    def __init__(
        self,
        *,
        transport: HttpTransport,
        config: HttpClientConfig | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self.config = config or HttpClientConfig()
        self._sleeper = sleeper

    def fetch(
        self,
        *,
        provider_key: str,
        provider_version: str,
        request: ProviderRequest,
        headers: Mapping[str, str] | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ProviderResult:
        supplied_headers = {
            name: value
            for name, value in dict(headers or {}).items()
            if name.strip().lower() != "user-agent"
        }
        transport_headers = {**supplied_headers, "User-Agent": self.config.user_agent}
        try:
            descriptor = build_provider_request_context_descriptor(
                capability=request.capability.value,
                scope=request.scope,
                method=request.method,
                source_url=request.source_url,
                request_identity=request.request_identity,
            )
            normalized_metadata = normalize_json_without_credentials(
                dict(metadata or {}),
                value_name="Provider metadata",
            )
        except AuditSecurityError as error:
            raise ProviderValidationError(str(error)) from None
        if not isinstance(normalized_metadata, dict):
            raise ProviderValidationError("Provider metadata must be an object.")

        transport_request = TransportRequest(
            method=descriptor.method,
            url=request.source_url,
            headers=transport_headers,
            timeouts=self.config.timeouts,
            max_response_bytes=self.config.max_response_bytes,
        )
        safe_identity = descriptor.identity
        safe_metadata = cast(dict[str, object], normalized_metadata)

        for attempt_number in range(1, self.config.retry_policy.max_attempts + 1):
            try:
                response = self._transport.send(transport_request)
                return self._build_result(
                    provider_key=provider_key,
                    provider_version=provider_version,
                    request=request,
                    descriptor_method=descriptor.method,
                    safe_source_url=descriptor.source_url.stored,
                    request_fingerprint=descriptor.fingerprint,
                    safe_identity=safe_identity,
                    response=response,
                    metadata={**safe_metadata, "attempt_count": attempt_number},
                )
            except TimeoutError:
                caught_error: ProviderError = ProviderTimeoutError("Provider request timed out.")
            except OSError:
                caught_error = ProviderTemporaryError("Provider transport failed temporarily.")
            except ProviderError as provider_error:
                caught_error = provider_error

            if (
                not caught_error.retryable
                or attempt_number >= self.config.retry_policy.max_attempts
            ):
                raise caught_error
            self._sleeper(self._retry_delay(error=caught_error, attempt_number=attempt_number))

        raise RuntimeError("Provider retry loop ended unexpectedly.")

    def _build_result(
        self,
        *,
        provider_key: str,
        provider_version: str,
        request: ProviderRequest,
        descriptor_method: str,
        safe_source_url: str,
        request_fingerprint: str,
        safe_identity: Mapping[str, object],
        response: TransportResponse,
        metadata: Mapping[str, object],
    ) -> ProviderResult:
        content_length = _header_value(response.headers, "content-length")
        declared_size = int(content_length) if content_length.isdigit() else None
        observed_size = len(response.body)
        if (
            declared_size is not None and declared_size > self.config.max_response_bytes
        ) or observed_size > self.config.max_response_bytes:
            raise ProviderResponseTooLargeError(
                limit_bytes=self.config.max_response_bytes,
                observed_bytes=observed_size,
            )
        _raise_for_status(response)
        try:
            ensure_payload_has_no_credentials(response.body)
        except AuditSecurityError:
            raise ProviderValidationError(
                "Provider response contains credential-like data."
            ) from None
        return ProviderResult(
            provider_key=provider_key,
            provider_version=provider_version,
            capability=request.capability,
            scope=request.scope,
            request_started_at=request.request_started_at,
            source_url=safe_source_url,
            request_method=descriptor_method,
            request_fingerprint=request_fingerprint,
            request_identity=safe_identity,
            http_status=response.status_code,
            content_type=_header_value(response.headers, "content-type"),
            raw_content=response.body,
            fetched_at=response.fetched_at,
            metadata=metadata,
        )

    def _retry_delay(self, *, error: ProviderError, attempt_number: int) -> float:
        if isinstance(error, ProviderRateLimitError) and error.retry_after_seconds is not None:
            return min(float(error.retry_after_seconds), self.config.retry_policy.max_delay_seconds)
        exponential = self.config.retry_policy.base_delay_seconds * math.pow(
            2.0,
            attempt_number - 1,
        )
        return min(exponential, self.config.retry_policy.max_delay_seconds)


def _raise_for_status(response: TransportResponse) -> None:
    status = response.status_code
    if 200 <= status <= 299:
        return
    if status in {401, 403}:
        raise ProviderAuthenticationError(
            f"Provider authentication failed with HTTP {status}.",
            http_status=status,
        )
    if status == 408:
        raise ProviderTimeoutError("Provider returned HTTP 408.", http_status=status)
    if status == 429:
        raise ProviderRateLimitError(
            http_status=status,
            retry_after_seconds=_parse_retry_after(response.headers),
        )
    if 500 <= status <= 599:
        raise ProviderTemporaryError(
            f"Provider returned temporary HTTP {status}.",
            http_status=status,
        )
    raise ProviderPermanentError(
        f"Provider returned permanent HTTP {status}.",
        http_status=status,
    )


def _parse_retry_after(headers: Mapping[str, str]) -> int | None:
    value = _header_value(headers, "retry-after").strip()
    if not value.isdigit():
        return None
    return int(value)


def _header_value(headers: Mapping[str, str], name: str) -> str:
    normalized_name = name.lower()
    for header_name, value in headers.items():
        if header_name.lower() == normalized_name:
            return value
    return ""


def _require_positive_finite(value: float, *, value_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{value_name} must be a positive finite number.")


def _require_non_negative_finite(value: float, *, value_name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{value_name} must be a non-negative finite number.")
