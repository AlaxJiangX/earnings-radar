from __future__ import annotations

from providers.base import Provider
from providers.exceptions import ProviderValidationError
from providers.types import ProviderCapability, ProviderRequest, ProviderResult


def require_scope_index_code(request: ProviderRequest) -> str:
    """Extract and validate ``index_code`` from an ``INDEX_CONSTITUENTS`` scope.

    This helper is shared by all index constituent providers so that
    ``index_code`` validation (non-empty string, optionally constrained
    to known index codes) stays consistent regardless of the underlying
    HTTP transport or source format.

    Args:
        request: A ``ProviderRequest`` whose ``capability`` must be
            ``INDEX_CONSTITUENTS`` and whose ``scope`` must contain a
            non-empty ``"index_code"`` key.

    Returns:
        The uppercased, stripped index code (e.g. ``"SP500"``).

    Raises:
        ProviderValidationError: If ``index_code`` is missing, empty, or
            not a string.
    """
    scope = dict(request.scope)
    code = scope.get("index_code")
    if not isinstance(code, str) or not code.strip():
        raise ProviderValidationError(
            "INDEX_CONSTITUENTS request scope must include a non-empty 'index_code'."
        )
    return code.strip().upper()


class IndexConstituentProvider(Provider):
    """Abstract base for providers that return index constituent snapshots.

    All real (and fixture) ``INDEX_CONSTITUENTS`` providers **must** extend
    this class rather than ``Provider`` directly.  It declares the single
    allowed capability and provides the shared ``index_code`` scope
    validation needed by every concrete implementation.

    Concrete subclasses only need to implement:

    * ``provider_key`` / ``provider_version`` (class-level constants)
    * ``_fetch(request)`` — the actual HTTP (or fixture) logic that
      returns a ``ProviderResult`` whose ``raw_content`` can later be
      parsed via :func:`indexes.constituents.parse_index_constituent_snapshot`.

    Usage::

        class MyIndexProvider(IndexConstituentProvider):
            provider_key = "my-index-source"
            provider_version = "v1"

            def _fetch(self, request: ProviderRequest) -> ProviderResult:
                index_code = require_scope_index_code(request)
                # … talk to the upstream API …
    """

    capabilities = frozenset({ProviderCapability.INDEX_CONSTITUENTS})

    def fetch(self, request: ProviderRequest) -> ProviderResult:
        """Validate capability then scope, then delegate to ``_fetch``."""
        if request.capability not in self.capabilities:
            raise ProviderValidationError(
                f"Provider {self.provider_key} does not support {request.capability.value}."
            )
        _ = require_scope_index_code(request)
        return super().fetch(request)
