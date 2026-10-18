from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCHLISTS_ROOT = PROJECT_ROOT / "watchlists"
FORBIDDEN_WATCHLIST_DEPENDENCIES = {"earnings", "filings", "notifications", "providers"}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize(
    "path",
    sorted(path for path in WATCHLISTS_ROOT.glob("*.py") if path.name not in {"__init__.py"}),
    ids=lambda path: path.name,
)
def test_watchlists_does_not_import_downstream_or_provider_modules(path: Path) -> None:
    imported_roots = {module.split(".", maxsplit=1)[0] for module in _imported_modules(path)}
    assert imported_roots.isdisjoint(FORBIDDEN_WATCHLIST_DEPENDENCIES)


def test_watchlist_recompute_uses_public_index_selector_boundary() -> None:
    imports = _imported_modules(WATCHLISTS_ROOT / "services.py")
    assert "indexes.selectors" in imports
    assert "indexes.models" not in imports


def test_monitoring_pool_v2_does_not_import_watchlists() -> None:
    path = PROJECT_ROOT / "earnings" / "services" / "monitoring_pool.py"
    imported_roots = {module.split(".", maxsplit=1)[0] for module in _imported_modules(path)}
    assert "watchlists" not in imported_roots
