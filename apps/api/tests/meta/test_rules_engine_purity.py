"""S14a AC 11: the Rules Engine package stays pure (invariant 2; CANON.md:126).

An AST walk allows only stdlib modules with no clock, randomness, env or I/O, plus the contracts
package and pydantic. The import contract and ruff TID251 cover the DB, web and model layers.
"""

from __future__ import annotations

import ast
import configparser
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path("apps/api/app/services/rules_engine")

ALLOWED_MODULES = frozenset(
    {
        "__future__",
        "bisect",
        "collections",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "functools",
        "itertools",
        "math",
        "operator",
        "re",
        "string",
        "typing",
        "unicodedata",
        "uuid",
        "pydantic",
    }
)
BANNED_CALLS = frozenset(
    {"datetime.now", "datetime.utcnow", "date.today", "uuid.uuid1", "uuid.uuid4", "uuid1", "uuid4"}
)
# The stdlib regex engine's own parser, constants, case-fold table and C core: pure, no I/O.
IMPORTLIB_ALLOWED_ARGS = frozenset({"re._parser", "re._constants", "re._casefix", "_sre"})


def _module_allowed(name: str) -> bool:
    return name in ALLOWED_MODULES or name == "mkos_schemas" or name.startswith("mkos_schemas.")


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def check_source(source: str, filename: str) -> list[str]:
    """Return one message per disallowed import or call in ``source``."""
    problems: list[str] = []
    tree = ast.parse(source, filename=filename)
    is_report = Path(filename).name == "report.py"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    continue  # its use is checked call by call below
                if not _module_allowed(alias.name):
                    problems.append(f"{filename}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue  # relative imports stay inside the package
            module = node.module or ""
            if module == "importlib" or not _module_allowed(module):
                problems.append(f"{filename}:{node.lineno} from {module} import")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in BANNED_CALLS:
                problems.append(f"{filename}:{node.lineno} call {name}")
            if name is not None and name.startswith("importlib"):
                literal = (
                    name == "importlib.import_module"
                    and len(node.args) == 1
                    and not node.keywords
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in IMPORTLIB_ALLOWED_ARGS
                )
                if not literal:
                    problems.append(f"{filename}:{node.lineno} call {name}")
            if (
                is_report
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"format", "format_map"}
            ):
                problems.append(f"{filename}:{node.lineno} str.{node.func.attr} in report.py")
    return problems


def test_rules_engine_import_allowlist(repo_root: Path) -> None:
    files = sorted((repo_root / PACKAGE).glob("*.py"))
    assert files, "the rules_engine package is missing"
    problems: list[str] = []
    for path in files:
        problems += check_source(path.read_text(encoding="utf-8"), path.name)
    assert problems == []


@pytest.mark.parametrize(
    ("source", "filename"),
    [
        ("import time\n", "text.py"),
        ("import random\n", "text.py"),
        ("import os\n", "text.py"),
        ("from datetime import datetime\ndatetime.now()\n", "text.py"),
        ("import logging\n", "text.py"),
        ("from app.db import x\n", "text.py"),
        ('import importlib\nimportlib.import_module("os")\n', "text.py"),
        ('"{value}".format(value=1)\n', "report.py"),
    ],
)
def test_allowlist_flags_seeded_sources(source: str, filename: str) -> None:
    assert check_source(source, filename) != []


def test_allowlist_accepts_the_regex_parser_import() -> None:
    source = 'import importlib\nP = importlib.import_module("re._parser")\n'
    assert check_source(source, "text.py") == []


def test_ruff_bans_openai_in_engine_package(repo_root: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--stdin-filename",
            "apps/api/app/services/rules_engine/evaluators.py",
            "-",
        ],
        input="import openai\n\nCLIENT = openai\n",
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "TID251" in result.stdout


def test_importlinter_has_rules_engine_contract(repo_root: Path) -> None:
    parser = configparser.ConfigParser()
    parser.read(repo_root / ".importlinter", encoding="utf-8")
    section = parser["importlinter:contract:rules-engine-is-pure"]
    assert section["type"] == "forbidden"
    assert section["source_modules"].split() == ["app.services.rules_engine"]
    assert set(section["forbidden_modules"].split()) == {
        "sqlalchemy",
        "psycopg",
        "fastapi",
        "starlette",
        "app.db",
        "app.repositories",
        "app.api",
        "app.workers",
        "app.adapters",
        "app.core",
        "app.cli",
    }
    assert "allow_indirect_imports" not in section
