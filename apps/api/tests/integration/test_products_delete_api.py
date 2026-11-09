"""PD1: `DELETE /api/v1/products/{product_id}` and `POST /api/v1/products/bulk-delete` as app_rw
through the real app. A soft delete that cancels the product's queued work and open questions,
archives its listings and hides it everywhere; refused, with nothing changed, while a listing is
or was with a marketplace."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .family_support import family
from .generation_support import GenWorld, gen_world, make_generating, set_status
from .products_support import bad_bearer
from .review_support import ADMIN, OPERATOR, OWNER, REVIEWER, generated, new_product, open_issue

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
PREFIX = "/api/v1/products"
BULK = f"{PREFIX}/bulk-delete"


async def _delete(
    api: httpx.AsyncClient, world: GenWorld, product_id: object, email: str = OWNER
) -> httpx.Response:
    return await api.delete(f"{PREFIX}/{product_id}", headers=world.headers(email))


async def _bulk(
    api: httpx.AsyncClient,
    world: GenWorld,
    ids: list[Any],
    email: str = OWNER,
    key: str | None = None,
) -> httpx.Response:
    body = {"product_ids": [str(i) for i in ids]}
    return await api.post(BULK, headers=world.headers(email, key=key), json=body)


async def _made(world: GenWorld) -> tuple[uuid.UUID, uuid.UUID]:
    made = await generated(world)
    product = world.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id])
    return uuid.UUID(str(product)), made.listing_id


def _deleted(world: GenWorld, product_id: uuid.UUID) -> bool:
    return world.scalar("SELECT deleted_at FROM products WHERE id = %s", [product_id]) is not None


def _task(world: GenWorld, product_id: uuid.UUID) -> uuid.UUID:
    return f.human_task(world.conn, world.tenant("acme"), product_id=product_id)


# ---- DELETE: the case matrix ----


@pytest.mark.endpoint("products_delete", case="success")
async def test_delete_hides_the_product_everywhere(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    from .review_support import Made

    made = await generated(gen_world)
    product = uuid.UUID(
        str(gen_world.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id]))
    )
    task = _task(gen_world, product)
    issue = await open_issue(gen_world, Made(made.listing_id, made.version_id, made.job_id))
    image = f.image(gen_world.conn, gen_world.tenant("acme"), product, slot="secondary")

    response = await _delete(api, gen_world, product)
    assert response.status_code == 204, response.text
    assert response.content == b""

    assert _deleted(gen_world, product)
    row = gen_world.listing(made.listing_id)
    assert row["archived_at"] is not None and row["last_event"] == "archive"
    assert row["archived_by_user_id"] == gen_world.user(OWNER)
    assert row["status"] == "validated"  # the status is kept; the overlay freezes it
    assert gen_world.scalar(
        "SELECT status || '/' || cancelled_reason FROM human_tasks WHERE id = %s", [task]
    ) == ("cancelled/product_deleted")
    assert gen_world.scalar("SELECT status FROM listing_issues WHERE id = %s", [issue]) == "open"
    assert gen_world.scalar("SELECT deleted_at FROM images WHERE id = %s", [image]) is not None

    headers = gen_world.headers(OWNER)
    assert (await api.get(f"{PREFIX}/{product}", headers=headers)).status_code == 404
    listed = (await api.get(PREFIX, headers=headers, params={"limit": 200})).json()["data"]
    assert str(product) not in {p["id"] for p in listed}
    listings = (await api.get("/api/v1/listings", headers=headers)).json()["data"]
    assert str(made.listing_id) not in {item["id"] for item in listings}
    issues = (await api.get("/api/v1/issues", headers=headers)).json()["data"]
    assert str(issue) not in {i["id"] for i in issues}
    for status in ("open", "cancelled"):
        tasks = await api.get("/api/v1/tasks", headers=headers, params={"status": status})
        assert str(task) not in {t["id"] for t in tasks.json()["data"]}

    audit = gen_world.conn.execute(
        "SELECT actor_user_id, reason FROM audit_log WHERE entity_id = %s AND action = %s",
        [product, "product.deleted"],
    ).fetchall()
    assert len(audit) == 1 and audit[0][0] == gen_world.user(OWNER)
    assert "tasks:1;issues:1;" in audit[0][1] and str(made.listing_id) in audit[0][1]
    assert (
        gen_world.count(
            "audit_log", "entity_id = %s AND action = 'listing.archived'", [made.listing_id]
        )
        == 1
    )


@pytest.mark.endpoint("products_delete", case="validation")
async def test_delete_bad_id_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await _delete(api, gen_world, "not-a-uuid")).status_code == 422


@pytest.mark.endpoint("products_delete", case="missing_auth")
async def test_delete_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.delete(f"{PREFIX}/{uuid.uuid4()}")).status_code == 401


@pytest.mark.endpoint("products_delete", case="invalid_token", variant="expired")
@pytest.mark.endpoint("products_delete", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("products_delete", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_delete_invalid_token_401(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    response = await api.delete(
        f"{PREFIX}/{uuid.uuid4()}", headers=bad_bearer(gen_world, variant, OWNER)
    )
    assert response.status_code == 401


@pytest.mark.endpoint("products_delete", case="wrong_role")
@pytest.mark.parametrize("email", [OPERATOR, REVIEWER])
async def test_delete_operator_and_reviewer_403(
    api: httpx.AsyncClient, gen_world: GenWorld, email: str
) -> None:
    product, _ = await _made(gen_world)
    response = await _delete(api, gen_world, product, email)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert not _deleted(gen_world, product)


@pytest.mark.endpoint("products_delete", case="wrong_tenant")
async def test_delete_foreign_product_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    product, listing_id = await _made(gen_world)
    foreign = await _delete(api, gen_world, product, "owner@bharat.test")
    missing = await _delete(api, gen_world, uuid.uuid4(), "owner@bharat.test")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert not _deleted(gen_world, product)
    assert gen_world.listing(listing_id)["archived_at"] is None


@pytest.mark.endpoint("products_delete", case="not_found")
async def test_delete_unknown_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _delete(api, gen_world, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


@pytest.mark.endpoint("products_delete", case="conflict")
@pytest.mark.parametrize(
    ("status", "code"),
    [("published", "INVALID_STATUS_TRANSITION"), ("publishing", "SUBMISSION_IN_FLIGHT")],
)
async def test_delete_refused_while_a_listing_is_with_a_marketplace(
    api: httpx.AsyncClient, gen_world: GenWorld, status: str, code: str
) -> None:
    product, listing_id = await _made(gen_world)
    task = _task(gen_world, product)
    set_status(gen_world, listing_id, status)
    response = await _delete(api, gen_world, product)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == code
    assert not _deleted(gen_world, product)
    assert gen_world.listing(listing_id)["archived_at"] is None
    assert gen_world.scalar("SELECT status FROM human_tasks WHERE id = %s", [task]) == "open"


# ---- DELETE: the rest ----


async def test_delete_admin_may_delete(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    product, _ = await _made(gen_world)
    assert (await _delete(api, gen_world, product, ADMIN)).status_code == 204


async def test_second_delete_is_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    product, _ = await _made(gen_world)
    assert (await _delete(api, gen_world, product)).status_code == 204
    again = await _delete(api, gen_world, product)
    assert again.status_code == 404
    assert again.json()["error"]["type"] == "NOT_FOUND"
    assert (
        gen_world.count("audit_log", "entity_id = %s AND action = 'product.deleted'", [product])
        == 1
    )


async def test_delete_refused_after_an_earlier_submission(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """A listing back to `failed` that the marketplace once saw stays: never live is not enough."""
    product, listing_id = await _made(gen_world)
    with gen_world.conn.transaction():
        gen_world.conn.execute("SET LOCAL session_replication_role = replica")
        gen_world.conn.execute(
            "UPDATE listings SET marketplace_state = 'rejected', status = 'failed', "
            "status_reason = 'marketplace_rejected' WHERE id = %s",
            [listing_id],
        )
    response = await _delete(api, gen_world, product)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    assert not _deleted(gen_world, product)


async def test_delete_cancels_a_queued_generation(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    sku = await new_product(gen_world)
    accepted = await make_generating(gen_world, email=OPERATOR, sku=sku)
    product = gen_world.product("acme", sku)
    assert gen_world.listing(accepted.listing_id)["status"] == "generating"
    response = await _delete(api, gen_world, product)
    assert response.status_code == 204, response.text
    assert gen_world.scalar("SELECT status FROM job_runs WHERE id = %s", [accepted.job_id]) == (
        "cancelled"
    )
    row = gen_world.listing(accepted.listing_id)
    assert row["status"] == "failed" and row["archived_at"] is not None
    assert _deleted(gen_world, product)


async def test_delete_refused_while_a_generation_runs(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    sku = await new_product(gen_world)
    accepted = await make_generating(gen_world, email=OPERATOR, sku=sku)
    product = gen_world.product("acme", sku)
    with gen_world.conn.transaction():
        gen_world.conn.execute("SET LOCAL session_replication_role = replica")
        gen_world.conn.execute(
            "UPDATE job_runs SET status = 'running', attempt = 1, started_at = now(), "
            "lease_expires_at = now() + interval '10 minutes' WHERE id = %s",
            [accepted.job_id],
        )
    response = await _delete(api, gen_world, product)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "LISTING_BUSY"
    assert not _deleted(gen_world, product)
    assert gen_world.scalar("SELECT status FROM job_runs WHERE id = %s", [accepted.job_id]) == (
        "running"
    )


async def test_delete_refused_while_a_listing_is_locked(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    import asyncio

    product, listing_id = await _made(gen_world)
    with gen_world.conn.transaction():
        gen_world.conn.execute("SELECT 1 FROM listings WHERE id = %s FOR UPDATE", [listing_id])
        response = await asyncio.wait_for(_delete(api, gen_world, product), 10)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "LISTING_BUSY"
    assert not _deleted(gen_world, product)


async def test_delete_a_style_takes_its_sizes(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    tenant = gen_world.tenant("acme")
    style, children = family(gen_world.conn, tenant, "PD1-FAM")
    child = next(iter(children.values()))
    refused = await _delete(api, gen_world, child)
    assert refused.status_code == 422
    assert not _deleted(gen_world, child)

    assert (await _delete(api, gen_world, style)).status_code == 204
    assert _deleted(gen_world, style)
    assert all(_deleted(gen_world, c) for c in children.values())
    assert (
        gen_world.count(
            "variant_matrix_cells",
            "style_product_id = %s AND availability = 'available'",
            [style],
        )
        == 0
    )


async def test_delete_cancels_a_queued_photo_check(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    tenant = gen_world.tenant("acme")
    product = f.product(gen_world.conn, tenant, category="kurtas")
    image = f.image(gen_world.conn, tenant, product, slot="main")
    job = f.insert(
        gen_world.conn,
        "job_runs",
        tenant_id=tenant,
        kind="image_compliance",
        entity_type="image",
        entity_id=image,
        max_attempts=1,
        payload_json={},
        correlation_id=uuid.uuid4(),
    )
    response = await _delete(api, gen_world, product)
    assert response.status_code == 204, response.text
    assert gen_world.scalar("SELECT status FROM job_runs WHERE id = %s", [job]) == "cancelled"
    assert gen_world.scalar("SELECT deleted_at FROM images WHERE id = %s", [image]) is not None


# ---- bulk: the case matrix ----


@pytest.mark.endpoint("products_bulk_delete", case="success")
async def test_bulk_answers_each_product(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    ok, _ = await _made(gen_world)
    sent, sent_listing = await _made(gen_world)
    set_status(gen_world, sent_listing, "published")
    unknown = uuid.uuid4()
    response = await _bulk(api, gen_world, [ok, sent, unknown])
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [(i["product_id"], i["http_status"]) for i in items] == [
        (str(ok), 204),
        (str(sent), 409),
        (str(unknown), 404),
    ]
    assert items[0]["error"] is None
    assert items[1]["error"]["error_code"] == "INVALID_STATUS_TRANSITION"
    assert items[1]["error"]["errors"][0]["field"] == "request"
    assert _deleted(gen_world, ok) and not _deleted(gen_world, sent)
    assert gen_world.listing(sent_listing)["archived_at"] is None


@pytest.mark.endpoint("products_bulk_delete", case="validation")
@pytest.mark.parametrize(
    "body",
    [{}, {"product_ids": []}, {"product_ids": ["x"]}, {"product_ids": [str(uuid.UUID(int=1))] * 2}],
)
async def test_bulk_validation_422(
    api: httpx.AsyncClient, gen_world: GenWorld, body: dict[str, Any]
) -> None:
    response = await api.post(BULK, headers=gen_world.headers(OWNER), json=body)
    assert response.status_code == 422, response.text


async def test_bulk_over_100_is_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _bulk(api, gen_world, [uuid.uuid4() for _ in range(101)])
    assert response.status_code == 422


@pytest.mark.endpoint("products_bulk_delete", case="missing_auth")
async def test_bulk_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.post(
        BULK, json={"product_ids": [str(uuid.uuid4())]}, headers={"Idempotency-Key": "k-1"}
    )
    assert response.status_code == 401


@pytest.mark.endpoint("products_bulk_delete", case="invalid_token", variant="expired")
@pytest.mark.endpoint("products_bulk_delete", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("products_bulk_delete", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_bulk_invalid_token_401(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    headers = {**bad_bearer(gen_world, variant, OWNER), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(BULK, headers=headers, json={"product_ids": [str(uuid.uuid4())]})
    assert response.status_code == 401


@pytest.mark.endpoint("products_bulk_delete", case="wrong_role")
async def test_bulk_operator_403(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    product, _ = await _made(gen_world)
    response = await _bulk(api, gen_world, [product], OPERATOR)
    assert response.status_code == 403
    assert not _deleted(gen_world, product)


@pytest.mark.endpoint("products_bulk_delete", case="wrong_tenant")
async def test_bulk_foreign_item_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    product, _ = await _made(gen_world)
    response = await _bulk(api, gen_world, [product], "owner@bharat.test")
    assert response.status_code == 200
    [item] = response.json()["items"]
    assert (item["http_status"], item["error"]["error_code"]) == (404, "NOT_FOUND")
    assert not _deleted(gen_world, product)


@pytest.mark.endpoint("products_bulk_delete", case="not_found")
async def test_bulk_unknown_item_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _bulk(api, gen_world, [uuid.uuid4()])
    [item] = response.json()["items"]
    assert (item["http_status"], item["error"]["error_code"]) == (404, "NOT_FOUND")


@pytest.mark.endpoint("products_bulk_delete", case="conflict")
async def test_bulk_key_reuse_409_and_replay(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    a, _ = await _made(gen_world)
    b, _ = await _made(gen_world)
    key = str(uuid.uuid4())
    first = await _bulk(api, gen_world, [a], key=key)
    replay = await _bulk(api, gen_world, [a], key=key)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()  # the stored answer, not a second-delete 404
    other = await _bulk(api, gen_world, [b], key=key)
    assert other.status_code == 409
    assert other.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    assert not _deleted(gen_world, b)
