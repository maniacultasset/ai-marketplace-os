"""S9 T9 (AC 7, 9; D1): `BrandChunkRepository.search` returns the tenant's own active chunks
of the current embedding model, nearest first, from a filter-first MATERIALIZED CTE."""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from . import factories as f
from .conftest import LOCAL_HOSTS

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("db_settings", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]
TYPES = ("tone_rule", "glossary", "compliance", "example")


def _vec(key: str) -> list[float]:
    from app.core.vectors import hash_vector

    return hash_vector(key)


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(v) for v in vector) + "]"


def _chunk(conn: Conn, tenant_id: UUID, key: str, **columns: Any) -> UUID:
    return f.brand_chunk(conn, tenant_id, embedding=_literal(_vec(key)), **columns)


@pytest.fixture
def tenants(superuser_conn: Conn) -> tuple[UUID, UUID]:
    return f.tenant(superuser_conn), f.tenant(superuser_conn)


async def _search(tenant_id: UUID, vector: list[float], **kwargs: Any) -> list[Any]:
    from app.db.session import unit_of_work
    from app.repositories.brand_chunks import BrandChunkRepository

    async with unit_of_work(tenant_id) as session:
        return await BrandChunkRepository(session, tenant_id).search(vector, **kwargs)


async def test_search_returns_k_ordered(superuser_conn: Conn, tenants: tuple[UUID, UUID]) -> None:
    a, _ = tenants
    ids = [_chunk(superuser_conn, a, f"a{i}", type=TYPES[i % 4]) for i in range(12)]
    found = await _search(a, _vec("a3"))
    assert len(found) == 8
    assert found[0].id == ids[3]
    sims = [c.similarity for c in found]
    assert sims == sorted(sims, reverse=True)


async def test_search_own_tenant_only(superuser_conn: Conn, tenants: tuple[UUID, UUID]) -> None:
    a, b = tenants
    for i in range(5):
        _chunk(superuser_conn, a, f"a{i}")
        _chunk(superuser_conn, b, f"b{i}")
    found = await _search(a, _vec("b0"))
    assert {c.tenant_id for c in found} == {a}


async def test_search_small_tenant_gets_all_3(
    superuser_conn: Conn, tenants: tuple[UUID, UUID]
) -> None:
    a, b = tenants
    own = {_chunk(superuser_conn, a, f"a{i}") for i in range(3)}
    for i in range(20):
        _chunk(superuser_conn, b, f"b{i}")
    found = await _search(a, _vec("a0"))
    assert {c.id for c in found} == own


async def test_search_types_filter(superuser_conn: Conn, tenants: tuple[UUID, UUID]) -> None:
    a, _ = tenants
    for i, t in enumerate(TYPES * 2):
        _chunk(superuser_conn, a, f"a{i}", type=t)
    found = await _search(a, _vec("a0"), types=["glossary", "example"])
    assert len(found) == 4
    assert {c.type for c in found} == {"glossary", "example"}


async def test_search_excludes_other_embedding_model(
    superuser_conn: Conn, tenants: tuple[UUID, UUID]
) -> None:
    a, _ = tenants
    kept = _chunk(superuser_conn, a, "a0")
    _chunk(superuser_conn, a, "a1", embedding_model="text-embedding-3-large")
    assert [c.id for c in await _search(a, _vec("a1"))] == [kept]


async def test_search_excludes_inactive(superuser_conn: Conn, tenants: tuple[UUID, UUID]) -> None:
    a, _ = tenants
    kept = _chunk(superuser_conn, a, "a0")
    _chunk(superuser_conn, a, "a1", is_active=False)
    assert [c.id for c in await _search(a, _vec("a1"))] == [kept]


async def test_search_identical_vector_similarity_le_1(
    superuser_conn: Conn, tenants: tuple[UUID, UUID]
) -> None:
    a, _ = tenants
    _chunk(superuser_conn, a, "same")
    found = await _search(a, _vec("same"))
    assert len(found) == 1
    assert -1.0 <= found[0].similarity <= 1.0
    assert math.isclose(found[0].similarity, 1.0, abs_tol=1e-6)


async def test_search_empty_tenant_empty_list(
    superuser_conn: Conn, tenants: tuple[UUID, UUID]
) -> None:
    a, b = tenants
    _chunk(superuser_conn, b, "b0")
    assert await _search(a, _vec("x")) == []


def _bad_inputs() -> list[tuple[str, dict[str, Any]]]:
    good = [0.1] * 1536
    return [
        ("k0", {"query_embedding": good, "k": 0}),
        ("k51", {"query_embedding": good, "k": 51}),
        ("kbool", {"query_embedding": good, "k": True}),
        ("len1535", {"query_embedding": good[:-1]}),
        ("nan", {"query_embedding": [math.nan, *good[1:]]}),
        ("inf", {"query_embedding": [math.inf, *good[1:]]}),
        ("zero", {"query_embedding": [0.0] * 1536}),
        ("types_empty", {"query_embedding": good, "types": []}),
        ("types_bogus", {"query_embedding": good, "types": ["bogus"]}),
        ("types_str", {"query_embedding": good, "types": "glossary"}),
    ]


@pytest.mark.parametrize(("case", "kwargs"), _bad_inputs(), ids=[c for c, _ in _bad_inputs()])
async def test_search_validation(case: str, kwargs: dict[str, Any]) -> None:
    from app.repositories.brand_chunks import BrandChunkRepository

    class NoSql:
        async def execute(self, *args: Any, **kw: Any) -> Any:
            raise AssertionError("SQL issued")

    repo = BrandChunkRepository(NoSql(), UUID(int=1))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await repo.search(**kwargs)


async def test_search_sql_is_materialized_cte(tenants: tuple[UUID, UUID]) -> None:
    from app.repositories.brand_chunks import BrandChunkRepository

    captured: list[Any] = []

    class Capture:
        async def execute(self, stmt: Any, *args: Any, **kw: Any) -> Any:
            captured.append(stmt)

            class Empty:
                def all(self) -> list[Any]:
                    return []

            return Empty()

    await BrandChunkRepository(Capture(), tenants[0]).search(_vec("q"))  # type: ignore[arg-type]
    sql_text = str(captured[0].compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert "AS MATERIALIZED" in sql_text
    assert sql_text.index("brand_chunks.tenant_id") < sql_text.index("<=>")


async def test_search_outer_statement_stamped_via_base_helper(
    superuser_conn: Conn, tenants: tuple[UUID, UUID]
) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.models.audit_brand import BrandChunk
    from app.db.session import unit_of_work
    from app.repositories.brand_chunks import BrandChunkRepository
    from sqlalchemy.orm import aliased

    a, _ = tenants
    _chunk(superuser_conn, a, "a0")
    async with unit_of_work(a) as session:
        repo = BrandChunkRepository(session, a)
        assert len(await repo.search(_vec("a0"))) == 1
        inner = repo._select().cte("candidates")
        chunk = aliased(BrandChunk, inner)
        unstamped = select(chunk.id)
        with pytest.raises(CrossTenantQueryError):
            await session.execute(unstamped)
        stamped = repo._scoped_outer(select(chunk.id), inner)
        assert len((await session.execute(stamped)).all()) == 1
