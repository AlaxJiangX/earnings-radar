from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import ClassVar

from audit.security import sanitize_url
from providers.exceptions import ProviderValidationError
from providers.types import ProviderCapability, ProviderRequest, ProviderResult

_PROVIDER_KEY_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")


class Provider(ABC):
    provider_key: ClassVar[str]
    provider_version: ClassVar[str]
    capabilities: ClassVar[frozenset[ProviderCapability]]

    def fetch(self, request: ProviderRequest) -> ProviderResult:
        self._validate_contract()
        if request.capability not in self.capabilities:
            raise ProviderValidationError(
                f"Provider {self.provider_key} does not support {request.capability.value}."
            )
        result = self._fetch(request)
        expected_source_url = sanitize_url(request.source_url).stored
        if (
            result.provider_key != self.provider_key
            or result.provider_version != self.provider_version
            or result.capability != request.capability
            or dict(result.scope) != dict(request.scope)
            or result.request_started_at != request.request_started_at
            or result.request_method != request.method
            or result.source_url != expected_source_url
        ):
            raise ProviderValidationError(
                "Provider result does not match the provider identity or request context."
            )
        return result

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
