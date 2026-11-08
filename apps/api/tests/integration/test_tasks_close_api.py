"""SI9 T16: `POST /api/v1/tasks/{task_id}/close` as app_rw through the real app. An account task
(`marketplace_error_review`, `permission_decision`) closes as `done` or `dismissed`: tenant-scoped,
audited on the account, idempotent."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.db.session import unit_of_work
from app.repositories.human_tasks import HumanTaskRepository

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bad_bearer
from .publish_support import insert_account
from .review_support import ADMIN, OPERATOR, REVIEWER

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


def _url(task_id: object) -> str:
    return f"/api/v1/tasks/{task_id}/close"


async def _open(
    world: GenWorld, kind: str = "marketplace_error_review", slug: str = "acme"
) -> uuid.UUID:
    tenant = world.tenant(slug)
    account = insert_account(world, slug)
    async with unit_of_work(tenant) as session:
        task = await HumanTaskRepository(session, tenant).open_account_task(
            kind,
            account,
            "account.listing_error",
            "Review the error Amazon reported for this listing.",
            "Amazon refused the listing for a reason only the seller can check.",
            {"kind": "acknowledge"},
        )
    assert task is not None
    return task


async def _close(
    api: httpx.AsyncClient,
    world: GenWorld,
    task_id: object,
    body: Any,
    *,
    email: str = ADMIN,
    key: str | None = None,
) -> httpx.Response:
    return await api.post(_url(task_id), headers=world.headers(email, key), json=body)


def _task(world: GenWorld, task_id: uuid.UUID) -> dict[str, Any]:
    cur = world.conn.execute("SELECT * FROM human_tasks WHERE id = %s", [task_id])
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


# ---- the case matrix ----


@pytest.mark.endpoint("tasks_close", case="success")
@pytest.mark.parametrize("kind", ["marketplace_error_review", "permission_decision"])
@pytest.mark.parametrize("resolution", ["done", "dismissed"])
async def test_tasks_close_applies_the_resolution(
    api: httpx.AsyncClient, gen_world: GenWorld, kind: str, resolution: str
) -> None:
    task_id = await _open(gen_world, kind)
    response = await _close(api, gen_world, task_id, {"resolution": resolution, "note": "Fixed."})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["resolution"]) == ("applied", resolution)
    assert "answer" not in body and "note" not in body
    row = _task(gen_world, task_id)
    assert row["answer"] == {"resolution": resolution, "note": "Fixed."}
    assert row["answered_by_user_id"] == gen_world.user(ADMIN)
    audits = gen_world.conn.execute(
        "SELECT entity_type, entity_id, reason FROM audit_log WHERE action = 'human_task.closed'"
        " AND entity_id = %s",
        [row["marketplace_account_id"]],
    ).fetchall()
    assert audits == [
        (
            "marketplace_account",
            row["marketplace_account_id"],
            f"account.listing_error {resolution}",
        )
    ]


@pytest.mark.endpoint("tasks_close", case="validation")
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"resolution": "later"},
        {"resolution": "done", "note": ""},
        {"resolution": "done", "x": 1},
    ],
)
async def test_tasks_close_validation_422(
    api: httpx.AsyncClient, gen_world: GenWorld, body: Any
) -> None:
    task_id = await _open(gen_world)
    response = await _close(api, gen_world, task_id, body)
    assert response.status_code == 422, response.text
    assert _task(gen_world, task_id)["status"] == "open"


async def test_tasks_close_other_kinds_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    task_id = await _open(gen_world, "reconnect_account")
    response = await _close(api, gen_world, task_id, {"resolution": "done"})
    assert response.status_code == 422, response.text
    assert _task(gen_world, task_id)["status"] == "open"


@pytest.mark.endpoint("tasks_close", case="missing_auth")
async def test_tasks_close_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await api.post(
        _url(uuid.uuid4()), headers={"Idempotency-Key": "k-1"}, json={"resolution": "done"}
    )
    assert response.status_code == 401


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    headers = {**bad_bearer(world, variant, OPERATOR), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(_url(uuid.uuid4()), headers=headers, json={"resolution": "done"})
    assert response.status_code == 401


@pytest.mark.endpoint("tasks_close", case="invalid_token", variant="expired")
async def test_tasks_close_invalid_token_expired(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("tasks_close", case="invalid_token", variant="bad_signature")
async def test_tasks_close_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("tasks_close", case="invalid_token", variant="wrong_typ")
async def test_tasks_close_invalid_token_wrong_typ(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "wrong_typ")


@pytest.mark.endpoint("tasks_close", case="wrong_role")
@pytest.mark.parametrize("email", [REVIEWER, OPERATOR])
async def test_tasks_close_wrong_role_403(
    api: httpx.AsyncClient, gen_world: GenWorld, email: str
) -> None:
    # an account task is marketplace:connect work (owner, admin), not product:import
    task_id = await _open(gen_world)
    response = await _close(api, gen_world, task_id, {"resolution": "done"}, email=email)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _task(gen_world, task_id)["status"] == "open"


@pytest.mark.endpoint("tasks_close", case="wrong_tenant")
async def test_tasks_close_foreign_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    task_id = await _open(gen_world)
    foreign = await _close(
        api, gen_world, task_id, {"resolution": "done"}, email="owner@bharat.test"
    )
    missing = await _close(
        api, gen_world, uuid.uuid4(), {"resolution": "done"}, email="owner@bharat.test"
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]
    assert _task(gen_world, task_id)["status"] == "open"


@pytest.mark.endpoint("tasks_close", case="not_found")
async def test_tasks_close_not_found_404(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    response = await _close(api, gen_world, uuid.uuid4(), {"resolution": "done"})
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"


@pytest.mark.endpoint("tasks_close", case="conflict")
async def test_tasks_close_a_different_close_409(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    task_id = await _open(gen_world)
    assert (await _close(api, gen_world, task_id, {"resolution": "done"})).status_code == 200
    again = await _close(api, gen_world, task_id, {"resolution": "dismissed"})
    assert again.status_code == 409
    assert again.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    key = str(uuid.uuid4())
    first = await _close(api, gen_world, task_id, {"resolution": "done"}, key=key)
    reused = await _close(api, gen_world, task_id, {"resolution": "dismissed"}, key=key)
    assert reused.status_code == 409
    assert reused.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    assert first.status_code == 200


async def test_tasks_close_same_close_again_is_200(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    task_id = await _open(gen_world)
    first = await _close(api, gen_world, task_id, {"resolution": "dismissed"})
    again = await _close(api, gen_world, task_id, {"resolution": "dismissed"})
    assert first.status_code == again.status_code == 200
    assert again.json()["resolution"] == "dismissed"
    closed = gen_world.count(
        "audit_log",
        "action = 'human_task.closed' AND entity_id = %s",
        [_task(gen_world, task_id)["marketplace_account_id"]],
    )
    assert closed == 1
