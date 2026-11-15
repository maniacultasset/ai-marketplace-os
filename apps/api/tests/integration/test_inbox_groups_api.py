"""G1 (prod bulk test, 15 Nov 2026): `GET /inbox/groups` and `POST /inbox/groups/answer` as app_rw
through the real app. Identical open questions across products are one group whose counts add up
to Home's Inbox counts; one answer writes each product that has no value, never one that has."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from app.db.session import unit_of_work
from app.repositories.human_tasks import HumanTaskRepository
from app.services.inbox_groups_model import task_key
from psycopg.types.json import Jsonb

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS, Conn
from .products_support import bad_bearer, bearer_for, insert_products, soft_delete

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
GROUPS = "/api/v1/inbox/groups"
ANSWER = "/api/v1/inbox/groups/answer"
HOME = "/api/v1/home/summary"
OPERATOR = "operator@acme.test"
COLOUR_SPEC: dict[str, Any] = {"kind": "enum", "allowed_values": ["black", "red", "maroon"]}
TEXT_SPEC: dict[str, Any] = {"kind": "text", "max_length": 256}
COLOUR = "facts.attributes.color"
FABRIC = "facts.attributes.fabric_type"
IDENTIFIER = "facts.identifier"
COLOUR_KEY = task_key("missing_fact", COLOUR, COLOUR_SPEC)


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The grouped Inbox on, and no review-queue re-check on read (the rows keep the states the
    test wrote, as the Home summary suite does)."""
    from app.core.config import get_settings
    from app.services import listing_display

    async def no_recheck(*_: Any) -> None:
        return None

    monkeypatch.setenv("INBOX_GROUPS_ENABLED", "true")
    monkeypatch.setattr(listing_display, "recheck_queue", no_recheck)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@contextmanager
def _guards_off(conn: Conn) -> Iterator[None]:
    """Listings and issues in the states a test needs (set-up only, as `home_support`)."""
    conn.execute("SET session_replication_role = replica")
    try:
        yield
    finally:
        conn.execute("SET session_replication_role = DEFAULT")


def _hdrs(world: World, email: str = OPERATOR, key: str | None = None) -> dict[str, str]:
    return {**bearer_for(world, email), "Idempotency-Key": key or str(uuid.uuid4())}


async def _ask(
    world: World,
    conn: Conn,
    product_id: uuid.UUID,
    *,
    slug: str = "acme",
    target: str = COLOUR,
    spec: dict[str, Any] = COLOUR_SPEC,
) -> uuid.UUID:
    """One open `missing_fact` question on `target` of the product."""
    tenant = world.tenant(slug)
    async with unit_of_work(tenant) as session:
        assert await HumanTaskRepository(session, tenant).open_photo_fact_task(
            product_id=product_id,
            kind="missing_fact",
            target_path=target,
            question="Add the value on the product.",
            reason="Amazon needs it for this category.",
            expected_answer=spec,
            suggestions=[],
            evidence=[],
        )
    row = conn.execute(
        "SELECT id FROM human_tasks WHERE product_id = %s AND target_path = %s AND status = 'open'",
        [product_id, target],
    ).fetchone()
    assert row is not None and isinstance(row[0], uuid.UUID)
    return row[0]


def _products(world: World, conn: Conn, n: int, slug: str = "acme") -> list[uuid.UUID]:
    return insert_products(conn, world.tenant(slug), n, prefix=f"G1-{uuid.uuid4().hex[:6]}")


async def _groups(
    api: httpx.AsyncClient, world: World, view: str = "all", email: str = OPERATOR
) -> dict[str, Any]:
    response = await api.get(GROUPS, params={"view": view}, headers=bearer_for(world, email))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _group(body: dict[str, Any], key: str) -> dict[str, Any]:
    found = [g for g in body["groups"] if g["key"] == key]
    assert len(found) == 1, [g["key"] for g in body["groups"]]
    group: dict[str, Any] = found[0]
    return group


async def _answer(
    api: httpx.AsyncClient,
    world: World,
    task_ids: list[uuid.UUID],
    value: object,
    *,
    group: str = COLOUR_KEY,
    email: str = OPERATOR,
    key: str | None = None,
) -> httpx.Response:
    body = {"group": group, "task_ids": [str(t) for t in task_ids], "value": value}
    return await api.post(ANSWER, headers=_hdrs(world, email, key), json=body)


def _status(conn: Conn, task_id: uuid.UUID) -> str:
    row = conn.execute("SELECT status FROM human_tasks WHERE id = %s", [task_id]).fetchone()
    assert row is not None
    return str(row[0])


