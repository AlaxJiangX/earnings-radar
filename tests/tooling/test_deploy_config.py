"""Stage 8.1: the gunicorn configuration file loads with sane values.

The gunicorn dependency itself is pending merge-gate approval
(docs/deployment.md section 0); this only validates that the config module
is plain, environment-driven Python that gunicorn can exec later.
"""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

DEPLOY_CONFIG_PATH = Path(__file__).resolve().parents[2] / "deploy" / "gunicorn.conf.py"


def _load(
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    for key in ("PORT", "GUNICORN_WEB_WORKERS", "GUNICORN_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    return runpy.run_path(str(DEPLOY_CONFIG_PATH))


def test_gunicorn_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = _load(monkeypatch)

    assert loaded["bind"] == "0.0.0.0:8000"
    assert loaded["workers"] == 2
    assert loaded["worker_class"] == "sync"
    assert loaded["timeout"] == 60
    assert loaded["accesslog"] == "-"
    assert loaded["errorlog"] == "-"


def test_gunicorn_config_reads_platform_port_and_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _load(
        monkeypatch,
        env={
            "PORT": "9001",
            "GUNICORN_WEB_WORKERS": "1",
            "GUNICORN_TIMEOUT_SECONDS": "30",
        },
    )

    assert loaded["bind"] == "0.0.0.0:9001"
    assert loaded["workers"] == 1
    assert loaded["timeout"] == 30
