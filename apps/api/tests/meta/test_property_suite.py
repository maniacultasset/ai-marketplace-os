"""S10 C6: the Schemathesis property suite needs PostgreSQL (real bearers, limiters), so it lives
in the integration suite and never under tests/unit (which runs with no database)."""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]


def _imports_schemathesis(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "schemathesis" for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "schemathesis":
            return True
    return False


def test_property_suite_not_in_unit_dir() -> None:
    unit = sorted(p for p in (TESTS / "unit").rglob("*.py") if _imports_schemathesis(p))
    assert unit == []
    assert _imports_schemathesis(TESTS / "integration" / "test_api_surface_property.py")
