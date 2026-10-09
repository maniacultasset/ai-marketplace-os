"""S18 T20: generation never publishes, and only its worker imports the pipeline."""

from __future__ import annotations

import ast
from pathlib import Path

ROOTS = (
    Path("apps/api/app/workers/generation.py"),
    Path("apps/api/app/services"),
)


def _sources() -> list[Path]:
    found: list[Path] = []
    for root in ROOTS:
        if root.is_file():
            found.append(root)
        else:
            found.extend(sorted(root.glob("generation*.py")))
    return found


def test_generation_never_publishes() -> None:
    banned_names = {"listing_publish", "approved_by_user_id", "approved_at", "LISTING_PUBLISH"}
    banned_targets = {"approved", "publishing"}
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in banned_names, f"{path}:{node.lineno} {node.id}"
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned_names, f"{path}:{node.lineno} {node.attr}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in banned_names, f"{path}:{node.lineno} {node.value!r}"
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "to" and isinstance(keyword.value, ast.Constant):
                        assert keyword.value.value not in banned_targets, path


def test_only_generation_worker_imports_mkos_pipeline() -> None:
    hits: list[str] = []
    for path in Path("apps/api/app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported = False
            if isinstance(node, ast.Import):
                imported = any(
                    alias.name == "mkos_pipeline" or alias.name.startswith("mkos_pipeline.")
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = node.module == "mkos_pipeline" or node.module.startswith(
                    "mkos_pipeline."
                )
            if imported:
                hits.append(path.as_posix())
    assert hits == ["apps/api/app/workers/generation.py"]
