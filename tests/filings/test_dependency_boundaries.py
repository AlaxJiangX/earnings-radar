"""Automatic app dependency-direction enforcement for the filings app."""

from __future__ import annotations

import ast
from pathlib import Path

FILINGS_ROOT = Path(__file__).resolve().parents[2] / "filings"
FORBIDDEN_IMPORT_PREFIX = "earnings"


def test_filings_does_not_import_earnings() -> None:
    violations: list[str] = []
    for path in sorted(FILINGS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.append(node.module)
            else:
                continue
            for module_name in imported_modules:
                if module_name == FORBIDDEN_IMPORT_PREFIX or module_name.startswith(
                    f"{FORBIDDEN_IMPORT_PREFIX}."
                ):
                    relative = path.relative_to(FILINGS_ROOT)
                    violations.append(f"{relative}:{node.lineno}:{module_name}")

    assert violations == []
