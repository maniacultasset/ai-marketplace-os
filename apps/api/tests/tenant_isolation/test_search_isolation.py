"""S9 T19 (AC 1, 7; CANON.md:137; DATABASE.md:1076), as `app_rw`: 50 tenants x 100 chunks plus
one tenant with 3. A tenant's own chunk embedding returns exactly 8 of its own chunks, nearest
first; the small tenant gets all 3 (`min(8, its chunk count)`); nobody sees another tenant's."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.usefixtures("iso_settings")

Conn = psycopg.Connection[tuple[object, ...]]
BIG_TENANTS = 50
CHUNKS = 100
TYPES = ("tone_rule", "glossary", "compliance", "example")


@dataclass(frozen=True)
class Corpus:
    big: list[UUID]
    small: UUID


HNSW_INDEX = "brand_chunks_embedding_idx"


def _ensure_hnsw_index(conn: Conn) -> None:
    """Recreate the HNSW index from the model if an interrupted run left it dropped."""
    import app.db.models  # noqa: F401
    from app.db.base import metadata
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex

    if conn.execute("SELECT to_regclass(%s)", [HNSW_INDEX]).fetchone() != (None,):
        return
    (index,) = (i for i in metadata.tables["brand_chunks"].indexes if i.name == HNSW_INDEX)
    conn.execute(str(CreateIndex(index).compile(dialect=postgresql.dialect())))  # type: ignore[no-untyped-call]


@pytest.fixture(scope="module")
def corpus(clean_module_admin_conn: Conn) -> Iterator[Corpus]:
    """Random vectors generated in SQL from a fixed seed. Search here is exact (filter-first
    CTE), so the HNSW index is dropped while the corpus exists: maintaining it for 5,003
    vectors costs ~30 s. Teardown empties the corpus and rebuilds the index on the empty
    table; setup first repairs an index a killed run left dropped."""
    conn = clean_module_admin_conn
    _ensure_hnsw_index(conn)
    indexdef = conn.execute("SELECT pg_get_indexdef(%s::regclass)", [HNSW_INDEX]).fetchone()
    assert indexdef is not None
    rows = conn.execute(
        "INSERT INTO tenants (name, slug, status) "
        "SELECT 'Search ' || g, 'search-' || g, 'active' FROM generate_series(1, %s) g "
        "RETURNING id, slug",
        [BIG_TENANTS + 1],
    ).fetchall()
    by_slug = {str(slug): UUID(str(tid)) for tid, slug in rows}
    small = by_slug.pop(f"search-{BIG_TENANTS + 1}")
    conn.execute(f"DROP INDEX {HNSW_INDEX}")
    try:
        conn.execute("SELECT setseed(0.42)")
        for tenant, count in [*((t, CHUNKS) for t in by_slug.values()), (small, 3)]:
            conn.execute(
                "INSERT INTO brand_chunks (tenant_id, type, text, embedding, embedding_model, "
                "content_hash, source_type, source_id, chunk_index) "
                "SELECT %(t)s, (ARRAY['tone_rule','glossary','compliance','example'])[1 + g %% 4], "
                "'chunk ' || g, (SELECT array_agg(random() - 0.5 + 0 * d + 0 * g) "
                "FROM generate_series(1, 1536) d)::vector, 'text-embedding-3-small', 'h' || g, "
                "'brand_document', gen_random_uuid(), 0 FROM generate_series(1, %(n)s) g",
                {"t": tenant, "n": count},
            )
        yield Corpus(big=sorted(by_slug.values()), small=small)
    finally:
        conn.execute("DELETE FROM brand_chunks")
        conn.execute(str(indexdef[0]))


def _embedding(conn: Conn, tenant: UUID, chunk_type: str | None = None) -> tuple[UUID, list[float]]:
    row = conn.execute(
        "SELECT id, embedding::text FROM brand_chunks WHERE tenant_id = %s "
        "AND (%s::text IS NULL OR type = %s) ORDER BY id LIMIT 1",
        [tenant, chunk_type, chunk_type],
    ).fetchone()
    assert row is not None
    return UUID(str(row[0])), [float(v) for v in str(row[1]).strip("[]").split(",")]


async def _search(
    tenant: UUID, vector: list[float], types: list[str] | None = None
) -> list[object]:
    from app.db.session import unit_of_work
    from app.repositories.brand_chunks import BrandChunkRepository

    async with unit_of_work(tenant) as session:
        return list(await BrandChunkRepository(session, tenant).search(vector, types=types))  # type: ignore[arg-type]


async def test_brand_chunk_search_small_tenant(
    clean_module_admin_conn: Conn, corpus: Corpus
) -> None:
    target = corpus.big[17]
    chunk_id, vector = _embedding(clean_module_admin_conn, target)
    found = await _search(target, vector)
    assert len(found) == 8
    assert found[0].id == chunk_id  # type: ignore[attr-defined]
    assert {c.tenant_id for c in found} == {target}  # type: ignore[attr-defined]
    sims = [c.similarity for c in found]  # type: ignore[attr-defined]
    assert sims == sorted(sims, reverse=True)
    assert all(-1.0 <= s <= 1.0 for s in sims)
    _, small_vector = _embedding(clean_module_admin_conn, corpus.small)
    small = await _search(corpus.small, small_vector)
    assert len(small) == 3
    assert {c.tenant_id for c in small} == {corpus.small}  # type: ignore[attr-defined]


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    index=st.integers(min_value=0, max_value=BIG_TENANTS - 1),
    other=st.integers(min_value=0, max_value=BIG_TENANTS - 1),
    types=st.none() | st.lists(st.sampled_from(TYPES), min_size=1, max_size=4, unique=True),
)
async def test_search_never_returns_other_tenant_property(
    clean_module_admin_conn: Conn, corpus: Corpus, index: int, other: int, types: list[str] | None
) -> None:
    """Even with another tenant's exact embedding as the query, only own chunks come back."""
    tenant = corpus.big[index]
    _, vector = _embedding(clean_module_admin_conn, corpus.big[other])
    found = await _search(tenant, vector, types)
    assert len(found) == 8
    assert {c.tenant_id for c in found} == {tenant}  # type: ignore[attr-defined]
    if types is not None:
        assert {c.type for c in found} <= set(types)  # type: ignore[attr-defined]
