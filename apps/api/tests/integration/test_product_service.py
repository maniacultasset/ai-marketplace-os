"""S12 T7 (AC1, AC4, AC5; PLAN D2-D5, CP1, CP2): the product service as `app_rw`. Tenant and
actor come only from the principal; refusals happen before any transaction where they can; the
product, its facts and its audit row commit together."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from .auth_support import World, audit_rows, build_world
from .conftest import LOCAL_HOSTS
from .products_support import Conn, seed_dictionary

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def world(auth_db: Conn) -> World:
    seed_dictionary(auth_db)
    return build_world(auth_db)


def _principal(world: World, email: str) -> Any:
    from app.core.rbac import Principal

    slug = email.split("@", 1)[1].split(".", 1)[0]
    return Principal(
        user_id=world.user(email),
        tenant_id=world.tenant(slug),
        role=email.split("@", 1)[0],  # type: ignore[arg-type]
        email=email,
        jti=uuid.uuid4().hex,
    )


def _cmd(sku: str = "S12-SVC", **overrides: Any) -> Any:
    from app.services.products import NewProduct

    values: dict[str, Any] = {
        "sku": sku,
        "title": "Cotton Kurta",
        "category": "kurtas",
        "images": (),
        "attributes": {"color": "navy blue", "material": "organic cotton"},
    }
    values.update(overrides)
    return NewProduct(**values)


def _row(conn: Conn, sku: str) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT tenant_id, created_by, status, attributes, provenance FROM products WHERE sku = %s",
        [sku],
    ).fetchone()


def _count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row[0])


@pytest.fixture
def uow_spy(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    import app.services.products as svc

    entered: list[uuid.UUID] = []
    real = svc.tenant_uow  # type: ignore[attr-defined]

    @asynccontextmanager
    async def spy(tenant_id: uuid.UUID, **kwargs: Any) -> Any:
        entered.append(tenant_id)
        async with real(tenant_id, **kwargs) as uow:
            yield uow

    monkeypatch.setattr(svc, "tenant_uow", spy)
    return entered


async def test_create_row_under_principal_tenant_and_creator(world: World, auth_db: Conn) -> None:
    from app.services.products import create_product

    view = await create_product(_principal(world, "operator@acme.test"), _cmd())
    row = _row(auth_db, "S12-SVC")
    assert row is not None
    assert row[0] == world.tenant("acme")
    assert row[1] == world.user("operator@acme.test")
    assert view.attributes == {"color": "navy blue", "fabric_type": "organic cotton"}
    assert view.images == []
    assert view.category == "kurtas"


async def test_create_status_defaults_draft(world: World) -> None:
    from app.services.products import create_product

    view = await create_product(_principal(world, "owner@acme.test"), _cmd("S12-DRAFT"))
    assert view.status == "draft"


async def test_create_unknown_key_writes_nothing(world: World, auth_db: Conn) -> None:
    from app.core.errors import InputError
    from app.services.products import create_product
    from mkos_schemas.codes import ErrorCode

    with pytest.raises(InputError) as caught:
        await create_product(
            _principal(world, "operator@acme.test"), _cmd("S12-UNK", attributes={"shade": "x"})
        )
    assert caught.value.code is ErrorCode.UNKNOWN_FACT_KEY
    assert _row(auth_db, "S12-UNK") is None
    assert audit_rows(auth_db, "product.created") == []


async def test_create_images_refused_before_transaction(
    world: World, auth_db: Conn, uow_spy: list[uuid.UUID]
) -> None:
    from app.core.errors import NotFoundError
    from app.services.products import create_product

    with pytest.raises(NotFoundError) as caught:
        await create_product(
            _principal(world, "operator@acme.test"),
            _cmd("S12-IMG", images=("acme/products/a.jpg",)),
        )
    assert caught.value.field == "images[0]"
    assert uow_spy == []
    assert _row(auth_db, "S12-IMG") is None


async def test_create_without_category_refused_before_transaction(
    world: World, auth_db: Conn, uow_spy: list[uuid.UUID]
) -> None:
    from app.core.errors import InputError
    from app.services.products import create_product

    with pytest.raises(InputError) as caught:
        await create_product(
            _principal(world, "operator@acme.test"), _cmd("S12-NOCAT", category=None)
        )
    assert caught.value.field == "category"
    assert uow_spy == []


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"sku": "S12-\x00"}, "sku"),
        ({"title": "Kurta\x00"}, "title"),
        ({"attributes": {"color": "red\x00"}}, "attributes.color"),
    ],
    ids=["sku", "title", "attr_value"],
)
async def test_create_nul_refused_before_transaction(
    world: World, uow_spy: list[uuid.UUID], overrides: dict[str, Any], field: str
) -> None:
    from app.core.errors import InputError
    from app.services.products import create_product
    from mkos_schemas.codes import ErrorCode

    cmd = _cmd("S12-NUL", **{k: v for k, v in overrides.items() if k != "sku"})
    if "sku" in overrides:
        cmd = _cmd(overrides["sku"])
    with pytest.raises(InputError) as caught:
        await create_product(_principal(world, "operator@acme.test"), cmd)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.field == field
    assert uow_spy == []


async def test_create_writes_one_audit_row(world: World, auth_db: Conn) -> None:
    from app.services.products import create_product

    view = await create_product(_principal(world, "operator@acme.test"), _cmd("S12-AUD"))
    rows = audit_rows(auth_db, "product.created")
    assert len(rows) == 1
    tenant_id, actor_type, actor, _email, entity_type, entity_id, before, after, _rid, _r = rows[0]
    assert (tenant_id, actor_type, actor) == (
        world.tenant("acme"),
        "user",
        world.user("operator@acme.test"),
    )
    assert (entity_type, entity_id, before) == ("product", view.id, None)
    assert set(after) == {"attributes"}
    assert set(after["attributes"]) == {"color", "fabric_type"}


async def test_create_audit_failure_rolls_back_product(
    world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.identity import AuditRepository
    from app.services.products import create_product

    async def broken(self: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr(AuditRepository, "write", broken)
    with pytest.raises(RuntimeError, match="audit down"):
        await create_product(_principal(world, "operator@acme.test"), _cmd("S12-AUDFAIL"))
    assert _row(auth_db, "S12-AUDFAIL") is None
    assert audit_rows(auth_db, "product.created") == []


async def test_get_other_tenant_not_found(world: World) -> None:
    from app.core.errors import NotFoundError
    from app.services.products import create_product, get_product

    view = await create_product(_principal(world, "operator@acme.test"), _cmd("S12-GET"))
    assert (await get_product(_principal(world, "reviewer@acme.test"), view.id)).id == view.id
    with pytest.raises(NotFoundError) as caught:
        await get_product(_principal(world, "owner@bharat.test"), view.id)
    assert caught.value.field == "product_id"


async def test_list_passes_has_more(world: World) -> None:
    from app.services.products import create_product, list_products

    principal = _principal(world, "operator@acme.test")
    for n in range(3):
        await create_product(principal, _cmd(f"S12-LIST-{n}", attributes={}))
    first, more = await list_products(principal, limit=2, after=None)
    assert len(first) == 2 and more is True
    rest, more = await list_products(principal, limit=2, after=(first[-1].created_at, first[-1].id))
    assert len(rest) == 1 and more is False
    assert {v.sku for v in first + rest} == {"S12-LIST-0", "S12-LIST-1", "S12-LIST-2"}
    bharat, _ = await list_products(_principal(world, "owner@bharat.test"), limit=50, after=None)
    assert bharat == []