def _colour(conn: Conn, product_id: uuid.UUID) -> Any:
    row = conn.execute(
        "SELECT attributes -> 'color' FROM products WHERE id = %s", [product_id]
    ).fetchone()
    return None if row is None else row[0]


def _row_bytes(conn: Conn, product_id: uuid.UUID) -> Any:
    row = conn.execute("SELECT to_jsonb(p) FROM products p WHERE id = %s", [product_id]).fetchone()
    assert row is not None
    return row[0]


# ---- the read: the case matrix ----


@pytest.mark.endpoint("inbox_groups_list", case="success")
async def test_one_question_on_34_products_is_one_group(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    products = _products(products_world, auth_db, 34)
    tasks = [await _ask(products_world, auth_db, p) for p in products]
    body = await _groups(api, products_world)
    assert body["enabled"] is True and body["view"] == "all"
    group = _group(body, COLOUR_KEY)
    assert (group["count"], group["products"], group["scope"]) == (34, 34, "product_fact")
    assert group["field"] == "color" and group["task_kind"] == "missing_fact"
    assert group["answer"] == COLOUR_SPEC and group["item_kind"] == "task"
    assert {m["task_id"] for m in group["members"]} == {str(t) for t in tasks}
    assert not any(m["has_value"] for m in group["members"])
    assert group["sample_task"]["id"] == group["members"][0]["task_id"]
    assert group["sample_task"]["target_path"] == COLOUR


@pytest.mark.endpoint("inbox_groups_list", case="validation")
async def test_groups_unknown_view_is_422(api: httpx.AsyncClient, products_world: World) -> None:
    response = await api.get(
        GROUPS, params={"view": "everything"}, headers=bearer_for(products_world, OPERATOR)
    )
    assert response.status_code == 422


@pytest.mark.endpoint("inbox_groups_list", case="missing_auth")
async def test_groups_missing_auth_401(api: httpx.AsyncClient, products_world: World) -> None:
    assert (await api.get(GROUPS)).status_code == 401


@pytest.mark.endpoint("inbox_groups_list", case="invalid_token", variant="expired")
@pytest.mark.endpoint("inbox_groups_list", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("inbox_groups_list", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_groups_invalid_token_401(
    api: httpx.AsyncClient, products_world: World, variant: str
) -> None:
    headers = bad_bearer(products_world, variant, OPERATOR)
    assert (await api.get(GROUPS, headers=headers)).status_code == 401


@pytest.mark.endpoint("inbox_groups_list", case="wrong_tenant")
async def test_groups_never_show_another_brand(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    theirs = [await _ask(products_world, auth_db, p, slug="bharat")
              for p in _products(products_world, auth_db, 3, "bharat")]  # fmt: skip
    body = await _groups(api, products_world)
    seen = {m["task_id"] for g in body["groups"] for m in g["members"]}
    assert not seen & {str(t) for t in theirs}
    mine = await _groups(api, products_world, email="operator@bharat.test")
    assert _group(mine, COLOUR_KEY)["count"] == 3


# ---- the read: one count definition (B16) ----


@pytest.mark.parametrize("email", [OPERATOR, "reviewer@acme.test"])
async def test_group_counts_add_up_to_the_home_inbox_counts(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, email: str
) -> None:
    acme = products_world.tenant("acme")
    a, b, c, gone = _products(products_world, auth_db, 4)
    ta = await _ask(products_world, auth_db, a)
    await _ask(products_world, auth_db, b)
    await _ask(products_world, auth_db, c, target=FABRIC, spec=TEXT_SPEC)
    await _ask(products_world, auth_db, gone)
    soft_delete(auth_db, gone)  # a deleted product's question leaves the Inbox
    with _guards_off(auth_db):
        la = f.listing(auth_db, acme, a)
        f.listing_issue(auth_db, acme, la, human_task_id=ta, field_path="attributes.color",
                        error_code="REQUIRED_FIELD_MISSING")  # fmt: skip
        lc = f.listing(auth_db, acme, c)
        f.listing_issue(auth_db, acme, lc)  # an issue no question stands for
        f.listing_issue(auth_db, acme, lc, field_path="content.bullets[0]")
        lb = f.listing(auth_db, acme, b, archived_at=datetime.now(UTC))
        f.listing_issue(auth_db, acme, lb)  # an archived listing's issue leaves the Inbox
    home = await api.get(HOME, headers=bearer_for(products_world, email))
    assert home.status_code == 200, home.text
    inbox = home.json()["inbox"]
    expected = {"all": "open_items", "questions": "questions", "issues": "issues"}
    for view, name in expected.items():
        body = await _groups(api, products_world, view, email)
        assert body["total_items"] == inbox[name], view
        assert sum(g["count"] for g in body["groups"]) == inbox[name], view
    everything = await _groups(api, products_world, "all", email)
    colour = _group(everything, COLOUR_KEY)
    assert colour["count"] == 2  # the issue behind `a`'s question is counted once, as it
    assert colour["blocking"] is True
    issues = await _groups(api, products_world, "issues", email)
    linked = [g for g in issues["groups"] if g["error_code"] == "REQUIRED_FIELD_MISSING"]
    assert len(linked) == 1 and linked[0]["sample_issue"]["human_task_id"] == str(ta)
    titles = [g for g in issues["groups"] if g["error_code"] == "FIELD_TOO_LONG"]
    assert {g["field"] for g in titles} == {"content.title", "content.bullets[]"}


async def test_a_product_with_its_own_value_is_listed_as_kept(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    a, b = _products(products_world, auth_db, 2)
    await _ask(products_world, auth_db, a)
    tb = await _ask(products_world, auth_db, b)
    auth_db.execute(
        "UPDATE products SET attributes = %s WHERE id = %s",
        [Jsonb({"color": f.fact(value="black")}), b],
    )
    group = _group(await _groups(api, products_world), COLOUR_KEY)
    kept = {m["task_id"]: m["has_value"] for m in group["members"]}
    assert kept[str(tb)] is True and list(kept.values()).count(True) == 1


async def test_switched_off(
    api: httpx.AsyncClient,
    products_world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    (product,) = _products(products_world, auth_db, 1)
    task = await _ask(products_world, auth_db, product)
    monkeypatch.setenv("INBOX_GROUPS_ENABLED", "false")
    get_settings.cache_clear()
    body = await _groups(api, products_world)
    assert (body["enabled"], body["groups"], body["total_items"]) == (False, [], 0)
    response = await _answer(api, products_world, [task], "red")
    assert response.status_code == 501
    assert response.json()["error"]["type"] == "NOT_IMPLEMENTED"
    assert _status(auth_db, task) == "open"


# ---- the answer: the case matrix ----


@pytest.mark.endpoint("inbox_groups_answer", case="success")
async def test_answer_for_all_writes_each_product_without_a_value(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    a, b, c, kept = _products(products_world, auth_db, 4)
    tasks = [await _ask(products_world, auth_db, p) for p in (a, b, c, kept)]
    auth_db.execute(
        "UPDATE products SET attributes = %s WHERE id = %s",
        [Jsonb({"color": f.fact(value="black")}), kept],
    )
    before = _row_bytes(auth_db, kept)
    with _guards_off(auth_db):
        created = f.listing(auth_db, acme, a)
        approved = f.listing(auth_db, acme, b, status="approved")
        f.listing(auth_db, acme, c, archived_at=datetime.now(UTC))
    response = await _answer(api, products_world, tasks, "Red")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {x["task_id"] for x in body["applied"]} == {str(t) for t in tasks[:3]}
    assert body["skipped"] == [
        {"task_id": str(tasks[3]), "product_id": str(kept), "reason": "has_value"}
    ]
    for product, task in zip((a, b, c), tasks, strict=False):
        fact = _colour(auth_db, product)
        assert (fact["value"], fact["source"], fact["captured_via"]) == (
            "red", "seller", "human_task",
        )  # fmt: skip
        assert fact["evidence_ref"] == f"human_task:{task}"
        assert _status(auth_db, task) == "applied"
        rows = auth_db.execute(
            "SELECT action, actor_user_id FROM audit_log WHERE entity_id = %s ORDER BY action",
            [product],
        ).fetchall()
        operator = products_world.user(OPERATOR)
        assert [(str(r[0]), r[1]) for r in rows] == [
            ("human_task.answered", operator),
            ("human_task.applied", operator),
        ]
    assert _row_bytes(auth_db, kept) == before and _status(auth_db, tasks[3]) == "open"
    listings = {x["listing_id"]: x for x in body["listings"]}
    assert set(listings) == {str(created), str(approved)}  # the archived listing is left out
    assert listings[str(created)]["redraft"] is False  # never written: generate, not redraft
    assert listings[str(approved)]["redraft"] is True


@pytest.mark.endpoint("inbox_groups_answer", case="validation")
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"group": COLOUR_KEY, "task_ids": [], "value": "red"},
        {"group": COLOUR_KEY, "task_ids": ["not-a-uuid"], "value": "red"},
        {"group": COLOUR_KEY, "task_ids": [str(uuid.UUID(int=1))] * 2, "value": "red"},
        {"group": COLOUR_KEY, "task_ids": [str(uuid.UUID(int=1))], "value": None},
        {"group": COLOUR_KEY, "task_ids": [str(uuid.UUID(int=1))], "value": "red", "x": 1},
        {"group": COLOUR_KEY, "task_ids": [str(uuid.UUID(int=n)) for n in range(101)],
         "value": "red"},
    ],
)  # fmt: skip
async def test_answer_validation_422(
    api: httpx.AsyncClient, products_world: World, body: object
) -> None:
    response = await api.post(ANSWER, headers=_hdrs(products_world), json=body)
    assert response.status_code == 422


@pytest.mark.endpoint("inbox_groups_answer", case="missing_auth")
async def test_answer_missing_auth_401(api: httpx.AsyncClient, products_world: World) -> None:
    body = {"group": COLOUR_KEY, "task_ids": [str(uuid.uuid4())], "value": "red"}
    response = await api.post(ANSWER, headers={"Idempotency-Key": "k-1"}, json=body)
    assert response.status_code == 401


async def _invalid(api: httpx.AsyncClient, world: World, variant: str) -> None:
    headers = {**bad_bearer(world, variant, OPERATOR), "Idempotency-Key": str(uuid.uuid4())}
    body = {"group": COLOUR_KEY, "task_ids": [str(uuid.uuid4())], "value": "red"}
    assert (await api.post(ANSWER, headers=headers, json=body)).status_code == 401


@pytest.mark.endpoint("inbox_groups_answer", case="invalid_token", variant="expired")
async def test_answer_invalid_token_expired(api: httpx.AsyncClient, products_world: World) -> None:
    await _invalid(api, products_world, "expired")


@pytest.mark.endpoint("inbox_groups_answer", case="invalid_token", variant="bad_signature")
async def test_answer_invalid_token_bad_signature(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "bad_signature")


@pytest.mark.endpoint("inbox_groups_answer", case="invalid_token", variant="wrong_typ")
async def test_answer_invalid_token_wrong_typ(
    api: httpx.AsyncClient, products_world: World
) -> None:
    await _invalid(api, products_world, "wrong_typ")


@pytest.mark.endpoint("inbox_groups_answer", case="wrong_role")
@pytest.mark.parametrize("email", ["reviewer@acme.test", "admin@acme.test"])
async def test_answer_wrong_role_403(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn, email: str
) -> None:
    (product,) = _products(products_world, auth_db, 1)
    task = await _ask(products_world, auth_db, product)
    response = await _answer(api, products_world, [task], "red", email=email)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _status(auth_db, task) == "open"


@pytest.mark.endpoint("inbox_groups_answer", case="wrong_tenant")
async def test_answer_another_brands_questions_are_gone(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    (product,) = _products(products_world, auth_db, 1)
    task = await _ask(products_world, auth_db, product)
    unknown = uuid.uuid4()
    response = await _answer(api, products_world, [task, unknown], "red",
                             email="operator@bharat.test")  # fmt: skip
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == [] and body["listings"] == []
    reasons = {x["task_id"]: (x["reason"], x["product_id"]) for x in body["skipped"]}
    # a foreign id and an unknown id answer alike
    assert reasons == {str(task): ("gone", None), str(unknown): ("gone", None)}
    assert _status(auth_db, task) == "open" and _colour(auth_db, product) is None


@pytest.mark.endpoint("inbox_groups_answer", case="conflict")
async def test_answer_idempotency_key(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    a, b = _products(products_world, auth_db, 2)
    tasks = [await _ask(products_world, auth_db, p) for p in (a, b)]
    key = str(uuid.uuid4())
    first = await _answer(api, products_world, tasks, "red", key=key)
    assert first.status_code == 200, first.text
    replay = await _answer(api, products_world, list(reversed(tasks)), "red", key=key)
    assert replay.status_code == 200 and replay.content == first.content
    reused = await _answer(api, products_world, tasks, "black", key=key)
    assert reused.status_code == 409
    assert reused.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    assert _colour(auth_db, a)["value"] == "red"


# ---- the answer: the rest ----


async def test_a_value_no_category_accepts_is_422_at_value(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    tasks = [await _ask(products_world, auth_db, p) for p in _products(products_world, auth_db, 2)]
    response = await _answer(api, products_world, tasks, "chartreuse")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"][0]["loc"][-1] == "value"
    assert all(_status(auth_db, t) == "open" for t in tasks)


async def test_a_question_of_another_group_is_422_at_its_index(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    a, b = _products(products_world, auth_db, 2)
    colour = await _ask(products_world, auth_db, a)
    fabric = await _ask(products_world, auth_db, b, target=FABRIC, spec=TEXT_SPEC)
    response = await _answer(api, products_world, [colour, fabric], "red")
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"][0]["loc"][-2:] == ["task_ids", 1]
    assert _status(auth_db, colour) == "open" and _colour(auth_db, a) is None


async def test_a_brand_or_per_item_group_takes_no_answer(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    (product,) = _products(products_world, auth_db, 1)
    spec = {"kind": "text", "max_length": 256}
    task = await _ask(products_world, auth_db, product, target="facts.manufacturer", spec=spec)
    group = task_key("missing_fact", "facts.manufacturer", spec)
    response = await _answer(api, products_world, [task], "Mills", group=group)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"][0]["loc"][-1] == "group"


async def test_a_closed_question_is_skipped(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    a, b = _products(products_world, auth_db, 2)
    ta, tb = await _ask(products_world, auth_db, a), await _ask(products_world, auth_db, b)
    tenant = products_world.tenant("acme")
    async with unit_of_work(tenant) as session:
        await HumanTaskRepository(session, tenant).cancel_fact_tasks(b, [COLOUR])
    body = (await _answer(api, products_world, [ta, tb], "maroon")).json()
    assert [x["task_id"] for x in body["applied"]] == [str(ta)]
    assert body["skipped"] == [{"task_id": str(tb), "product_id": str(b), "reason": "closed"}]


async def test_a_fact_of_another_level_is_skipped(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    """The size belongs on a size (a variant) or a standalone, never on a style."""
    acme = products_world.tenant("acme")
    style = f.product(auth_db, acme, "style")
    (standalone,) = _products(products_world, auth_db, 1)
    size = "facts.attributes.size"
    ts = await _ask(products_world, auth_db, style, target=size, spec=TEXT_SPEC)
    tp = await _ask(products_world, auth_db, standalone, target=size, spec=TEXT_SPEC)
    group = task_key("missing_fact", size, TEXT_SPEC)
    response = await _answer(api, products_world, [ts, tp], "M", group=group)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [x["task_id"] for x in body["applied"]] == [str(tp)]
    assert body["skipped"] == [
        {"task_id": str(ts), "product_id": str(style), "reason": "wrong_level"}
    ]
    assert _status(auth_db, ts) == "open" and _status(auth_db, tp) == "applied"


async def test_exemption_marks_only_products_without_a_barcode(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    acme = products_world.tenant("acme")
    plain = f.product(auth_db, acme)
    barcoded = f.product(auth_db, acme, **f.barcoded())
    style = f.product(auth_db, acme, "style")
    sized = f.product(auth_db, acme, "child", parent=style, **f.barcoded())
    bare = f.product(auth_db, acme, "child", parent=style)
    tasks = [
        await _ask(products_world, auth_db, p, target=IDENTIFIER, spec=TEXT_SPEC)
        for p in (plain, barcoded, style)
    ]
    group = task_key("missing_fact", IDENTIFIER, TEXT_SPEC)
    read = _group(await _groups(api, products_world), group)
    assert read["scope"] == "exemption"
    kept = {m["task_id"]: m["has_value"] for m in read["members"]}
    assert kept == {str(tasks[0]): False, str(tasks[1]): True, str(tasks[2]): False}
    before = _row_bytes(auth_db, sized)
    refused = await _answer(api, products_world, tasks, "yes", group=group)
    assert refused.status_code == 422  # an exemption is a yes, never a value
    response = await _answer(api, products_world, tasks, True, group=group)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {x["task_id"] for x in body["applied"]} == {str(tasks[0]), str(tasks[2])}
    assert body["skipped"] == [
        {"task_id": str(tasks[1]), "product_id": str(barcoded), "reason": "has_value"}
    ]

    def status(product_id: uuid.UUID) -> Any:
        return auth_db.execute(
            "SELECT identifier_status FROM products WHERE id = %s", [product_id]
        ).fetchone()

    assert status(plain) == ("exempt",) and status(bare) == ("exempt",)
    assert status(barcoded) == ("provided",) and status(style) == (None,)
    assert _row_bytes(auth_db, sized) == before  # a barcoded size is never touched
    audited = auth_db.execute(
        "SELECT count(*) FROM audit_log WHERE entity_id = %s AND action = 'product.fact_updated'",
        [bare],
    ).fetchone()
    assert audited == (1,)
