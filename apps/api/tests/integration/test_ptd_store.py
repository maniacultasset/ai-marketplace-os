"""DW2 T8 (AC5, AC6, AC13): the migrator's PTD writes (`app.db.ptd_loads`) and the platform reader
(`ProductTypeDefinitionRepository`). Run in the Docker batch."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.adapters.amazon.definitions import canonical_digest
from app.db import ptd_loads
from app.db.ptd_loads import Actor, DefinitionRow, promote_definition, store_definition
from app.repositories.product_type_definitions import ProductTypeDefinitionRepository
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from tests.support.fake_marketplace import ptd_fixture

from .conftest import LOCAL_HOSTS, Conn

pytestmark = [pytest.mark.allow_hosts(LOCAL_HOSTS), pytest.mark.usefixtures("clean_tenant_db")]

ACTOR = Actor("staff@example.com", "DW2 test refresh", "req-dw2")
KEY = {
    "marketplace_id": "A21TJRUUN4KGV",
    "product_type": "KURTA",
    "requirements": "LISTING_PRODUCT_ONLY",
    "locale": "en_IN",
}


def row(
    name: str = "KURTA.schema.json", *, version: str = "UFAKE0001", **over: Any
) -> DefinitionRow:
    schema = json.loads(ptd_fixture(name))
    fields: dict[str, Any] = {
        **KEY,
        "product_type_version": version,
        "requirements_enforced": "ENFORCED",
        "display_name": "Kurta",
        "property_groups": {"product_identity": {"propertyNames": ["item_name"]}},
        "meta_schema_id": "https://schemas.amazon.com/selling-partners/definitions/"
        "product-types/meta-schema/v1",
        "schema": schema,
        "digest": canonical_digest(schema),
    }
    fields.update(over)
    return DefinitionRow(**fields)


@pytest.fixture
async def engine(db_settings: Any) -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(os.environ["DATABASE_URL"])
    yield created
    await created.dispose()


def _rows(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT id, product_type_version, is_current FROM product_type_definitions "
        "ORDER BY created_at, product_type_version"
    ).fetchall()


def _audits(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT action, tenant_id, actor_type, actor_email, reason, entity_type, entity_id, after "
        "FROM audit_log WHERE entity_type = 'product_type_definition' ORDER BY id"
    ).fetchall()


async def test_first_store_is_current_and_audited(
    engine: AsyncEngine, superuser_conn: Conn
) -> None:
    result = await store_definition(engine, row(), ACTOR)
    assert result.outcome == "stored_current" and result.version == "UFAKE0001"
    ((row_id, version, current),) = _rows(superuser_conn)
    assert (row_id, version, current) == (result.id, "UFAKE0001", True)
    audits = _audits(superuser_conn)
    assert sorted(a[0] for a in audits) == [
        "product_type_definition.promoted",
        "product_type_definition.stored",
    ]
    for _action, tenant, actor, email, reason, entity, entity_id, after in audits:
        assert (tenant, actor, email, reason, entity) == (
            None,
            "staff",
            "staff@example.com",
            "DW2 test refresh",
            "product_type_definition",
        )
        assert entity_id == result.id
        assert set(after) == {*KEY, "product_type_version"}
        assert "link" not in after and "schema" not in after


async def test_same_digest_is_unchanged(engine: AsyncEngine, superuser_conn: Conn) -> None:
    await store_definition(engine, row(), ACTOR)
    before = (_rows(superuser_conn), _audits(superuser_conn))
    again = await store_definition(engine, row(), ACTOR)
    assert again.outcome == "unchanged"
    assert (_rows(superuser_conn), _audits(superuser_conn)) == before


async def test_new_digest_then_promote(engine: AsyncEngine, superuser_conn: Conn) -> None:
    first = await store_definition(engine, row(), ACTOR)
    second = await store_definition(
        engine, row("KURTA.drift.schema.json", version="UFAKE0002"), ACTOR
    )
    assert second.outcome == "stored"
    current = {r[1]: r[2] for r in _rows(superuser_conn)}
    assert current == {"UFAKE0001": True, "UFAKE0002": False}
    audits = len(_audits(superuser_conn))
    assert await promote_definition(
        engine, key=KEY, version="UFAKE0002", digest_prefix=None, actor=ACTOR
    )
    current = {r[1]: r[2] for r in _rows(superuser_conn)}
    assert current == {"UFAKE0001": False, "UFAKE0002": True}
    after_promote = _audits(superuser_conn)
    assert len(after_promote) == audits + 1
    assert sum(1 for a in after_promote if a[6] == second.id) == 2  # stored, then promoted
    assert first.id != second.id


async def test_promote_current_and_unknown(engine: AsyncEngine, superuser_conn: Conn) -> None:
    stored = await store_definition(engine, row(), ACTOR)
    audits = len(_audits(superuser_conn))
    assert not await promote_definition(
        engine, key=KEY, version="UFAKE0001", digest_prefix=None, actor=ACTOR
    )
    assert len(_audits(superuser_conn)) == audits
    with pytest.raises(LookupError):
        await promote_definition(engine, key=KEY, version="NOPE", digest_prefix=None, actor=ACTOR)
    with pytest.raises(LookupError):
        await promote_definition(
            engine, key=KEY, version="UFAKE0001", digest_prefix="ffff" * 4, actor=ACTOR
        )
    assert stored.outcome == "stored_current"
    prefix = canonical_digest(row().schema).hex()[:12]
    assert not await promote_definition(
        engine, key=KEY, version="UFAKE0001", digest_prefix=prefix, actor=ACTOR
    )


async def test_ambiguous_version_needs_a_digest(engine: AsyncEngine, superuser_conn: Conn) -> None:
    await store_definition(engine, row(), ACTOR)
    drift = row("KURTA.drift.schema.json")  # same version, other digest
    await store_definition(engine, drift, ACTOR)
    with pytest.raises(LookupError):
        await promote_definition(
            engine, key=KEY, version="UFAKE0001", digest_prefix=None, actor=ACTOR
        )
    assert await promote_definition(
        engine, key=KEY, version="UFAKE0001", digest_prefix=drift.digest.hex()[:12], actor=ACTOR
    )


async def test_concurrent_stores_write_one_row(engine: AsyncEngine, superuser_conn: Conn) -> None:
    results = await asyncio.gather(
        store_definition(engine, row(), ACTOR), store_definition(engine, row(), ACTOR)
    )
    assert sorted(r.outcome for r in results) == ["stored_current", "unchanged"]
    assert len(_rows(superuser_conn)) == 1


async def test_audit_failure_rolls_the_insert_back(
    engine: AsyncEngine, superuser_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ptd_loads, "AUDIT_SQL", "SELECT audit_missing_function()")
    with pytest.raises(Exception) as caught:
        await store_definition(engine, row(), ACTOR)
    assert "audit_missing_function" in str(caught.value)
    assert _rows(superuser_conn) == []


async def test_reader(engine: AsyncEngine, superuser_conn: Conn) -> None:
    statements: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    async with AsyncSession(engine) as session:
        repo = ProductTypeDefinitionRepository(session)
        assert await repo.current(**KEY) is None
        stored = await store_definition(engine, row(), ACTOR)
        await store_definition(engine, row("KURTA.drift.schema.json", version="U2"), ACTOR)
        await store_definition(
            engine,
            row(version="U3", requirements_enforced="NOT_ENFORCED", digest=b"\x07" * 32),
            ACTOR,
        )
        statements.clear()
        found = await repo.current(**KEY)
        assert found is not None
        assert (found.definition_id, found.product_type_version, found.enforced) == (
            stored.id,
            "UFAKE0001",
            True,
        )
        assert found.digest == canonical_digest(row().schema) and found.validator is None
        (select_sql,) = statements
        assert "schema_json" not in select_sql
        assert await repo.schema_json(stored.id) == row().schema
        with pytest.raises(LookupError):
            await repo.schema_json(uuid.uuid4())
        await promote_definition(engine, key=KEY, version="U3", digest_prefix=None, actor=ACTOR)
        promoted = await repo.current(**KEY)
        assert promoted is not None and promoted.enforced is False
    event.remove(engine.sync_engine, "before_cursor_execute", capture)
