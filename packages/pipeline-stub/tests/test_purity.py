"""S14b T2 (AC 4; CANON.md:125): the stub imports no sqlalchemy, FastAPI or app.*, opens no DB
session, reads no os.environ and is deterministic (no clock, no randomness).

An AST allowlist walk over every module of `mkos_pipeline`; the import contract
`pipeline-never-imports-app` covers the installed forbidden packages (C3)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "mkos_pipeline"

ALLOWED_MODULES = frozenset(
    {
        "__future__",
        # S26: `stub:timeout` awaits one bounded `asyncio.sleep` (no clock read, no loop time)
        "asyncio",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "importlib.resources",
        "json",
        "typing",
        "uuid",
        "pydantic",
    }
)
BANNED_CALLS = frozenset(
    {"datetime.now", "datetime.utcnow", "date.today", "uuid.uuid1", "uuid.uuid4", "uuid1", "uuid4"}
)
BANNED_NAMES = frozenset({"environ", "getenv", "now", "utcnow", "uuid1", "uuid4"})


def _allowed(name: str) -> bool:
    return name in ALLOWED_MODULES or name == "mkos_schemas" or name.startswith("mkos_schemas.")


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def check_source(source: str, filename: str = "<seeded>") -> list[str]:
    """One message per import outside the allowlist, banned call or env/clock lookup."""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed(alias.name):
                    problems.append(f"{filename}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue
            module = node.module or ""
            if not _allowed(module):
                problems.append(f"{filename}:{node.lineno} from {module} import")
            for alias in node.names:
                if alias.name in BANNED_NAMES:
                    problems.append(f"{filename}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name is not None and (name in BANNED_CALLS or name.split(".")[-1] in BANNED_NAMES):
                problems.append(f"{filename}:{node.lineno} calls {name}")
        elif isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
            problems.append(f"{filename}:{node.lineno} reads {node.attr}")
    return problems


def test_stub_import_allowlist() -> None:
    files = sorted(PACKAGE.rglob("*.py"))
    assert files, PACKAGE
    problems = [p for f in files for p in check_source(f.read_text(encoding="utf-8"), str(f))]
    assert problems == []


@pytest.mark.parametrize(
    "source",
    [
        "import openai",
        "import langgraph",
        "import boto3",
        "import procrastinate",
        "from app.db import x",
        "import os; os.environ",
        "from datetime import datetime; datetime.now()",
        "import uuid; uuid.uuid4()",
        "import sys",
        "import socket",
        "import random",
        "import time",
        "import sqlalchemy",
        "from os import environ",
    ],
)
def test_allowlist_flags_seeded_sources(source: str) -> None:
    assert check_source(source) != []
