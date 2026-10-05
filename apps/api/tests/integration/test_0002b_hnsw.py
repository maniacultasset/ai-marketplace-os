"""Revision 0002b_brand_chunks_hnsw (DATABASE.md §5.5 brand_chunks, §8.2 ledger row 0002b;
CANON.md 0002b row; T66): the pgvector gate, the HNSW and partial index definitions, EXPLAIN on
both search paths, and the offline SQL that runs CONCURRENTLY outside a transaction."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from app.db.migrations import ExtensionTooOld, require_extension_version
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from . import factories as f
from .conftest import ScratchDb

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]
MODEL = "text-embedding-3-small"


def test_hnsw_version_gate(head_db: ScratchDb) -> None:
    """Done-when 5: the gate refuses a too-old pgvector with a clear message, on the real
    catalog; compose ships 0.8.5, which passes the 0.8.2 minimum."""
    engine = create_engine(head_db.url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            with pytest.raises(
                ExtensionTooOld, match=r"^pgvector 0\.8\.5 found; 0002b needs >= 99\.0\.0$"
            ):
                require_extension_version(connection, "vector", (99, 0, 0))
            require_extension_version(connection, "vector", (0, 8, 2))
            with pytest.raises(ExtensionTooOld, match=r"^no_such_ext not installed"):
                require_extension_version(connection, "no_such_ext", (1,))
    finally:
        engine.dispose()


def _indexes(db: Conn) -> dict[str, tuple[str, bool]]:
    rows = db.execute(
        "SELECT c.relname, pg_get_indexdef(x.indexrelid), x.indisvalid FROM pg_index x "
        "JOIN pg_class c ON c.oid = x.indexrelid WHERE x.indrelid = 'brand_chunks'::regclass"
    ).fetchall()
    return {str(name): (str(definition), bool(valid)) for name, definition, valid in rows}


def test_hnsw_index_definition(db: Conn) -> None:
    indexes = _indexes(db)
    hnsw, hnsw_valid = indexes["brand_chunks_embedding_idx"]
    assert hnsw.endswith(
        "USING hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='64')"
    )
    partial, partial_valid = indexes["brand_chunks_tenant_type_idx"]
    assert partial.endswith("USING btree (tenant_id, type) WHERE is_active")
    assert hnsw_valid and partial_valid


def _chunks(db: Conn, count: int = 20) -> Any:
    tenant_id = f.tenant(db)
    for n in range(count):
        f.brand_chunk(db, tenant_id, embedding=f.embedding(n / 100), type="tone_rule")
    db.execute("ANALYZE brand_chunks")
    return tenant_id


def _plan(db: Conn, query: str, params: tuple[Any, ...]) -> str:
    rows = db.execute(f"EXPLAIN (COSTS OFF) {query}", params).fetchall()
    return "\n".join(str(r[0]) for r in rows)


TOP_8 = "SELECT id FROM brand_chunks ORDER BY embedding <=> %s::vector LIMIT 8"
EXACT = """
    WITH c AS MATERIALIZED (
      SELECT id, text, embedding FROM brand_chunks
      WHERE tenant_id = %s AND is_active AND embedding_model = %s AND type = %s)
    SELECT id, text, embedding <=> %s::vector AS distance FROM c ORDER BY distance LIMIT 8
"""


def test_hnsw_explain_uses_index(db: Conn) -> None:
    """Done-when 5: a top-8 cosine query can use the HNSW index."""
    _chunks(db)
    db.execute("SET LOCAL enable_seqscan = off")
    plan = _plan(db, TOP_8, (f.embedding(0.05),))
    assert "brand_chunks_embedding_idx" in plan, plan


def test_exact_path_skips_hnsw(db: Conn) -> None:
    """T66: the year-1 query filters first and sorts the tenant's rows exactly; it never reaches
    the global HNSW index (DATABASE.md §5.5), even with sequential scans off."""
    tenant_id = _chunks(db)
    params = (tenant_id, MODEL, "tone_rule", f.embedding(0.05))
    for seqscan in ("on", "off"):
        db.execute(f"SET LOCAL enable_seqscan = {seqscan}")
        plan = _plan(db, EXACT, params)
        assert "brand_chunks_embedding_idx" not in plan, plan
    rows = db.execute(EXACT, params).fetchall()
    assert len(rows) == 8


def test_0002b_offline_sql_runs_outside_a_transaction(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    """`--sql` (the squawk input) skips the gate, which needs a connection, and emits both
    CONCURRENTLY builds between the 0002 COMMIT and the next BEGIN."""
    cfg = alembic_env(None)
    buffer = io.StringIO()
    cfg.output_buffer = buffer
    command.upgrade(cfg, "0002_listing_pipeline:0002b_brand_chunks_hnsw", sql=True)
    sql = buffer.getvalue()
    commit = sql.index("COMMIT;")
    embedding = sql.index("CREATE INDEX CONCURRENTLY brand_chunks_embedding_idx")
    partial = sql.index("CREATE INDEX CONCURRENTLY brand_chunks_tenant_type_idx")
    assert commit < embedding < partial
    assert "BEGIN;" not in sql[commit:partial]
    assert "pg_extension" not in sql


def test_0002b_round_trip(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    """Online, through env.py: 0002 → 0002b → 0002 → 0002b, both indexes valid at the end."""
    cfg = alembic_env(None)
    command.upgrade(cfg, "0002_listing_pipeline")
    command.upgrade(cfg, "0002b_brand_chunks_hnsw")
    command.downgrade(cfg, "0002_listing_pipeline")
    with psycopg.connect(scratch_db.libpq, autocommit=True, connect_timeout=5) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT relname FROM pg_class WHERE relname LIKE 'brand_chunks_%%idx'"
            ).fetchall()
        }
        assert names == set()
    command.upgrade(cfg, "head")
    with psycopg.connect(scratch_db.libpq, autocommit=True, connect_timeout=5) as conn:
        assert _indexes(conn)["brand_chunks_embedding_idx"][1] is True
