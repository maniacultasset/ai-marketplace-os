"""S9 T5 (AC 8, 10): `unit_of_work` sets the tenant and user GUCs transaction-locally on every
transaction (DATABASE.md:131-132), and a constraint error surfaces as a mapped `ConflictError`
in the service, never at teardown (KNOWN-ISSUES.md:50)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import func, select, text

from . import factories as f
from .conftest import LOCAL_HOSTS, REPO_ROOT

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("db_settings", "clean_tenant_db"),
]

Conn = psycopg.Connection[tuple[Any, ...]]


@pytest.fixture
def tenant_a(superuser_conn: Conn) -> UUID:
    return f.tenant(superuser_conn)


async def _guc(session: Any, name: str) -> str | None:
    value = await session.scalar(select(func.current_setting(name, True)))
    return None if value is None else str(value)


def _products(conn: Conn, tenant_id: UUID) -> int:
    query = sql.SQL("SELECT count(*) FROM products WHERE tenant_id = %s")
    row = conn.execute(query, [tenant_id]).fetchone()
    assert row is not None
    return int(row[0])


def _product(tenant_id: UUID, sku: str) -> Any:
    from app.db.models.catalog import Product

    return Product(
        tenant_id=tenant_id,
        sku=sku,
        title="t",
        category="t_shirts",
        provenance={"title": f.fact(), "category": f.fact()},
    )


async def test_guc_set_inside_uow(tenant_a: UUID) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(tenant_a) as session:
        assert await _guc(session, "app.tenant_id") == str(tenant_a)
        assert await _guc(session, "app.user_id") in ("", None)


async def test_guc_reapplied_second_transaction(tenant_a: UUID) -> None:
    from app.db.session import unit_of_work

    async with unit_of_work(tenant_a) as session:
        assert await _guc(session, "app.tenant_id") == str(tenant_a)
        await session.commit()
        assert await _guc(session, "app.tenant_id") == str(tenant_a)


async def test_guc_empty_after_commit_on_pooled_connection(
    tenant_a: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings
    from app.db.session import dispose_engine, get_engine, unit_of_work

    monkeypatch.setenv("DATABASE_POOL_SIZE", "1")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "0")
    get_settings.cache_clear()
    await dispose_engine()
    async with unit_of_work(tenant_a) as session:
        pid = await session.scalar(select(func.pg_backend_pid()))
        assert await _guc(session, "app.tenant_id") == str(tenant_a)
    async with get_engine().connect() as conn:
        assert await conn.scalar(select(func.pg_backend_pid())) == pid
        value = await conn.scalar(text("SELECT current_setting('app.tenant_id', true)"))
        assert value == ""


async def test_user_guc_set_when_given(tenant_a: UUID) -> None:
    from app.db.session import unit_of_work

    user = uuid4()
    async with unit_of_work(tenant_a, user_id=user) as session:
        assert await _guc(session, "app.user_id") == str(user)


@pytest.mark.parametrize("bad", [None, "8f7a4bd2-0000-4000-8000-000000000000", 1])
async def test_uow_rejects_non_uuid_tenant(bad: Any) -> None:
    from app.db.session import unit_of_work

    with pytest.raises(ValueError, match="tenant_id"):
        async with unit_of_work(bad):
            pass


async def test_uow_rejects_str_user(tenant_a: UUID) -> None:
    from app.db.session import unit_of_work

    with pytest.raises(ValueError, match="user_id"):
        async with unit_of_work(tenant_a, user_id=str(uuid4())):  # type: ignore[arg-type]
            pass


async def test_session_integrity_error_mapped(superuser_conn: Conn, tenant_a: UUID) -> None:
    from app.core.errors import ConflictError
    from app.db.session import unit_of_work
    from mkos_schemas.codes import ErrorCode

    f.product(superuser_conn, tenant_a, sku="DUP-1")
    with pytest.raises(ConflictError) as info:
        async with unit_of_work(tenant_a) as session:
            session.add(_product(tenant_a, "DUP-1"))
    assert info.value.code is ErrorCode.SKU_CONFLICT
    assert info.value.constraint == "products_tenant_sku_idx"
    assert _products(superuser_conn, tenant_a) == 1


async def test_uow_rolls_back_on_exception(superuser_conn: Conn, tenant_a: UUID) -> None:
    from app.db.session import unit_of_work

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):
        async with unit_of_work(tenant_a) as session:
            session.add(_product(tenant_a, "RB-1"))
            await session.flush()
            raise BoomError
    assert _products(superuser_conn, tenant_a) == 0


async def test_concurrent_duplicate_sku_one_conflict(superuser_conn: Conn, tenant_a: UUID) -> None:
    from app.core.errors import ConflictError
    from app.db.session import unit_of_work

    async def write() -> None:
        async with unit_of_work(tenant_a) as session:
            session.add(_product(tenant_a, "RACE-1"))

    results = await asyncio.gather(write(), write(), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["ConflictError", "NoneType"]
    assert any(isinstance(r, ConflictError) for r in results)
    assert _products(superuser_conn, tenant_a) == 1


def test_no_set_local_guc_in_session_py() -> None:
    """GUCs go only through `set_config(name, value, true)` (DATABASE.md:131); `SET LOCAL ROLE`
    in `platform_scope` is a role switch, not a GUC."""
    source = Path(REPO_ROOT / "apps/api/app/db/session.py").read_text(encoding="utf-8")
    assert not re.search(r"SET\s+LOCAL\s+app\.", source, re.IGNORECASE)
    assert "set_config" in source
