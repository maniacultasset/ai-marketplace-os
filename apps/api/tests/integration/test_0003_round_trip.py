"""The 0003/0003b round trip on a scratch database through env.py (DATABASE.md §8.2, §8.3): each
downgrade step leaves exactly the catalog the step below created, the full cycle passes
`alembic check` at both ends, and both revisions open with `SET lock_timeout`."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from .conftest import ScratchDb
from .test_0002_catalog import _objects

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

MakeConfig = Callable[[Path | None], Config]

REVISION_0002B = "0002b_brand_chunks_hnsw"
REVISION_0003 = "0003_ops_tables"
REVISION_0003B = "0003b_procrastinate"
TABLES_0003 = frozenset(
    {
        "job_runs",
        "imports",
        "import_rows",
        "idempotency_keys",
        "refresh_tokens",
        "oauth_states",
        "rate_limit_counters",
        "listing_submissions",
        "publish_field_results",
        "category_templates",
        "marketplace_error_map",
        "attribute_definitions",
        "attribute_columns",
        "category_attributes",
    }
)
DEFERRED_FKS = (
    "li_error_map_fk",
    "fk_validation_runs_category_template_id_category_templates",
    "fk_listing_versions_category_template_id_category_templates",
)


def _procrastinate_objects(db: ScratchDb) -> list[tuple[str, str]]:
    with psycopg.connect(db.libpq, autocommit=True, connect_timeout=5) as conn:
        rows = conn.execute(
            "SELECT 'class', relname FROM pg_class WHERE relname LIKE 'procrastinate%%' "
            "UNION ALL SELECT 'proc', proname FROM pg_proc WHERE proname LIKE 'procrastinate%%' "
            "UNION ALL SELECT 'type', typname FROM pg_type WHERE typname LIKE 'procrastinate%%' "
            "OR typname LIKE '\\_procrastinate%%' ORDER BY 1, 2"
        ).fetchall()
    return [(str(kind), str(name)) for kind, name in rows]


def _grants(db: ScratchDb) -> list[tuple[object, ...]]:
    with psycopg.connect(db.libpq, autocommit=True, connect_timeout=5) as conn:
        return conn.execute(
            "SELECT table_name, grantee, privilege_type FROM information_schema.table_privileges "
            "WHERE table_schema = 'public' UNION ALL "
            "SELECT table_name || '.' || column_name, grantee, privilege_type "
            "FROM information_schema.column_privileges WHERE table_schema = 'public' ORDER BY 1, 2, 3"
        ).fetchall()


def _version(db: ScratchDb) -> list[tuple[object, ...]]:
    with psycopg.connect(db.libpq, autocommit=True, connect_timeout=5) as conn:
        return conn.execute("SELECT version_num FROM alembic_version").fetchall()


def test_downgrade_0003b_clean(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION_0003)
    at_0003 = _objects(scratch_db)
    assert _procrastinate_objects(scratch_db) == []
    command.upgrade(cfg, REVISION_0003B)
    assert len([o for o in _procrastinate_objects(scratch_db) if o[0] == "proc"]) == 18
    command.downgrade(cfg, "-1")
    assert _procrastinate_objects(scratch_db) == []
    assert _objects(scratch_db) == at_0003
    assert _version(scratch_db) == [(REVISION_0003,)]


def test_downgrade_0003_restores_0002_head(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION_0002B)
    at_0002b = _objects(scratch_db)
    grants_0002b = _grants(scratch_db)
    command.upgrade(cfg, REVISION_0003B)
    at_head = _objects(scratch_db)
    relations = {str(r[0]) for r in at_head["relations"]}
    assert relations >= TABLES_0003
    constraints = {str(c[1]) for c in at_head["constraints"]}
    assert set(DEFERRED_FKS) <= constraints
    command.downgrade(cfg, "-1")
    command.downgrade(cfg, "-1")
    after = _objects(scratch_db)
    assert after == at_0002b
    assert not {str(r[0]) for r in after["relations"]} & TABLES_0003
    assert not set(DEFERRED_FKS) & {str(c[1]) for c in after["constraints"]}
    assert ("job_runs_set_queue()",) not in after["functions"]
    assert {"listing_issues", "validation_runs", "listing_versions"} <= {
        str(r[0]) for r in after["relations"]
    }
    assert _grants(scratch_db) == grants_0002b
    assert _version(scratch_db) == [(REVISION_0002B,)]
    command.upgrade(cfg, REVISION_0003B)
    assert _objects(scratch_db) == at_head


def test_round_trip_to_head_and_check(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, "head")
    command.check(cfg)
    at_head = _objects(scratch_db)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    command.check(cfg)
    assert _objects(scratch_db) == at_head
    assert _version(scratch_db) == [(ScriptDirectory.from_config(cfg).get_current_head(),)]


@pytest.mark.parametrize(
    ("previous", "revision"),
    [(REVISION_0002B, REVISION_0003), (REVISION_0003, REVISION_0003B)],
)
def test_revisions_set_lock_timeout(
    alembic_env: MakeConfig, scratch_db: ScratchDb, previous: str, revision: str
) -> None:
    cfg = alembic_env(None)
    for direction in ("upgrade", "downgrade"):
        buffer = io.StringIO()
        cfg.output_buffer = buffer
        if direction == "upgrade":
            command.upgrade(cfg, f"{previous}:{revision}", sql=True)
            marker = f"-- Running upgrade {previous} -> {revision}"
        else:
            command.downgrade(cfg, f"{revision}:{previous}", sql=True)
            marker = f"-- Running downgrade {revision} -> {previous}"
        sql = buffer.getvalue()
        after = sql[sql.index(marker) + len(marker) :]
        statements = [s.strip() for s in after.split(";") if s.strip()]
        assert statements[0] == "SET lock_timeout = '5s'", (direction, statements[:2])
