"""Revision 0004_integrity's loader (DATABASE.md §8.2 ledger row 0004): ordered SQL file pairs
under `alembic/sql/0004/`, run in name order on upgrade and in reverse on downgrade, each file
opening with its own `-- 0004/<file>` marker line."""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from .conftest import ScratchDb
from .test_0002_catalog import _objects

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

MakeConfig = Callable[[Path | None], Config]

SQL_DIR = Path(__file__).resolve().parents[2] / "alembic" / "sql" / "0004"
PREVIOUS = "0003b_procrastinate"
REVISION = "0004_integrity"
FILE_NAME = re.compile(r"^(?P<stem>[0-9]{2}b?_[a-z0-9_]+)\.(?P<direction>up|down)\.sql$")
# The planned files, in run order (CODE-PLAN §3 S10a). Each task adds its pair here.
EXPECTED_STEMS: tuple[str, ...] = (
    "01_transitions",
    "02_audit",
    "03_append_only",
    "04_lv_guard",
    "05_last_owner",
    "06_listings_status_guard",
)


def _stems(direction: str) -> list[str]:
    names = sorted(p.name for p in SQL_DIR.glob(f"*.{direction}.sql"))
    return [name.removesuffix(f".{direction}.sql") for name in names]


def test_0004_sql_files_paired() -> None:
    names = sorted(p.name for p in SQL_DIR.iterdir()) if SQL_DIR.is_dir() else []
    for name in names:
        assert FILE_NAME.match(name), f"unexpected file in alembic/sql/0004: {name}"
    assert _stems("up") == _stems("down")


def test_0004_file_names_ordered() -> None:
    assert tuple(_stems("up")) == EXPECTED_STEMS
    for direction in ("up", "down"):
        for stem in _stems(direction):
            first = (SQL_DIR / f"{stem}.{direction}.sql").read_text(encoding="utf-8")
            assert first.splitlines()[0] == f"-- 0004/{stem}.{direction}.sql", stem


def _offline(cfg: Config, direction: str) -> str:
    buffer = io.StringIO()
    cfg.output_buffer = buffer
    if direction == "up":
        command.upgrade(cfg, f"{PREVIOUS}:{REVISION}", sql=True)
        marker = f"-- Running upgrade {PREVIOUS} -> {REVISION}"
    else:
        command.downgrade(cfg, f"{REVISION}:{PREVIOUS}", sql=True)
        marker = f"-- Running downgrade {REVISION} -> {PREVIOUS}"
    sql = buffer.getvalue()
    return sql[sql.index(marker) + len(marker) :]


@pytest.mark.parametrize("direction", ["up", "down"])
def test_0004_offline_sql_in_order(alembic_env: MakeConfig, direction: str) -> None:
    rendered = _offline(alembic_env(None), direction)
    statements = [s.strip() for s in rendered.split(";") if s.strip()]
    assert statements[0] == "SET lock_timeout = '5s'"
    order = list(EXPECTED_STEMS) if direction == "up" else list(reversed(EXPECTED_STEMS))
    positions = [rendered.index(f"-- 0004/{stem}.{direction}.sql") for stem in order]
    assert positions == sorted(positions)


def test_0004_round_trip_empty(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    """On an empty database: up to 0004, down to 0003b leaves exactly the 0003b catalog, and the
    next upgrade rebuilds the same 0004 catalog."""
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    at_previous = _objects(scratch_db)
    command.upgrade(cfg, REVISION)
    at_revision = _objects(scratch_db)
    command.downgrade(cfg, PREVIOUS)
    assert _objects(scratch_db) == at_previous
    command.upgrade(cfg, REVISION)
    assert _objects(scratch_db) == at_revision
