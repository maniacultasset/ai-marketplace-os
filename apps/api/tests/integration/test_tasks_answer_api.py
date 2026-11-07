"""SI3 T16 (AC11): `POST /api/v1/tasks/{task_id}/answer` as app_rw through the real app. A value
becomes the seller's own fact with `human_task:` evidence, and the task is applied."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from app.db.session import unit_of_work
from app.repositories.human_tasks import HumanTaskRepository

from .auth_support import World
from .conftest import LOCAL_HOSTS, Conn
from .products_support import bad_bearer, bearer_for, insert_products

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
OPERATOR = "operator@acme.test"
COLOURS = ["black", "red", "maroon", "navy blue"]
# AnswerSpec shapes (DATABASE.md:1411): what the photo screen opens, and a canonical SI4 question
COLOUR_SPEC: dict[str, Any] = {"kind": "enum", "allowed_values": COLOURS}
PIECES_SPEC: dict[str, Any] = {"kind": "number", "min": 1, "max": 50}
TEXT_SPEC: dict[str, Any] = {"kind": "text"}


def _url(task_id: object) -> str:
    return f"/api/v1/tasks/{task_id}/answer"


async def _open(
    world: World,
    conn: Conn,
    *,
    slug: str = "acme",
    key: str = "color",
    kind: str = "missing_fact",
    expected: dict[str, Any] = COLOUR_SPEC,
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = world.tenant(slug)
    (product_id,) = insert_products(conn, tenant, 1, prefix=f"TA-{uuid.uuid4().hex[:6]}")
    async with unit_of_work(tenant) as session:
        assert await HumanTaskRepository(session, tenant).open_fact_task(
            product_id=product_id,
            kind=kind,
            target_path=f"facts.attributes.{key}",
            question=f"What is the {key} of this product?",
            reason="The photos suggest maroon, but not clearly enough to use it.",
            expected_answer=expected,
            suggestions=[],
            evidence=[],
        )
    row = conn.execute("SELECT id FROM human_tasks WHERE product_id = %s", [product_id]).fetchone()
    assert row is not None and isinstance(row[0], uuid.UUID)
    return row[0], product_id


async def _answer(
    api: httpx.AsyncClient,
    world: World,
    task_id: object,
    body: object,
    *,
    email: str = OPERATOR,
    key: str | None = None,
) -> httpx.Response:
    headers = {**bearer_for(world, email), "Idempotency-Key": key or str(uuid.uuid4())}
    return await api.post(_url(task_id), headers=headers, json=body)


def _fact(conn: Conn, product_id: uuid.UUID, key: str) -> Any:
    row = conn.execute("SELECT attributes -> %s FROM products WHERE id = %s", [key, product_id])
    found = row.fetchone()
    return None if found is None else found[0]


def _task(conn: Conn, task_id: uuid.UUID) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM human_tasks WHERE id = %s", [task_id])
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


# ---- the case matrix ----


@pytest.mark.endpoint("tasks_answer", case="success")
async def test_tasks_answer_value_writes_a_seller_fact(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, product_id = await _open(products_world, auth_db)
    response = await _answer(api, products_world, task_id, {"value": "Maroon"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied" and body["answered_at"] is not None
    assert "answer" not in body and "evidence" not in body
    fact = _fact(auth_db, product_id, "color")
    assert (fact["value"], fact["source"], fact["captured_via"]) == (
        "maroon",
        "seller",
        "human_task",
    )
    assert fact["evidence_ref"] == f"human_task:{task_id}"
    assert fact["evidence_snapshot"]["answer"] == "Maroon"
    actions = [
        r[0]
        for r in auth_db.execute(
            "SELECT action FROM audit_log WHERE entity_id = %s ORDER BY occurred_at, id",
            [product_id],
        ).fetchall()
    ]
    assert sorted(str(a) for a in actions) == ["human_task.answered", "human_task.applied"]


@pytest.mark.endpoint("tasks_answer", case="validation")
@pytest.mark.parametrize(
    "body", [{}, {"value": "x", "skip": True}, {"skip": False}, {"value": None}]
)
async def test_tasks_answer_validation_422(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, body: object
) -> None:
    task_id, _ = await _open(products_world, auth_db)
    response = await _answer(api, products_world, task_id, body)
    assert response.status_code == 422
    assert _task(auth_db, task_id)["status"] == "open"


@pytest.mark.endpoint("tasks_answer", case="missing_auth")
async def test_tasks_answer_missing_auth_401(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.post(
        _url(uuid.uuid4()), headers={"Idempotency-Key": "k-1"}, json={"skip": True}
    )
    assert response.status_code == 401


async def _invalid(api: httpx.AsyncClient, world: World, variant: str) -> None:
    headers = {**bad_bearer(world, variant, OPERATOR), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(_url(uuid.uuid4()), headers=headers, json={"skip": True})
    assert response.status_code == 401


@pytest.mark.endpoint("tasks_answer", case="invalid_token", variant="expired")
async def test_tasks_answer_invalid_token_expired(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "expired")


@pytest.mark.endpoint("tasks_answer", case="invalid_token", variant="bad_signature")
async def test_tasks_answer_invalid_token_bad_signature(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "bad_signature")


@pytest.mark.endpoint("tasks_answer", case="invalid_token", variant="wrong_typ")
async def test_tasks_answer_invalid_token_wrong_typ(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "wrong_typ")


@pytest.mark.endpoint("tasks_answer", case="wrong_role")
async def test_tasks_answer_wrong_role_403(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, _ = await _open(products_world, auth_db)
    response = await _answer(
        api, products_world, task_id, {"skip": True}, email="reviewer@acme.test"
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _task(auth_db, task_id)["status"] == "open"


@pytest.mark.endpoint("tasks_answer", case="wrong_tenant")
async def test_tasks_answer_foreign_404(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, _ = await _open(products_world, auth_db)
    foreign = await _answer(
        api, products_world, task_id, {"skip": True}, email="operator@bharat.test"
    )
    missing = await _answer(
        api, products_world, uuid.uuid4(), {"skip": True}, email="operator@bharat.test"
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert _task(auth_db, task_id)["status"] == "open"


@pytest.mark.endpoint("tasks_answer", case="not_found")
async def test_tasks_answer_not_found_404(api: httpx.AsyncClient, products_world: World) -> None:
    response = await _answer(api, products_world, uuid.uuid4(), {"skip": True})
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


@pytest.mark.endpoint("tasks_answer", case="conflict")
async def test_tasks_answer_different_answer_409(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, product_id = await _open(products_world, auth_db)
    assert (await _answer(api, products_world, task_id, {"value": "red"})).status_code == 200
    again = await _answer(api, products_world, task_id, {"value": "maroon"})
    assert again.status_code == 409
    assert again.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    assert _fact(auth_db, product_id, "color")["value"] == "red"


# ---- the rest of AC11 ----


async def test_tasks_answer_skip_writes_no_fact(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, product_id = await _open(products_world, auth_db)
    response = await _answer(api, products_world, task_id, {"skip": True})
    assert response.status_code == 200 and response.json()["status"] == "applied"
    assert _fact(auth_db, product_id, "color") is None
    assert _task(auth_db, task_id)["answer"] == {"skip": True}


async def test_tasks_answer_same_answer_again_is_200(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, _ = await _open(products_world, auth_db)
    assert (await _answer(api, products_world, task_id, {"value": "red"})).status_code == 200
    again = await _answer(api, products_world, task_id, {"value": "red"})
    assert again.status_code == 200 and again.json()["status"] == "applied"


async def test_tasks_answer_idempotency_key(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, _ = await _open(products_world, auth_db)
    key = str(uuid.uuid4())
    first = await _answer(api, products_world, task_id, {"value": "red"}, key=key)
    replay = await _answer(api, products_world, task_id, {"value": "red"}, key=key)
    assert replay.status_code == 200 and replay.content == first.content
    reused = await _answer(api, products_world, task_id, {"value": "black"}, key=key)
    assert reused.status_code == 409
    assert reused.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"


async def test_tasks_answer_an_account_task_is_422(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    tenant = products_world.tenant("acme")
    owner = products_world.user("owner@acme.test")
    account = auth_db.execute(
        "INSERT INTO marketplace_accounts (tenant_id, marketplace, seller_id, token_enc, "
        "kms_key_version, expires_at, connected_by_user_id) VALUES (%s, 'amazon', %s, %s, 'v1', "
        "now() + interval '1 hour', %s) RETURNING id",
        [tenant, f"TA{uuid.uuid4().hex[:8]}", b"sealed", owner],
    ).fetchone()
    assert account is not None and isinstance(account[0], uuid.UUID)
    async with unit_of_work(tenant) as session:
        assert await HumanTaskRepository(session, tenant).open_reconnect(account[0])
    row = auth_db.execute(
        "SELECT id FROM human_tasks WHERE marketplace_account_id = %s", [account[0]]
    ).fetchone()
    assert row is not None
    response = await _answer(api, products_world, row[0], {"skip": True})
    assert response.status_code == 422


async def test_tasks_answer_a_cancelled_task_is_409(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, product_id = await _open(products_world, auth_db)
    tenant = products_world.tenant("acme")
    async with unit_of_work(tenant) as session:
        await HumanTaskRepository(session, tenant).cancel_fact_tasks(
            product_id, ["facts.attributes.color"]
        )
    response = await _answer(api, products_world, task_id, {"value": "red"})
    assert response.status_code == 409


@pytest.mark.parametrize("value", ["three", True, 0, 51])
async def test_tasks_answer_pieces_must_be_a_count(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, value: object
) -> None:
    task_id, product_id = await _open(
        products_world, auth_db, key="number_of_pieces", expected=PIECES_SPEC
    )
    response = await _answer(api, products_world, task_id, {"value": value})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"][0]["loc"][-1] == "value"
    assert _fact(auth_db, product_id, "number_of_pieces") is None


async def test_tasks_answer_pieces_count_is_written(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, product_id = await _open(
        products_world, auth_db, key="number_of_pieces", expected=PIECES_SPEC
    )
    response = await _answer(api, products_world, task_id, {"value": 3})
    assert response.status_code == 200, response.text
    assert _fact(auth_db, product_id, "number_of_pieces")["value"] == 3


async def test_tasks_answer_an_out_of_list_value_is_422(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, _ = await _open(products_world, auth_db)
    response = await _answer(api, products_world, task_id, {"value": "chartreuse"})
    assert response.status_code == 422
    assert _task(auth_db, task_id)["status"] == "open"


@pytest.mark.parametrize("value", ["banana", "maroon", 7])
async def test_tasks_answer_a_canonical_enum_spec_takes_only_its_values(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, value: object
) -> None:
    """Round 1 F1: a question in the canonical AnswerSpec shape (an SI4 issue question on the same
    target) refuses a value outside its `allowed_values`, even one in the photo vocabulary."""
    spec = {"kind": "enum", "allowed_values": ["red", "blue"]}
    task_id, product_id = await _open(products_world, auth_db, expected=spec)
    response = await _answer(api, products_world, task_id, {"value": value})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"][0]["loc"][-1] == "value"
    assert _task(auth_db, task_id)["status"] == "open"
    assert _fact(auth_db, product_id, "color") is None


async def test_tasks_answer_a_canonical_enum_spec_stores_the_canonical_value(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    spec = {"kind": "enum", "allowed_values": ["red", "blue"]}
    task_id, product_id = await _open(products_world, auth_db, expected=spec)
    response = await _answer(api, products_world, task_id, {"value": " Red "})
    assert response.status_code == 200, response.text
    assert _fact(auth_db, product_id, "color")["value"] == "red"


async def test_tasks_answer_an_enum_spec_without_values_is_422(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, product_id = await _open(products_world, auth_db, expected={"kind": "enum"})
    response = await _answer(api, products_world, task_id, {"value": "red"})
    assert response.status_code == 422, response.text
    assert _fact(auth_db, product_id, "color") is None


@pytest.mark.parametrize("text", ["\N{DRAGON}" * 500, "\x01" * 500, "a" * 2000])
async def test_tasks_answer_long_text_snapshot_is_bounded(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, text: str
) -> None:
    task_id, product_id = await _open(products_world, auth_db, key="occasion", expected=TEXT_SPEC)
    response = await _answer(api, products_world, task_id, {"value": text})
    assert response.status_code == 200, response.text
    fact = _fact(auth_db, product_id, "occasion")
    assert fact["value"] == text
    assert len(fact["evidence_snapshot"]["answer"]) < len(text) or len(text) <= 500


async def test_tasks_answer_nul_is_422(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, _ = await _open(products_world, auth_db, key="occasion", expected=TEXT_SPEC)
    response = await _answer(api, products_world, task_id, {"value": "a\x00b"})
    assert response.status_code == 422


async def test_tasks_answer_two_at_once_one_wins(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, _ = await _open(products_world, auth_db)
    a, b = await asyncio.gather(
        _answer(api, products_world, task_id, {"value": "red"}),
        _answer(api, products_world, task_id, {"value": "black"}),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 409]


async def test_tasks_answer_value_stays_out_of_the_task_and_audit_reason(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    task_id, product_id = await _open(products_world, auth_db)
    response = await _answer(api, products_world, task_id, {"value": "maroon"})
    assert response.status_code == 200
    assert "answer" not in response.json()
    rows = auth_db.execute(
        "SELECT action, reason, after FROM audit_log WHERE entity_id = %s ORDER BY action",
        [product_id],
    ).fetchall()
    assert [(a, r) for a, r, _ in rows] == [
        ("human_task.answered", "facts.attributes.color"),
        ("human_task.applied", "facts.attributes.color"),
    ]
    # the envelope holds the answered value, as product.fact_updated does for seller values
    answered: Any = rows[0][2]
    assert answered["attributes"]["color"]["value"] == "maroon" and rows[1][2] is None
