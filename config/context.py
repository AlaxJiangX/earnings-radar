"""Site-wide template context injected on every page."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest


def site_globals(request: HttpRequest) -> dict[str, Any]:
    """Expose settings-level constants (e.g. ``GITHUB_URL``) to all templates."""

    return {"GITHUB_URL": settings.GITHUB_URL}
