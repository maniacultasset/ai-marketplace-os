"""SI1 T6 (AC4): `FactWriter.clear_attribute` and `clear_typed` as app_rw. Run in the Docker
batch (`make api-tests-integration`)."""

from __future__ import annotations

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
STAMP = "2026-11-06T00:00:00Z"


def seller(value: Any = "x") -> dict[str, Any]:
    return {"value": value, "source": "seller", "captured_via": "api", "updated_at": STAMP}


def document(value: Any = "x") -> dict[str, Any]:
    return {
        "value": value,
        "source": "document",
        "captured_via": "import",
        "evidence_ref": "image:abc",
        "updated_at": STAMP,
    }


@pytest.fixture
def product(products_world: World, auth_db: Conn) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = products_world.tenant("acme")
    (product_id,) = insert_products(auth_db, tenant, 1, prefix=f"FC-{uuid.uuid4().hex[:6]}")
    return tenant, product_id


def row(conn: Conn, product_id: uuid.UUID, columns: str) -> tuple[Any, ...]:
    found = conn.execute(
        f"SELECT {columns} FROM products WHERE id = %s", [product_id]  # noqa: S608
    ).fetchone()
    assert found is not None
    return tuple(found)


async def test_clear_seller_attribute(product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn) -> None:
    tenant, product_id = product
    async with unit_of_work(tenant) as session:
        await FactWriter(session, tenant).set_attribute(product_id, "fit", seller("slim"))
    (before,) = row(auth_db, product_id, "updated_at")
    async with unit_of_work(tenant) as session:
        assert await FactWriter(session, tenant).clear_attribute(product_id, "fit")
    attributes, after = row(auth_db, product_id, "attributes, updated_at")
    assert "fit" not in attributes
    assert after > before


async def test_clear_twice_writes_nothing(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    async with unit_of_work(tenant) as session:
        writer = FactWriter(session, tenant)
        await writer.set_attribute(product_id, "fit", seller("slim"))
        assert await writer.clear_attribute(product_id, "fit")
    (before,) = row(auth_db, product_id, "updated_at")
    async with unit_of_work(tenant) as session:
        assert not await FactWriter(session, tenant).clear_attribute(product_id, "fit")
    assert row(auth_db, product_id, "updated_at") == (before,)


async def test_clear_document_attribute_refused(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    async with unit_of_work(tenant) as session:
        await FactWriter(session, tenant).set_attribute(product_id, "fit", document("slim"))
    with pytest.raises(InputError) as caught:
        async with unit_of_work(tenant) as session:
            await FactWriter(session, tenant).clear_attribute(product_id, "fit")
    assert (caught.value.code, caught.value.field) == (
        ErrorCode.VALIDATION_FAILED,
        "attributes.fit",
    )
    (attributes,) = row(auth_db, product_id, "attributes")
    assert attributes["fit"]["source"] == "document"


async def test_clear_mrp(product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn) -> None:
    tenant, product_id = product
    async with unit_of_work(tenant) as session:
        await FactWriter(session, tenant).set_typed(product_id, "mrp", Decimal("999.00"), seller())
    async with unit_of_work(tenant) as session:
        writer = FactWriter(session, tenant)
        assert await writer.clear_typed(product_id, "mrp")
        assert not await writer.clear_typed(product_id, "mrp")
    mrp, provenance = row(auth_db, product_id, "mrp, provenance")
    assert mrp is None and "mrp" not in provenance


async def test_clear_manufacturer(product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn) -> None:
    tenant, product_id = product
    party = auth_db.execute(
        "INSERT INTO business_parties (tenant_id, roles, legal_name, address_line1, postal_code, "
        "source) VALUES (%s, ARRAY['manufacturer'], 'Acme', '1 Road', '560001', 'seller') "
        "RETURNING id",
        [tenant],
    ).fetchone()
    assert party is not None
    async with unit_of_work(tenant) as session:
        await FactWriter(session, tenant).set_typed(
            product_id, "manufacturer_party_id", party[0], {"source": "seller", "updated_at": STAMP}
        )
    async with unit_of_work(tenant) as session:
        assert await FactWriter(session, tenant).clear_typed(product_id, "manufacturer")
    column, provenance = row(auth_db, product_id, "manufacturer_party_id, provenance")
    assert column is None and "manufacturer" not in provenance


async def test_clear_unknown_key(product: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant, product_id = product
    for clear in ("clear_attribute", "clear_typed"):
        with pytest.raises(InputError) as caught:
            async with unit_of_work(tenant) as session:
                await getattr(FactWriter(session, tenant), clear)(product_id, "no_such_key")
        assert caught.value.code is ErrorCode.UNKNOWN_FACT_KEY


async def test_clear_other_tenant_product(
    product: tuple[uuid.UUID, uuid.UUID], products_world: World
) -> None:
    _, product_id = product
    other = products_world.tenant("bharat")
    with pytest.raises(RowCountError):
        async with unit_of_work(other) as session:
            await FactWriter(session, other).clear_typed(product_id, "mrp")
    with pytest.raises(RowCountError):
        async with unit_of_work(other) as session:
            await FactWriter(session, other).clear_attribute(product_id, "fit")
