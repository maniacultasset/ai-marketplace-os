"""S12 T6 (AC5; PLAN D6): `tenant_uow` is one tenant transaction carrying the product, dictionary
and audit repositories on one session. `user_id` is optional, so worker callers with no user
(a job whose `created_by` is NULL) open the same transaction."""

from __future__ import annotations

import json
import uuid

import pytest

from .auth_support import World, audit_rows, build_world
from .conftest import LOCAL_HOSTS
from .products_support import Conn

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

_STAMP = {"source": "seller", "captured_via": "api", "updated_at": "2026-10-06T00:00:00Z"}


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


async def _guc(session: object, name: str) -> str | None:
    from sqlalchemy import func, select

    value = await session.scalar(select(func.current_setting(name, True)))  # type: ignore[attr-defined]
    return None if value in (None, "") else str(value)


def _count(conn: Conn, sku: str) -> int:
    row = conn.execute("SELECT count(*) FROM products WHERE sku = %s", [sku]).fetchone()
    assert row is not None
    return int(row[0])


async def _add(uow: object, sku: str, created_by: uuid.UUID | None) -> object:
    return await uow.products.create(  # type: ignore[attr-defined]
        sku=sku,
        title="T",
        category="kurtas",
        attributes={},
        provenance={"title": _STAMP, "category": _STAMP},
        created_by=created_by,
    )


async def test_tenant_uow_sets_tenant_and_user_gucs(world: World) -> None:
    from app.repositories.uow import tenant_uow

    acme, operator = world.tenant("acme"), world.user("operator@acme.test")
    async with tenant_uow(acme, user_id=operator) as uow:
        assert await _guc(uow.session, "app.tenant_id") == str(acme)
        assert await _guc(uow.session, "app.user_id") == str(operator)


async def test_tenant_uow_without_user_sets_no_user_guc(world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme = world.tenant("acme")
    entity = uuid.uuid4()
    async with tenant_uow(acme) as uow:
        assert await _guc(uow.session, "app.tenant_id") == str(acme)
        assert await _guc(uow.session, "app.user_id") is None
        await uow.audit.write(
            tenant_id=acme,
            actor_type="system",
            actor_user_id=None,
            action="product.s12_probe",
            entity_type="product",
            entity_id=entity,
            after=json.dumps({"attributes": {}}),
        )
    rows = audit_rows(auth_db, "product.s12_probe")
    assert [(r[0], r[1], r[2], r[5]) for r in rows] == [(acme, "system", None, entity)]


async def test_tenant_uow_repos_share_session(world: World) -> None:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(world.tenant("acme")) as uow:
        assert uow.products.db is uow.session
        assert uow.attribute_definitions.db is uow.session
        assert uow.audit.db is uow.session
        assert uow.products.tenant_id == world.tenant("acme")


async def test_tenant_uow_exception_rolls_back(world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    operator = world.user("operator@acme.test")
    with pytest.raises(RuntimeError, match="boom"):
        async with tenant_uow(world.tenant("acme"), user_id=operator) as uow:
            await _add(uow, "S12-UOW-RB", operator)
            raise RuntimeError("boom")
    assert _count(auth_db, "S12-UOW-RB") == 0


async def test_tenant_uow_conflict_surfaces_inside(world: World, auth_db: Conn) -> None:
    from app.core.errors import ConflictError
    from app.repositories.uow import tenant_uow
    from mkos_schemas.codes import ErrorCode

    operator = world.user("operator@acme.test")
    async with tenant_uow(world.tenant("acme"), user_id=operator) as uow:
        await _add(uow, "S12-UOW-DUP", operator)
    with pytest.raises(ConflictError) as caught:
        async with tenant_uow(world.tenant("acme"), user_id=operator) as uow:
            try:
                await _add(uow, "S12-UOW-DUP", operator)
            except ConflictError as inside:
                assert inside.code is ErrorCode.SKU_CONFLICT
                raise
    assert caught.value.code is ErrorCode.SKU_CONFLICT
    assert _count(auth_db, "S12-UOW-DUP") == 1
