"""Alpha Vantage Free canonical candidate-entry provider adapter."""

from __future__ import annotations

from providers.alpha_vantage_reference import AlphaVantageReferenceProvider

ALPHA_VANTAGE_CANONICAL_PROVIDER_VERSION = "alpha-vantage-canonical-v1"


class AlphaVantageCanonicalProvider(AlphaVantageReferenceProvider):
    """Canonical-entry adapter reusing the approved reference HTTP transport."""

    provider_version = ALPHA_VANTAGE_CANONICAL_PROVIDER_VERSION
