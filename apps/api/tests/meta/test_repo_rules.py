"""AC5, AC17: scripts/ci/check_repo_rules.py (R1 env access, R2 error_code literals,
R3 async-engine imports in services; ADR-0013 §3; KNOWN-ISSUES.md:35)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

RULES = "scripts/ci/check_repo_rules.py"


@pytest.fixture
def rules(script: Callable[[str], ModuleType]) -> ModuleType:
    return script(RULES)


def _tree(root: Path, files: dict[str, str]) -> Path:
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _found(rules: ModuleType, root: Path) -> list[tuple[str, int, str]]:
    violations: list[Any] = rules.find_violations(root)
    return [(v.path, v.line, v.rule) for v in violations]


def test_real_tree_passes(rules: ModuleType, repo_root: Path) -> None:
    assert rules.find_violations(repo_root) == []
    assert rules.main(["--root", str(repo_root)]) == 0


@pytest.mark.parametrize(
    "body",
    [
        "import os\nX = os.environ['A']\n",
        "import os\nX = os.environ.get('A')\n",
        "import os\nX = os.getenv('A')\n",
        "from os import environ\n",
        "from os import getenv as g\n",
        "import os as o\nX = o.environ\n",
        "import os\nX = os.environb\n",
        "import os\nos.putenv('A', 'b')\n",
        "def f():\n    import os as q\n    return q.unsetenv('A')\n",
    ],
)
def test_flags_env_access_outside_config(rules: ModuleType, tmp_path: Path, body: str) -> None:
    root = _tree(tmp_path, {"apps/api/app/x.py": body})
    found = _found(rules, root)
    assert found, body
    assert {rule for _, _, rule in found} == {"R1"}
    assert rules.main(["--root", str(root)]) == 1


def test_allows_env_access_in_config_py(rules: ModuleType, tmp_path: Path) -> None:
    body = "import os\nX = os.environ.get('A')\n"
    root = _tree(tmp_path, {"apps/api/app/core/config.py": body})
    assert _found(rules, root) == []


def test_ignores_mentions_in_strings_and_comments(rules: ModuleType, tmp_path: Path) -> None:
    body = (
        '"""Never read os.environ here."""\n'
        "# os.getenv('A') is banned\n"
        "X = 'os.environ'\n"
        "environ = {}\n"
        "Y = environ.get('A')\n"
    )
    root = _tree(tmp_path, {"apps/api/app/x.py": body})
    assert _found(rules, root) == []


@pytest.mark.parametrize(
    "body",
    ["f(error_code='RULE_FAILED')\n", "f(error_code=f'RULE_{n}')\n"],
)
def test_flags_error_code_literals(rules: ModuleType, tmp_path: Path, body: str) -> None:
    root = _tree(tmp_path, {"apps/api/app/x.py": body})
    assert _found(rules, root) == [("apps/api/app/x.py", 1, "R2")]


def test_allows_error_code_enum_member(rules: ModuleType, tmp_path: Path) -> None:
    body = "f(error_code=ErrorCode.RULE_FAILED)\nf(error_code=code)\nf(code='X')\n"
    root = _tree(tmp_path, {"apps/api/app/x.py": body})
    assert _found(rules, root) == []


def test_out_of_scope_paths_ignored(rules: ModuleType, tmp_path: Path) -> None:
    bad = "import os\nX = os.environ\nf(error_code='X')\n"
    root = _tree(
        tmp_path,
        {
            "apps/api/tests/test_x.py": bad,
            "scripts/docgen/x.py": bad,
            "scripts/trace/x.py": bad,
            "scaffold/apps/api/app/x.py": bad,
            "docs/x.py": bad,
        },
    )
    assert _found(rules, root) == []
    in_scope = _tree(tmp_path, {"scripts/ci/x.py": bad, "packages/schemas/src/m/x.py": bad})
    assert {path for path, _, _ in _found(rules, in_scope)} == {
        "scripts/ci/x.py",
        "packages/schemas/src/m/x.py",
    }


def test_unparseable_file_fails_closed(rules: ModuleType, tmp_path: Path) -> None:
    root = _tree(tmp_path, {"apps/api/app/x.py": "def broken(:\n"})
    assert _found(rules, root) == [("apps/api/app/x.py", 1, "R0")]
    assert rules.main(["--root", str(root)]) == 1


def test_flags_async_engine_import_in_services(rules: ModuleType, tmp_path: Path) -> None:
    body = (
        "import sqlalchemy.ext.asyncio as sa_async\n"
        "from sqlalchemy.ext.asyncio import AsyncSession\n"
        "from sqlalchemy.ext import asyncio\n"
    )
    root = _tree(
        tmp_path,
        {"apps/api/app/services/x.py": body, "apps/api/app/db/session.py": body},
    )
    assert _found(rules, root) == [
        ("apps/api/app/services/x.py", 1, "R3"),
        ("apps/api/app/services/x.py", 2, "R3"),
        ("apps/api/app/services/x.py", 3, "R3"),
    ]
