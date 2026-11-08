"""SI7 (CODE-PLAN §12 LOW 9): besides `app/workers/generation.py`, only
`app/workers/image_edit_seam.py` reaches `mkos_pipeline`, and it does so lazily (`importlib`), so
an older pipeline pin degrades to "not available"."""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"


def _reaches_pipeline(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "mkos_pipeline" for a in node.names
        ):
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "mkos_pipeline"
        ):
            return True
        if isinstance(node, ast.Constant) and node.value == "mkos_pipeline":
            return True
    return False


def test_only_two_modules_reach_the_pipeline() -> None:
    hits = sorted(
        str(path.relative_to(APP))
        for path in APP.rglob("*.py")
        if _reaches_pipeline(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert hits == ["workers/generation.py", "workers/image_edit_seam.py"]


def test_the_seam_imports_lazily() -> None:
    tree = ast.parse((APP / "workers" / "image_edit_seam.py").read_text(encoding="utf-8"))
    static = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom) and "mkos_pipeline" in ast.dump(n)
    ]
    assert static == []
