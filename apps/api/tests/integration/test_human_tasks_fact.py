"""SI3 T9 (AC7, AC11): fact tasks in the repository, as app_rw."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.db.session import unit_of_work
from app.repositories.human_tasks import HumanTaskRepository

from .auth_support import World
from .conftest import LOCAL_HOSTS, Conn
from .products_support import insert_products

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
TARGET = "facts.attributes.color"


def _task(product_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "product_id": product_id,
        "kind": "missing_fact",
        "target_path": TARGET,
        "question": "What is the colour of this product?",
        "reason": "The photos suggest maroon, but not clearly enough to use it.",
        "expected_answer": {"kind": "enum", "allowed_values": ["maroon", "red"]},
        "suggestions": [{"value": "maroon", "label": "maroon", "source": "ai"}],
        "evidence": [{"kind": "source_value", "text": "photos: maroon", "ref": "image:x"}],
    }
    base.update(overrides)
    return base


@pytest.fixture
def setup(products_world: World, auth_db: Conn) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = products_world.tenant("acme")
    (product_id,) = insert_products(auth_db, tenant, 1, prefix=f"HT-{uuid.uuid4().hex[:6]}")
    return tenant, product_id, products_world.user("owner@acme.test")


async def _open(tenant: uuid.UUID, **task: Any) -> bool:
    async with unit_of_work(tenant) as session:
        return await HumanTaskRepository(session, tenant).open_fact_task(**task)


def _rows(conn: Conn, product_id: uuid.UUID) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM human_tasks WHERE product_id = %s ORDER BY created_at, id", [product_id]
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


async def test_insert_lands(setup: tuple[uuid.UUID, uuid.UUID, uuid.UUID], auth_db: Conn) -> None:
    tenant, product_id, _user = setup
    assert await _open(tenant, **_task(product_id))
    (row,) = _rows(auth_db, product_id)
    assert (row["status"], row["resume_from"], row["kind"]) == ("open", "generate", "missing_fact")
    assert row["suggestions"][0]["value"] == "maroon"


async def test_one_live_task_per_target(
    setup: tuple[uuid.UUID, uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id, _user = setup
    assert await _open(tenant, **_task(product_id))
    assert not await _open(tenant, **_task(product_id))
    assert not await _open(tenant, **_task(product_id, kind="conflicting_facts"))
    assert len(_rows(auth_db, product_id)) == 1


async def test_cancel_then_open_again(
    setup: tuple[uuid.UUID, uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id, _user = setup
    assert await _open(tenant, **_task(product_id))
    async with unit_of_work(tenant) as session:
        repo = HumanTaskRepository(session, tenant)
        assert await repo.cancel_fact_tasks(product_id, [TARGET, "facts.attributes.fit"]) == 1
        assert await repo.cancel_fact_tasks(product_id, [TARGET]) == 0
    assert [(r["status"], r["cancelled_reason"]) for r in _rows(auth_db, product_id)] == [
        ("cancelled", "superseded")
    ]
    assert await _open(tenant, **_task(product_id))


async def test_answer_and_apply(
    setup: tuple[uuid.UUID, uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id, user = setup
    assert await _open(tenant, **_task(product_id))
    (row,) = _rows(auth_db, product_id)
    async with unit_of_work(tenant) as session:
        repo = HumanTaskRepository(session, tenant)
        task = await repo.get_for_update(row["id"])
        assert task is not None and task.status == "open"
        assert await repo.answer_and_apply(row["id"], answer={"value": "maroon"}, user_id=user) == 1
    (done,) = _rows(auth_db, product_id)
    assert done["status"] == "applied" and done["answer"] == {"value": "maroon"}
    assert done["answered_by_user_id"] == user
    assert done["answered_at"] is not None and done["applied_at"] is not None


async def test_applying_a_cancelled_task_changes_nothing(
    setup: tuple[uuid.UUID, uuid.UUID, uuid.UUID], auth_db: Conn
) -> None:
    tenant, product_id, user = setup
    assert await _open(tenant, **_task(product_id))
    (row,) = _rows(auth_db, product_id)
    async with unit_of_work(tenant) as session:
        repo = HumanTaskRepository(session, tenant)
        await repo.cancel_fact_tasks(product_id, [TARGET])
        assert await repo.answer_and_apply(row["id"], answer={"skip": True}, user_id=user) == 0
    (after,) = _rows(auth_db, product_id)
    assert after["status"] == "cancelled" and after["answer"] is None


async def test_tenant_isolation(products_world: World, auth_db: Conn) -> None:
    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")
    (theirs,) = insert_products(auth_db, bharat, 1, prefix=f"HB-{uuid.uuid4().hex[:6]}")
    assert await _open(bharat, **_task(theirs))
    (row,) = _rows(auth_db, theirs)
    async with unit_of_work(acme) as session:
        repo = HumanTaskRepository(session, acme)
        assert await repo.get_for_update(row["id"]) is None
        assert await repo.cancel_fact_tasks(theirs, [TARGET]) == 0
        owner = products_world.user("owner@acme.test")
        assert await repo.answer_and_apply(row["id"], answer={"skip": True}, user_id=owner) == 0
    assert not await _open(acme, **_task(theirs, target_path="facts.attributes.fit"))
    rows = _rows(auth_db, theirs)
    assert [r["status"] for r in rows] == ["open"]
