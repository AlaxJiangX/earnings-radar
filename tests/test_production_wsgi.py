"""Stage 8.1: the app must serve real requests with DEBUG disabled.

Settings validation (tests/test_settings.py) already refuses DEBUG outside
development/test.  These tests exercise the runtime behaviour flip that
matters on a production path: with DEBUG off, Django serves views normally
but enforces ALLOWED_HOSTS with a bare 400 instead of a diagnostics page.
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client

pytestmark = pytest.mark.django_db


def test_health_endpoint_serves_with_debug_disabled(client: Client) -> None:
    previous_debug = settings.DEBUG
    try:
        settings.DEBUG = False
        response = client.get("/health/")
    finally:
        settings.DEBUG = previous_debug

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_debug_disabled_rejects_unknown_hosts_with_bare_400(client: Client) -> None:
    previous_debug = settings.DEBUG
    try:
        settings.DEBUG = False
        response = client.get("/health/", HTTP_HOST="attacker.example")
    finally:
        settings.DEBUG = previous_debug

    assert response.status_code == 400
