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
    assert "uv run --frozen python scripts/ci/check_repo_rules.py --rules R5,R6,R7,R8" in block
    assert block.index("--rules R5,R6,R7,R8") < block.index("grep -rl")


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


# ---- S13 A4: R5 allows app/workers/periodic.py; R8 keeps the job GUC in app/db/job_context.py ----


def test_r5_allows_workers_periodic(rules: ModuleType, tmp_path: Path) -> None:
    root = _tree(tmp_path, {"apps/api/app/workers/periodic.py": PLATFORM_IMPORTS[0]})
    assert _found(rules, root) == []


def test_r5_flags_workers_wrapper(rules: ModuleType, tmp_path: Path) -> None:
    root = _tree(tmp_path, {"apps/api/app/workers/wrapper.py": PLATFORM_IMPORTS[0]})
    assert {rule for _, _, rule in _found(rules, root)} == {"R5"}


JOB_CONTEXT_IMPORTS = [
    "from app.db.job_context import resolve_job\n",
    "from app.db.job_context import resolve_job as r\n",
    "from app.db import job_context\n",
    "import app.db.job_context\n",
    "import app.db.job_context as jc\n",
]


@pytest.mark.parametrize("body", JOB_CONTEXT_IMPORTS)
@pytest.mark.parametrize(
    "relative",
    [
        "apps/api/app/workers/periodic.py",
        "apps/api/app/services/jobs.py",
        "apps/api/app/repositories/jobs.py",
        "apps/api/app/db/queue.py",
    ],
)
def test_r8_flags_job_context_importers(
    rules: ModuleType, tmp_path: Path, relative: str, body: str
) -> None:
    root = _tree(tmp_path, {relative: body})
    assert "R8" in {rule for _, _, rule in _found(rules, root)}


def test_r8_flags_relative_import(rules: ModuleType, tmp_path: Path) -> None:
    root = _tree(tmp_path, {"apps/api/app/db/other.py": "from .job_context import resolve_job\n"})
    assert {rule for _, _, rule in _found(rules, root)} == {"R8"}


@pytest.mark.parametrize("body", JOB_CONTEXT_IMPORTS[:2])
def test_r8_allows_wrapper(rules: ModuleType, tmp_path: Path, body: str) -> None:
    root = _tree(tmp_path, {"apps/api/app/workers/wrapper.py": body})
    assert _found(rules, root) == []


@pytest.mark.parametrize(
    "body",
    [
        "X = 'app.job_id'\n",
        "def f(s):\n    return s.execute(\"SELECT set_config('app.job_id', '1', true)\")\n",
    ],
)
@pytest.mark.parametrize(
    "relative", ["apps/api/app/repositories/jobs.py", "apps/api/app/workers/wrapper.py"]
)
def test_r8_flags_job_guc_literal(
    rules: ModuleType, tmp_path: Path, relative: str, body: str
) -> None:
    root = _tree(tmp_path, {relative: body})
    assert {rule for _, _, rule in _found(rules, root)} == {"R8"}


def test_r8_allows_job_guc_in_job_context(rules: ModuleType, tmp_path: Path) -> None:
    root = _tree(tmp_path, {"apps/api/app/db/job_context.py": "X = 'app.job_id'\n"})
    assert _found(rules, root) == []


def test_r8_ignores_alembic_and_scripts(rules: ModuleType, tmp_path: Path) -> None:
    body = "X = 'app.job_id'\nimport app.db.job_context\n"
    root = _tree(tmp_path, {"apps/api/alembic/versions/x.py": body, "scripts/x.py": body})
    assert "R8" not in {rule for _, _, rule in _found(rules, root)}


# ---- S18 T5: R8 also allows the listing repository (worker-actor moves) ----


@pytest.mark.parametrize(
    "body", [*JOB_CONTEXT_IMPORTS[:2], "from app.db.job_context import bind_job_actor\n"]
)
def test_r8_allows_listings_repository_only(rules: ModuleType, tmp_path: Path, body: str) -> None:
    allowed = _tree(tmp_path / "a", {"apps/api/app/repositories/listings.py": body})
    assert _found(rules, allowed) == []
    other = _tree(tmp_path / "b", {"apps/api/app/repositories/listing_versions.py": body})
    assert {rule for _, _, rule in _found(rules, other)} == {"R8"}


def test_r8_listings_repository_may_not_name_the_guc(rules: ModuleType, tmp_path: Path) -> None:
    root = _tree(tmp_path, {"apps/api/app/repositories/listings.py": "X = 'app.job_id'\n"})
    assert {rule for _, _, rule in _found(rules, root)} == {"R8"}


# ---- S18 T7 (CP9): R5 allows app/workers/budget.py and no other worker ----


@pytest.mark.parametrize(
    "body", ["from app.db.session import platform_scope\n", "import app.db.session\n"]
)
def test_r5_allows_budget(rules: ModuleType, tmp_path: Path, body: str) -> None:
    text = body if "platform_scope" in body else body + "x = app.db.session.platform_scope\n"
    root = _tree(tmp_path, {"apps/api/app/workers/budget.py": text})
    assert "R5" not in {rule for _, _, rule in _found(rules, root)}


@pytest.mark.parametrize(
    "body",
    [
        "from app.db.session import platform_scope\n",
        "import app.db.session\nx = app.db.session.platform_scope\n",
    ],
)
def test_r5_flags_other_worker(rules: ModuleType, tmp_path: Path, body: str) -> None:
    root = _tree(tmp_path, {"apps/api/app/workers/other.py": body})
    assert "R5" in {rule for _, _, rule in _found(rules, root)}


# ---- S23 (KNOWN-ISSUES.md:31): R5 allows app/workers/rollup.py; services stay out ----


@pytest.mark.parametrize(
    "body",
    [
        "from app.db.session import platform_scope\n",
        "import app.db.session\nx = app.db.session.platform_scope\n",
    ],
)
def test_r5_allows_rollup(rules: ModuleType, tmp_path: Path, body: str) -> None:
    root = _tree(tmp_path, {"apps/api/app/workers/rollup.py": body})
    assert "R5" not in {rule for _, _, rule in _found(rules, root)}


@pytest.mark.parametrize(
    "path", ["apps/api/app/services/x.py", "apps/api/app/services/analytics.py"]
)
def test_r5_flags_services(rules: ModuleType, tmp_path: Path, path: str) -> None:
    root = _tree(tmp_path, {path: "from app.db.session import platform_scope\n"})
    assert "R5" in {rule for _, _, rule in _found(rules, root)}
