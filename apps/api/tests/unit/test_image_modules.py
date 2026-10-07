"""S17b T17 (invariants 4 and 7): the image modules import no model client and hold no
marketplace branch; every threshold lives in rule rows."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
MODULES = sorted(
    [*APP.glob("services/image_*.py"), APP / "services/images.py", APP / "api/v1/routers/images.py"]
)
MODEL_CLIENTS = {"openai", "anthropic", "langgraph", "langchain", "mkos_pipeline", "google"}


def _ids(path: Path) -> str:
    return str(path.relative_to(APP))


@pytest.mark.parametrize("path", MODULES, ids=_ids)
def test_image_modules_import_no_model_client(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        assert not {n.split(".")[0] for n in names} & MODEL_CLIENTS, (path, names)


def _is_marketplace_member(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Marketplace"
    )


@pytest.mark.parametrize("path", MODULES, ids=_ids)
def test_image_modules_have_no_marketplace_branch(path: Path) -> None:
    """No "amazon"/"flipkart" literal, and no comparison against a `Marketplace` member."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.casefold() not in {"amazon", "flipkart"}, (path, node.lineno)
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            assert not any(_is_marketplace_member(o) for o in operands), (path, node.lineno)


def test_image_modules_found() -> None:
    names = {_ids(p) for p in MODULES}
    assert {"services/image_runner.py", "services/image_gate.py", "services/images.py"} <= names
