"""S20 T20 (AC5; invariants 1 and 7): no adapter module imports an AI package. import-linter can
name only installed packages (`adapters-no-ai` covers `mkos_pipeline`), so this AST walk covers
`openai`, `langgraph` and `langchain` too, over every module under `app/adapters`."""

from __future__ import annotations

import ast
from pathlib import Path

ADAPTERS = Path(__file__).resolve().parents[2] / "app" / "adapters"
BANNED = frozenset({"openai", "langgraph", "langchain", "langchain_openai", "mkos_pipeline"})


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_adapters_import_no_ai_packages() -> None:
    files = sorted(ADAPTERS.rglob("*.py"))
    assert any(p.parent.name == "amazon" for p in files)
    offenders = {str(p.relative_to(ADAPTERS)): _imports(p) & BANNED for p in files}
    assert {k: v for k, v in offenders.items() if v} == {}
