"""AC5, AC17: scripts/ci/check_repo_rules.py (R1 env access, R2 error_code literals,
R3 async-engine imports in services; ADR-0013 §3)."""

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


def test_flags_env_access_in_alembic_env(rules: ModuleType, tmp_path: Path) -> None:
    root = _tree(tmp_path, {"apps/api/alembic/env.py": "import os\nX = os.environ['X']\n"})
    assert _found(rules, root) == [("apps/api/alembic/env.py", 2, "R1")]
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


# ---- S9: R4 raw SQL outside app/db and app/repositories, R5 platform_scope, R6 SCOPE_OK ----


@pytest.mark.parametrize(
    ("relative", "body"),
    [
        ("apps/api/app/services/x.py", "from sqlalchemy import text\n"),
        ("apps/api/app/services/x.py", "from sqlalchemy import select, text as t\n"),
        ("apps/api/app/services/x.py", "from sqlalchemy.sql import text\n"),
        ("apps/api/app/services/x.py", "import sqlalchemy as sa\nQ = sa.text('SELECT 1')\n"),
        ("apps/api/app/workers/x.py", "async def f(s):\n    return await s.connection()\n"),
        ("apps/api/app/cli.py", "from sqlalchemy import text\n"),
        # S9 fix round 1 (F0-F2): attribute chains, submodule imports, TextClause, engine SQL.
        ("apps/api/app/services/x.py", "from sqlalchemy import sql\nQ = sql.text('x')\n"),
        ("apps/api/app/services/x.py", "import sqlalchemy\nQ = sqlalchemy.sql.text('x')\n"),
        ("apps/api/app/services/x.py", "import sqlalchemy as sa\nQ = sa.sql.text('x')\n"),
        ("apps/api/app/services/x.py", "import sqlalchemy.sql\nQ = sqlalchemy.sql.text('x')\n"),
        (
            "apps/api/app/services/x.py",
            "import sqlalchemy\nQ = sqlalchemy.sql.expression.text('x')\n",
        ),
        (
            "apps/api/app/services/x.py",
            "from sqlalchemy.sql import expression\nQ = expression.text('x')\n",
        ),
        ("apps/api/app/services/x.py", "from sqlalchemy import TextClause\nQ = TextClause('x')\n"),
        ("apps/api/app/services/x.py", "from sqlalchemy.sql.elements import TextClause\n"),
        ("apps/api/app/services/x.py", "from sqlalchemy import literal_column\n"),
        ("apps/api/app/services/x.py", "import sqlalchemy as sa\nQ = sa.TextClause('x')\n"),
        ("apps/api/app/services/x.py", "import sqlalchemy as sa\nQ = sa.literal_column('x')\n"),
        (
            "apps/api/app/workers/x.py",
            "async def f(engine):\n    async with engine.connect() as c:\n"
            "        await c.exec_driver_sql('x')\n",
        ),
        ("apps/api/app/workers/x.py", "async def f(c):\n    await c.exec_driver_sql('x')\n"),
        ("apps/api/app/workers/x.py", "def f(s):\n    return s.get_bind()\n"),
        ("apps/api/app/services/x.py", "async def f(s):\n    return await s.bind.connect()\n"),
    ],
)
def test_r4_flags_raw_sql(rules: ModuleType, tmp_path: Path, relative: str, body: str) -> None:
    root = _tree(tmp_path, {relative: body})
    assert {rule for _, _, rule in _found(rules, root)} == {"R4"}


@pytest.mark.parametrize(
    "relative",
    ["apps/api/app/db/seed_ops.py", "apps/api/app/repositories/x.py", "scripts/x.py"],
)
def test_r4_allows_db_and_repositories(rules: ModuleType, tmp_path: Path, relative: str) -> None:
    body = "from sqlalchemy import text\nasync def f(s):\n    return await s.connection()\n"
    root = _tree(tmp_path, {relative: body})
    assert _found(rules, root) == []


PLATFORM_IMPORTS = [
    "from app.db.session import platform_scope\n",
    "from app.db.session import platform_scope as ps\n",
    "from app.db.session import *\n",
    "import app.db.session as s\nasync def f():\n    async with s.platform_scope():\n        pass\n",
    "import app.db.session\nX = app.db.session.platform_scope\n",
    # S9 fix round 1 (F2): string lookups through getattr and friends.
    "import app.db.session as s\nX = getattr(s, 'platform_scope')\n",
    "def f(session):\n    return getattr(session, 'platform_scope', None)\n",
]


