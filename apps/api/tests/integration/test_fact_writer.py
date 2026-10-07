"""S13 B6 (Q6, AC 14; DATABASE.md:1102): the FactWriter as app_rw: per-key server-side merges,
no-op re-runs, typed columns with their provenance, and the dictionary refusals (the app check
first, the 0006 guard's SQLSTATE mapped behind it)."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest
from app.core.errors import InputError, RowCountError
from app.db.session import unit_of_work
from app.repositories.facts import FactWriter
from mkos_schemas.codes import ErrorCode

from .auth_support import World
from .conftest import LOCAL_HOSTS, Conn
from .products_support import insert_products

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
STAMP = "2026-10-07T00:00:00Z"


def seller(value: Any = "x", **extra: Any) -> dict[str, Any]:
    return {"value": value, "source": "seller", "captured_via": "import", "updated_at": STAMP,
            **extra}  # fmt: skip


def ai(value: Any = "x") -> dict[str, Any]:
    return {
        "value": value,
        "source": "ai",
        "captured_via": "agent",
        "confidence": 0.9,
        "evidence_ref": "image:abc",
        "agent_run_id": str(uuid.uuid4()),
        "updated_at": STAMP,
    }


@pytest.fixture
def product(products_world: World, auth_db: Conn) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = products_world.tenant("acme")
    (product_id,) = insert_products(auth_db, tenant, 1, prefix=f"FW-{uuid.uuid4().hex[:6]}")
    return tenant, product_id


def row(conn: Conn, product_id: uuid.UUID, columns: str) -> tuple[Any, ...]:
    found = conn.execute(
        f"SELECT {columns} FROM products WHERE id = %s", [product_id]  # noqa: S608
    ).fetchone()
    assert found is not None
    return tuple(found)


async def test_fact_writer_merges_per_key_concurrently(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product

    async def write(key: str) -> None:
        async with unit_of_work(tenant) as session:
            await FactWriter(session, tenant).set_attribute(product_id, key, seller(key))
            await asyncio.sleep(0.05)

    await asyncio.gather(*(write(k) for k in ("fit", "pattern", "occasion", "season")))
    (attributes,) = row(auth_db, product_id, "attributes")
    assert {k: v["value"] for k, v in attributes.items()} == {
        "fit": "fit", "pattern": "pattern", "occasion": "occasion", "season": "season",
    }  # fmt: skip


async def test_fact_writer_same_value_no_updated_at_bump(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    async with unit_of_work(tenant) as session:
        assert await FactWriter(session, tenant).set_attribute(product_id, "fit", seller("slim"))
    (first,) = row(auth_db, product_id, "updated_at")
    async with unit_of_work(tenant) as session:
        writer = FactWriter(session, tenant)
        assert not await writer.set_attribute(product_id, "fit", seller("slim"))
        assert await writer.set_typed(product_id, "mrp", Decimal("999.00"), seller(None))
    (second,) = row(auth_db, product_id, "updated_at")
    assert second > first
    async with unit_of_work(tenant) as session:
        writer = FactWriter(session, tenant)
        assert not await writer.set_typed(product_id, "mrp", Decimal("999.00"), seller(None))
        assert not await writer.set_attribute(product_id, "fit", seller("slim", updated_at="x"))
    assert row(auth_db, product_id, "updated_at") == (second,)


async def test_fact_writer_typed_column_provenance_check_holds(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    fact = {k: v for k, v in seller().items() if k != "value"}
    async with unit_of_work(tenant) as session:
        writer = FactWriter(session, tenant)
        await writer.set_typed(product_id, "mrp", Decimal("1299.00"), fact)
        await writer.set_typed_group(
            product_id,
            {"package_length_cm": Decimal(30), "package_breadth_cm": Decimal(20),
             "package_height_cm": Decimal(5)},
            fact,
        )  # fmt: skip
    mrp, length, provenance = row(auth_db, product_id, "mrp, package_length_cm, provenance")
    assert (mrp, length) == (Decimal("1299.00"), Decimal("30.00"))
    assert provenance["mrp"]["source"] == "seller"
    assert provenance["package_dimensions"]["captured_via"] == "import"
    assert {"title", "category"} <= set(provenance)


async def test_fact_writer_unknown_key_rejected(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    for key, call in (
        ("no_such_key", "attribute"),
        ("mrp", "attribute"),  # a column key is not an attributes key
        ("sku", "typed"),  # not a fact column
        ("hem", "disabled"),
    ):
        if call == "disabled":
            auth_db.execute("UPDATE attribute_definitions SET enabled = false WHERE key = 'hem'")
        try:
            with pytest.raises(InputError) as caught:
                async with unit_of_work(tenant) as session:
                    writer = FactWriter(session, tenant)
                    if call == "typed":
                        await writer.set_typed(product_id, key, "X", seller())
                    else:
                        await writer.set_attribute(product_id, key, seller())
            assert caught.value.code is ErrorCode.UNKNOWN_FACT_KEY
        finally:
            auth_db.execute("UPDATE attribute_definitions SET enabled = true WHERE key = 'hem'")
    assert row(auth_db, product_id, "attributes") == ({},)


async def test_fact_writer_other_tenant_product_rowcount_error(
    products_world: World, product: tuple[uuid.UUID, uuid.UUID]
) -> None:
    _, product_id = product
    other = products_world.tenant("bharat")
    with pytest.raises(RowCountError):
        async with unit_of_work(other) as session:
            await FactWriter(session, other).set_attribute(product_id, "fit", seller())
    with pytest.raises(RowCountError):
        async with unit_of_work(other) as session:
            await FactWriter(session, other).set_typed(product_id, "mrp", Decimal(1), seller())


async def test_fact_writer_ai_on_critical_refused(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    with pytest.raises(InputError) as caught:
        async with unit_of_work(tenant) as session:
            await FactWriter(session, tenant).set_attribute(product_id, "fabric_type", ai())
    assert caught.value.code is ErrorCode.FACT_SOURCE_FORBIDDEN
    with pytest.raises(InputError):
        async with unit_of_work(tenant) as session:
            await FactWriter(session, tenant).set_typed(product_id, "mrp", Decimal(9), ai(None))
    assert row(auth_db, product_id, "attributes, mrp") == ({}, None)


async def test_fact_writer_ai_on_critical_override_refused(
    products_world: World, auth_db: Conn
) -> None:
    tenant = products_world.tenant("acme")
    category = f"cat_{uuid.uuid4().hex[:10]}"
    (product_id,) = insert_products(auth_db, tenant, 1, prefix="FW-OVR", category=category)
    auth_db.execute(
        "INSERT INTO category_attributes (category, attribute_key, relevance, critical_override, "
        "display_order, enabled) VALUES (%s, 'fit', 'core', true, 1, true)",
        [category],
    )
    try:
        with pytest.raises(InputError) as caught:
            async with unit_of_work(tenant) as session:
                await FactWriter(session, tenant).set_attribute(product_id, "fit", ai())
        assert caught.value.code is ErrorCode.FACT_SOURCE_FORBIDDEN
        async with unit_of_work(tenant) as session:
            assert await FactWriter(session, tenant).set_attribute(product_id, "fit", seller())
    finally:
        auth_db.execute(
            "UPDATE category_attributes SET enabled = false WHERE category = %s", [category]
        )


async def test_fact_writer_guard_sqlstate_mapped(
    product: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app check bypassed: the 0006 guard refuses and its SQLSTATE becomes the same 422."""
    tenant, product_id = product

    async def no_check(*_args: Any) -> None:
        return None

    monkeypatch.setattr(FactWriter, "_check", no_check)
    cases = [("fabric_type", ai(), ErrorCode.FACT_SOURCE_FORBIDDEN),
             ("no_such_key", seller(), ErrorCode.UNKNOWN_FACT_KEY)]  # fmt: skip
    for key, fact, code in cases:
        with pytest.raises(InputError) as caught:
            async with unit_of_work(tenant) as session:
                await FactWriter(session, tenant).set_attribute(product_id, key, fact)
        assert caught.value.code is code
        assert caught.value.field == f"attributes.{key}"
