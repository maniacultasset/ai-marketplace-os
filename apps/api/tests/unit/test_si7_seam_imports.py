"""SI7 (CODE-PLAN §12 LOW 9), SI12: besides `app/workers/generation.py`, only
`app/workers/image_edit_seam.py` and `app/workers/size_chart_seam.py` reach `mkos_pipeline`, and
they do so lazily (`importlib`), so an older pipeline pin degrades to "not available"."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

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


def test_only_three_modules_reach_the_pipeline() -> None:
    hits = sorted(
        str(path.relative_to(APP))
        for path in APP.rglob("*.py")
        if _reaches_pipeline(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert hits == [
        "workers/classify_seam.py",  # SI14: `classify_product`
        "workers/column_map_seam.py",  # SI14: `map_columns`
        "workers/error_diagnosis.py",  # SI13: the diagnosis prompt over `complete_json`
        "workers/generation.py",
        "workers/image_edit_seam.py",
        "workers/size_chart_seam.py",
    ]


@pytest.mark.parametrize(
    "seam",
    [
        "image_edit_seam.py",
        "size_chart_seam.py",
        "error_diagnosis.py",
        "classify_seam.py",
        "column_map_seam.py",
    ],
)
def test_the_seam_imports_lazily(seam: str) -> None:
    tree = ast.parse((APP / "workers" / seam).read_text(encoding="utf-8"))
    static = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom) and "mkos_pipeline" in ast.dump(n)
    ]
    assert static == []