@pytest.mark.parametrize("body", PLATFORM_IMPORTS)
@pytest.mark.parametrize("relative", ["apps/api/app/workers/x.py", "apps/api/app/services/x.py"])
def test_r5_flags_platform_scope_outside_allowlist(
    rules: ModuleType, tmp_path: Path, relative: str, body: str
) -> None:
    root = _tree(tmp_path, {relative: body})
    assert {rule for _, _, rule in _found(rules, root)} == {"R5"}


@pytest.mark.parametrize(
    "relative",
    [
        "apps/api/app/admin/x.py",
        "apps/api/app/cli.py",
        "scripts/x.py",
        "apps/api/app/db/session.py",
    ],
)
def test_r5_allows_admin_cli_scripts(rules: ModuleType, tmp_path: Path, relative: str) -> None:
    root = _tree(tmp_path, {relative: PLATFORM_IMPORTS[0] + PLATFORM_IMPORTS[3]})
    assert _found(rules, root) == []


@pytest.mark.parametrize(
    "body",
    [
        "from app.db.guard import SCOPE_OK\n",
        "from app.db.guard import SCOPE_OK as K\n",
        "from app.db.guard import *\n",
        "import app.db.guard as g\nX = g.SCOPE_OK\n",
        "import app.db.guard as g\nX = getattr(g, 'SCOPE_OK')\n",
        "def f(s):\n    setattr(s, 'SCOPE_OK', True)\n",
        "def f(s):\n    return hasattr(s, 'SCOPE_OK')\n",
    ],
)
@pytest.mark.parametrize(
    "relative", ["apps/api/app/services/x.py", "apps/api/app/repositories/products.py"]
)
def test_r6_flags_scope_ok_outside_db(
    rules: ModuleType, tmp_path: Path, relative: str, body: str
) -> None:
    root = _tree(tmp_path, {relative: body})
    assert {rule for _, _, rule in _found(rules, root)} == {"R6"}


@pytest.mark.parametrize(
    "relative", ["apps/api/app/db/session.py", "apps/api/app/repositories/base.py"]
)
def test_r6_allows_db_and_repository_base(rules: ModuleType, tmp_path: Path, relative: str) -> None:
    root = _tree(tmp_path, {relative: "from app.db.guard import SCOPE_OK\n"})
    assert _found(rules, root) == []


def test_rules_flag_limits_rules(rules: ModuleType, tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "apps/api/app/services/x.py": "from sqlalchemy import text\n",
            "apps/api/app/workers/y.py": "from app.db.session import platform_scope\n",
        },
    )
    assert {v.rule for v in rules.find_violations(root, {"R5", "R6"})} == {"R5"}
    assert {v.rule for v in rules.find_violations(root)} == {"R4", "R5"}
    assert rules.main(["--root", str(root), "--rules", "R6"]) == 0
    assert rules.main(["--root", str(root), "--rules", "R5,R6"]) == 1
    with pytest.raises(SystemExit):
        rules.main(["--root", str(root), "--rules", "R9"])


def test_lint_imports_runs_r5_r6(repo_root: Path) -> None:
    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    block = makefile.split("\nlint-imports:\n", 1)[1].split("\n\n", 1)[0]
    assert "uv run --frozen python scripts/ci/check_repo_rules.py --rules R5,R6,R7" in block
    assert block.index("--rules R5,R6,R7") < block.index("grep -rl")


# ---- S10: R7 identity_scope only in app/repositories/identity.py (CODE-PLAN S10 Q4) ----


@pytest.mark.parametrize(
    "body",
    [
        "from app.db.session import identity_scope\n",
        "from app.db.session import identity_scope as s\n",
        "import app.db.session as m\nX = m.identity_scope\n",
        "import app.db.session as m\nX = getattr(m, 'identity_scope')\n",
    ],
)
@pytest.mark.parametrize(
    "relative",
    [
        "apps/api/app/services/auth.py",
        "apps/api/app/api/v1/deps.py",
        "apps/api/app/repositories/products.py",
        "apps/api/app/db/other.py",
    ],
)
def test_identity_scope_importers(
    rules: ModuleType, tmp_path: Path, relative: str, body: str
) -> None:
    root = _tree(tmp_path, {relative: body})
    assert "R7" in {rule for _, _, rule in _found(rules, root)}


@pytest.mark.parametrize(
    "relative", ["apps/api/app/repositories/identity.py", "apps/api/app/db/session.py"]
)
def test_identity_scope_allowed_files(rules: ModuleType, tmp_path: Path, relative: str) -> None:
    root = _tree(tmp_path, {relative: "from app.db.session import identity_scope\n"})
    assert _found(rules, root) == []
