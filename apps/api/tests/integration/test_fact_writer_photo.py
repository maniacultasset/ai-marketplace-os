"""SI3 T6 (AC4, AC5, AC10): the guarded photo write, the reject and the confirm, as app_rw."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.core.errors import InputError, RowCountError
from app.db.session import unit_of_work
from app.repositories.facts import FactWriter
from mkos_schemas.codes import ErrorCode
from psycopg.types.json import Jsonb

from .auth_support import World
from .conftest import LOCAL_HOSTS, Conn
from .products_support import insert_products

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
STAMP = "2026-11-07T00:00:00.000000Z"
LATER = "2026-11-07T01:00:00.000000Z"
RUN = str(uuid.uuid4())
IMG = f"image:{uuid.uuid4()}"
USER = uuid.uuid4()


def photo(value: Any = "v-neck", *, at: str = STAMP, **extra: Any) -> dict[str, Any]:
    return {
        "value": value,
        "normalized": value,
        "source": "ai",
        "captured_via": "agent",
        "agent_run_id": RUN,
        "confidence": 0.95,
        "evidence_ref": IMG,
        "updated_at": at,
        **extra,
    }


def seller(value: Any = "Round") -> dict[str, Any]:
    return {"value": value, "source": "seller", "captured_via": "api", "updated_at": STAMP}


@pytest.fixture
def product(products_world: World, auth_db: Conn) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = products_world.tenant("acme")
    (product_id,) = insert_products(auth_db, tenant, 1, prefix=f"PH-{uuid.uuid4().hex[:6]}")
    return tenant, product_id


def stored(conn: Conn, product_id: uuid.UUID) -> dict[str, Any]:
    found = conn.execute("SELECT attributes FROM products WHERE id = %s", [product_id]).fetchone()
    assert found is not None and isinstance(found[0], dict)
    return found[0]


def put(conn: Conn, product_id: uuid.UUID, key: str, fact: dict[str, Any]) -> None:
    """A stored fact written by the admin login (an arrangement, not the app)."""
    conn.execute(
        "UPDATE products SET attributes = attributes || jsonb_build_object(%s::text, %s::jsonb) "
        "WHERE id = %s",
        [key, Jsonb(fact), product_id],
    )


async def write(
    tenant: uuid.UUID, product_id: uuid.UUID, key: str, fact: dict[str, Any], expected: Any
) -> bool:
    async with unit_of_work(tenant) as session:
        return await FactWriter(session, tenant).set_photo_attribute(
            product_id, key, fact, expected=expected
        )


async def test_lands_when_absent(product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn) -> None:
    tenant, product_id = product
    assert await write(tenant, product_id, "neckline", photo(), None)
    assert stored(auth_db, product_id)["neckline"] == photo()


async def test_replaces_the_expected_unconfirmed_ai_fact(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    put(auth_db, product_id, "neckline", photo("round neck"))
    assert await write(tenant, product_id, "neckline", photo(at=LATER), photo("round neck"))
    assert stored(auth_db, product_id)["neckline"]["value"] == "v-neck"


async def test_absent_expected_but_a_fact_appeared(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    put(auth_db, product_id, "neckline", photo("round neck"))
    assert not await write(tenant, product_id, "neckline", photo(), None)
    assert stored(auth_db, product_id)["neckline"]["value"] == "round neck"


async def test_reject_during_the_run_wins(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    assert not await write(tenant, product_id, "neckline", photo(), photo("round neck"))
    assert "neckline" not in stored(auth_db, product_id)


async def test_another_photo_run_replaced_it(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    put(auth_db, product_id, "neckline", photo("round neck", at=LATER))
    assert not await write(tenant, product_id, "neckline", photo(), photo("round neck"))
    assert stored(auth_db, product_id)["neckline"]["value"] == "round neck"


async def test_expected_without_updated_at_never_matches(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    put(auth_db, product_id, "neckline", photo("round neck"))
    expected = {k: v for k, v in photo("round neck").items() if k != "updated_at"}
    assert not await write(tenant, product_id, "neckline", photo(at=LATER), expected)
    assert stored(auth_db, product_id)["neckline"]["value"] == "round neck"


@pytest.mark.parametrize(
    "existing",
    [
        seller(),
        {**photo("round neck"), "source": "document", "captured_via": "import"},
        photo("round neck", confirmed_by=str(USER), confirmed_at=STAMP),
        {**photo("round neck"), "source": "derived", "captured_via": "system"},
    ],
)
async def test_never_replaces_a_trusted_fact(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn, existing: dict[str, Any]
) -> None:
    tenant, product_id = product
    put(auth_db, product_id, "neckline", existing)
    for expected in (None, photo("round neck")):
        assert not await write(tenant, product_id, "neckline", photo(at=LATER), expected)
    assert stored(auth_db, product_id)["neckline"] == existing


async def test_same_value_twice_writes_nothing(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    assert await write(tenant, product_id, "neckline", photo(), None)
    assert not await write(tenant, product_id, "neckline", photo(at=LATER), photo())
    assert stored(auth_db, product_id)["neckline"]["updated_at"] == STAMP


@pytest.mark.parametrize("key", ["number_of_pieces", "fiber_composition"])
async def test_critical_keys_refused(product: tuple[uuid.UUID, uuid.UUID], key: str) -> None:
    tenant, product_id = product
    with pytest.raises(InputError) as refused:
        await write(tenant, product_id, key, photo(3), None)
    assert refused.value.code is ErrorCode.FACT_SOURCE_FORBIDDEN


async def test_db_guard_refuses_ai_pieces_without_the_python_check(
    product: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch, auth_db: Conn
) -> None:
    async def no_check(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(FactWriter, "_check", no_check)
    tenant, product_id = product
    with pytest.raises(InputError) as refused:
        await write(tenant, product_id, "number_of_pieces", photo(3), None)
    assert refused.value.code is ErrorCode.FACT_SOURCE_FORBIDDEN
    assert "number_of_pieces" not in stored(auth_db, product_id)


@pytest.mark.parametrize(
    ("fact", "expected"),
    [
        (seller("v-neck"), None),
        ({**photo(), "captured_via": "api"}, None),
        (photo(), seller()),
        (photo(), photo("round neck", confirmed_by=str(USER), confirmed_at=STAMP)),
    ],
)
async def test_programming_errors(
    product: tuple[uuid.UUID, uuid.UUID], fact: dict[str, Any], expected: Any
) -> None:
    tenant, product_id = product
    with pytest.raises(ValueError):
        await write(tenant, product_id, "neckline", fact, expected)


async def test_clear_removes_ai_and_seller_but_not_document(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    put(auth_db, product_id, "neckline", photo())
    put(auth_db, product_id, "fit", seller("slim"))
    put(
        auth_db,
        product_id,
        "pattern",
        {**photo("solid"), "source": "document", "captured_via": "import"},
    )
    async with unit_of_work(tenant) as session:
        writer = FactWriter(session, tenant)
        assert await writer.clear_attribute(product_id, "neckline")
        assert await writer.clear_attribute(product_id, "fit")
    async with unit_of_work(tenant) as session:
        with pytest.raises(InputError) as refused:
            await FactWriter(session, tenant).clear_attribute(product_id, "pattern")
    assert refused.value.code is ErrorCode.VALIDATION_FAILED
    attrs = stored(auth_db, product_id)
    assert "neckline" not in attrs and "fit" not in attrs and "pattern" in attrs


async def confirm(tenant: uuid.UUID, product_id: uuid.UUID, key: str) -> bool:
    async with unit_of_work(tenant) as session:
        return await FactWriter(session, tenant).confirm_attribute(
            product_id, key, user_id=USER, at=LATER
        )


async def test_confirm_sets_both_fields(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id = product
    put(auth_db, product_id, "neckline", photo())
    assert await confirm(tenant, product_id, "neckline")
    fact = stored(auth_db, product_id)["neckline"]
    assert (fact["confirmed_by"], fact["confirmed_at"]) == (str(USER), LATER)
    assert fact["value"] == "v-neck" and fact["source"] == "ai"
    assert not await confirm(tenant, product_id, "neckline")  # a repeat is a no-op


@pytest.mark.parametrize(
    "existing",
    [seller(), {**photo(), "evidence_ref": "import_row:1", "evidence_snapshot": {"row": 1}}, None],
)
async def test_confirm_refuses_other_facts(
    product: tuple[uuid.UUID, uuid.UUID], auth_db: Conn, existing: dict[str, Any] | None
) -> None:
    tenant, product_id = product
    if existing is not None:
        put(auth_db, product_id, "neckline", existing)
    with pytest.raises(InputError) as refused:
        await confirm(tenant, product_id, "neckline")
    assert refused.value.code is ErrorCode.VALIDATION_FAILED


async def test_tenant_isolation(products_world: World, auth_db: Conn) -> None:
    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    (theirs,) = insert_products(auth_db, bharat, 1, prefix=f"PB-{uuid.uuid4().hex[:6]}")
    with pytest.raises(RowCountError):
        await write(acme, theirs, "neckline", photo(), None)
    with pytest.raises(RowCountError):
        await confirm(acme, theirs, "neckline")
    assert "neckline" not in stored(auth_db, theirs)
