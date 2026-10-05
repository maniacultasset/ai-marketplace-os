"""S9 fix round 2 (F1, F3, F4): R4 bypasses. Raw SQL or an engine reached through a re-export
from `app.db`, Core lightweight `table()` / `DDL`, and the engine route (`get_engine().begin()`,
`.sync_engine.raw_connection()`) are flagged outside app/db, app/repositories and app/cli.py;
the allowlisted `app.db` names stay legal."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

RULES = "scripts/ci/check_repo_rules.py"


@pytest.mark.parametrize(
    ("relative", "body"),
    [
        ("services/x.py", "from app.db.session import text\nQ = text('SELECT * FROM products')\n"),
        ("services/x.py", "import app.db.session as s\nQ = s.text('x')\n"),
        (
            "services/x.py",
            "from sqlalchemy import column, select, table\n"
            "Q = select(table('products', column('sku')))\n",
        ),
        (
            "services/x.py",
            "import sqlalchemy as sa\nQ = sa.select(sa.table('products', sa.column('sku')))\n",
        ),
        ("services/x.py", "from sqlalchemy.sql.expression import TableClause\n"),
        ("workers/x.py", "from sqlalchemy import DDL\nQ = DDL('x')\n"),
        (
            "workers/x.py",
            "from app.db.session import get_engine\n"
            "async def f():\n    async with get_engine().begin() as c:\n        pass\n",
        ),
        ("workers/x.py", "from app.db.session import get_sessionmaker\n"),
        ("workers/x.py", "import app.db.session\nQ = app.db.session.get_sessionmaker()\n"),
        ("workers/x.py", "from app.db import session\nQ = session.get_engine()\n"),
        ("workers/x.py", "from ..db.session import get_engine\n"),
        ("workers/x.py", "from app.db import seed_ops\n"),
        ("workers/x.py", "from app.db.base import Base\n"),
        ("workers/x.py", "from app.db.seed_ops import *\n"),
        ("workers/x.py", "def f(engine):\n    return engine.sync_engine.raw_connection()\n"),
        ("workers/x.py", "def f(c):\n    return c.raw_connection()\n"),
        ("workers/x.py", "from sqlalchemy.ext.asyncio import create_async_engine\n"),
        ("workers/x.py", "import sqlalchemy as sa\nE = sa.create_engine('x')\n"),
        ("workers/x.py", "from sqlalchemy import Engine\n"),
        ("api/x.py", "from sqlalchemy.ext.asyncio import AsyncEngine\n"),
    ],
)
def test_r4_flags_bypass(
    script: Callable[[str], ModuleType], tmp_path: Path, relative: str, body: str
) -> None:
    rules = script(RULES)
    path = tmp_path / "apps/api/app" / relative
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    assert {v.rule for v in rules.find_violations(tmp_path)} == {"R4"}


ALLOWED = (
    "from app.db.session import dispose_engine, ping, unit_of_work\n"
    "from app.db.guard import CrossTenantQueryError\n"
    "from app.db.models.catalog import Product\n"
    "from app.db import models\n"
    "import app.db.models.catalog as catalog\n"
    "import app.db.session\n"
    "import app.core.config\n"
    "from sqlalchemy import column, select\n"
    "from sqlalchemy.ext.asyncio import AsyncSession\n"
    "X = (catalog.Product, models.catalog.Product, app.core.config.get_settings)\n"
    "async def f(s: AsyncSession):\n"
    "    async with app.db.session.unit_of_work(1), s.begin():\n"
    "        return select(column('x'))\n"
)


def test_r4_allows_app_db_allowlist(script: Callable[[str], ModuleType], tmp_path: Path) -> None:
    rules = script(RULES)
    path = tmp_path / "apps/api/app/workers/x.py"
    path.parent.mkdir(parents=True)
    path.write_text(ALLOWED, encoding="utf-8")
    assert rules.find_violations(tmp_path, {"R4"}) == []


def test_r4_exempts_cli_from_engine_rules(
    script: Callable[[str], ModuleType], tmp_path: Path
) -> None:
    """The admin entrypoint opens its own admin engine and drives `seed_ops`; raw SQL in it is
    still R4."""
    rules = script(RULES)
    path = tmp_path / "apps/api/app/cli.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "from app.db import seed_ops\n"
        "from app.db.session import dispose_engine, unit_of_work\n",
        encoding="utf-8",
    )
    assert rules.find_violations(tmp_path, {"R4"}) == []
    path.write_text("from sqlalchemy import text\n", encoding="utf-8")
    assert {v.rule for v in rules.find_violations(tmp_path, {"R4"})} == {"R4"}
