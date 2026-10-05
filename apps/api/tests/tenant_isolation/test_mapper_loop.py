"""S9 T18 (AC 1, 12; CANON.md:137; DATABASE.md:129): every TenantScoped mapper, as `app_rw`.

- Layer 2 through the repository: a tenant sees only its own rows, another tenant's id reads
  as missing, and NULL-tenant rows stay hidden outside `RuleRepository.list_effective`.
- Layer 2 alone (no repository filter): every raw statement shape on every mapper raises
  `CrossTenantQueryError`, and a flush that writes another tenant's row is refused (D18).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from app.db.base import Base
from app.db.models._tenant import TenantScoped
from app.db.models.platform import CategoryTemplate
from psycopg import sql
from sqlalchemy import exists, func, inspect, select
from sqlalchemy.orm import make_transient

from .factories import World, build_world
from .factories import table_name as _table_name

pytestmark = pytest.mark.usefixtures("iso_settings")

Conn = psycopg.Connection[tuple[object, ...]]

MAPPERS: dict[str, type[Any]] = {
    _table_name(m.class_): m.class_
    for m in sorted(Base.registry.mappers, key=lambda m: _table_name(m.class_))
    if issubclass(m.class_, TenantScoped)
}
ID_MAPPERS = sorted(
    name
    for name, model in MAPPERS.items()
    if [c.name for c in inspect(model).primary_key] == ["id"]
)
SHAPES: dict[str, Callable[[type[Any]], Any]] = {
    "entity": lambda m: select(m),
    "join": lambda m: select(CategoryTemplate).join(m, m.tenant_col() == CategoryTemplate.id),
    "subquery": lambda m: select(CategoryTemplate).where(
        CategoryTemplate.id.in_(select(m.tenant_col()))
    ),
    "exists": lambda m: select(CategoryTemplate).where(exists().where(m.tenant_col().is_not(None))),
    "cte": lambda m: select(select(m).cte().c[m.__tenant_column__]),
    "select_from": lambda m: select(func.count()).select_from(m),
    "core_table": lambda m: select(m.__table__),
}


@pytest.fixture(scope="module")
def world(clean_module_admin_conn: Conn) -> World:
    """Built once: every test here only reads, or fails a flush and rolls back."""
    return build_world(clean_module_admin_conn, 2)


def _repository(model: type[Any]) -> type[Any]:
    from app.repositories.base import TenantScopedRepository

    return type(f"{model.__name__}Repository", (TenantScopedRepository,), {"model": model})


def _own_count(conn: Conn, model: type[Any], tenant: Any) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE {} = %s").format(
        sql.Identifier(model.__table__.name), sql.Identifier(model.__tenant_column__)
    )
    row = conn.execute(query, [tenant]).fetchone()
    assert row is not None
    return int(str(row[0]))


def test_every_tenant_mapper_has_a_factory(world: World) -> None:
    assert set(world.rows) == set(MAPPERS)


@pytest.mark.parametrize("table", sorted(MAPPERS))
async def test_list_returns_only_own_rows(admin_conn: Conn, world: World, table: str) -> None:
    from app.db.session import unit_of_work

    model = MAPPERS[table]
    a, _ = world.tenants
    async with unit_of_work(a) as session:
        rows = await _repository(model)(session, a).list(limit=500)
    assert rows, table
    assert {getattr(r, model.__tenant_column__) for r in rows} == {a}
    assert len(rows) == _own_count(admin_conn, model, a)


@pytest.mark.parametrize("table", ID_MAPPERS)
async def test_get_by_id_other_tenant_is_none(world: World, table: str) -> None:
    from app.db.session import unit_of_work

    a, b = world.tenants
    async with unit_of_work(a) as session:
        assert await _repository(MAPPERS[table])(session, a).get_by_id(world.rows[table][b]) is None


@pytest.mark.parametrize("table", ID_MAPPERS)
async def test_get_by_id_own_row(world: World, table: str) -> None:
    from app.db.session import unit_of_work

    a, _ = world.tenants
    async with unit_of_work(a) as session:
        found = await _repository(MAPPERS[table])(session, a).get_by_id(world.rows[table][a])
    assert found is not None
    assert found.id == world.rows[table][a]


@pytest.mark.parametrize("table", ["rules", "audit_log"])
async def test_null_tenant_rows_hidden(world: World, table: str) -> None:
    from app.db.session import unit_of_work

    a, _ = world.tenants
    platform_id = world.platform_rows[table]
    async with unit_of_work(a) as session:
        repo = _repository(MAPPERS[table])(session, a)
        assert platform_id not in {r.id for r in await repo.list(limit=500)}
        assert await repo.get_by_id(platform_id) is None


async def test_rule_repository_sees_platform_rows(world: World) -> None:
    from app.db.session import unit_of_work
    from app.repositories.rules import RuleRepository

    a, b = world.tenants
    async with unit_of_work(a) as session:
        rules = await RuleRepository(session, a).list_effective("amazon", "t_shirts")
    ids = {r.id for r in rules}
    assert world.platform_rows["rules"] in ids
    assert world.rows["rules"][a] in ids
    assert world.rows["rules"][b] not in ids


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("table", sorted(MAPPERS))
async def test_raw_select_raises_guard(world: World, table: str, shape: str) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.session import unit_of_work

    a, _ = world.tenants
    async with unit_of_work(a) as session:
        with pytest.raises(CrossTenantQueryError):
            await session.execute(SHAPES[shape](MAPPERS[table]))


@pytest.mark.parametrize("variant", ["insert_other", "move_to_other"])
@pytest.mark.parametrize("table", sorted(MAPPERS))
async def test_flush_rejects_other_tenant(
    admin_conn: Conn, world: World, table: str, variant: str
) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.db.session import unit_of_work

    model = MAPPERS[table]
    column = model.__tenant_column__
    a, b = world.tenants
    before = _own_count(admin_conn, model, b)
    with pytest.raises(CrossTenantQueryError):
        async with unit_of_work(a) as session:
            (row,) = await _repository(model)(session, a).list(limit=1)
            if variant == "insert_other":
                session.expunge(row)
                make_transient(row)
                setattr(row, column, b)
                session.add(row)
            else:
                setattr(row, column, b)
            await session.flush()
    assert _own_count(admin_conn, model, b) == before
