from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import ClassVar

from audit.security import (
    AuditSecurityError,
    ProviderRequestContextDescriptor,
    build_provider_request_context_descriptor,
)
from providers.exceptions import ProviderValidationError
from providers.types import ProviderCapability, ProviderRequest, ProviderResult

_PROVIDER_KEY_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")


class Provider(ABC):
    provider_key: ClassVar[str]
    provider_version: ClassVar[str]
    capabilities: ClassVar[frozenset[ProviderCapability]]

    def fetch(self, request: ProviderRequest) -> ProviderResult:
        expected_descriptor = self.describe_request(request)
        result = self._fetch(request)
        try:
            echoed_descriptor = build_provider_request_context_descriptor(
                capability=result.capability.value,
                scope=result.scope,
                method=result.request_method,
                source_url=result.source_url,
                request_identity=result.request_identity,
            )
        except AuditSecurityError as error:
            raise ProviderValidationError(str(error)) from None
        if (
            result.provider_key != self.provider_key
            or result.provider_version != self.provider_version
            or result.request_started_at != request.request_started_at
            or echoed_descriptor.descriptor_version != expected_descriptor.descriptor_version
            or echoed_descriptor.capability != expected_descriptor.capability
            or echoed_descriptor.scope != expected_descriptor.scope
            or echoed_descriptor.method != expected_descriptor.method
            or echoed_descriptor.source_url.canonical != expected_descriptor.source_url.canonical
            or echoed_descriptor.identity != expected_descriptor.identity
            or result.request_fingerprint != expected_descriptor.fingerprint
        ):
            raise ProviderValidationError(
                "Provider result does not match the provider identity or request context."
            )
        return result

    def describe_request(self, request: ProviderRequest) -> ProviderRequestContextDescriptor:
        """Rebuild the trusted, credential-free request context before any fetch."""

        self._validate_contract()
        if request.capability not in self.capabilities:
            raise ProviderValidationError(
                f"Provider {self.provider_key} does not support {request.capability.value}."
            )
        try:
            return build_provider_request_context_descriptor(
                capability=request.capability.value,
                scope=request.scope,
                method=request.method,
                source_url=request.source_url,
                request_identity=request.request_identity,
            )
        except AuditSecurityError as error:
            raise ProviderValidationError(str(error)) from None

    @abstractmethod
    def _fetch(self, request: ProviderRequest) -> ProviderResult:
        raise NotImplementedError

    def _validate_contract(self) -> None:
        if not _PROVIDER_KEY_RE.fullmatch(self.provider_key):
            raise ProviderValidationError("Provider key must be a stable lowercase identifier.")
        if not self.provider_version.strip() or len(self.provider_version.strip()) > 100:
            raise ProviderValidationError("Provider version must contain 1 to 100 characters.")
        if not self.capabilities or not all(
            isinstance(capability, ProviderCapability) for capability in self.capabilities
        ):
            raise ProviderValidationError("Provider capabilities must use the supported enum.")
