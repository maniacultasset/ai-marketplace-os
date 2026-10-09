"""S19 T21 (AC 14, 15): `GET /api/v1/tasks`, the human task inbox, and its case matrix."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bad_bearer, bearer_for

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
URL = "/api/v1/tasks"


def _task(world: GenWorld, slug: str = "acme", status: str = "open", n: int = 0) -> uuid.UUID:
    """A `missing_fact` task (admin login); an answered one carries an answer and evidence."""
    product = world.product(slug, "KURTA-002")
    task_id = uuid.uuid4()
    answered = status == "answered"
    world.conn.execute(
        "INSERT INTO human_tasks (id, tenant_id, kind, product_id, target_path, question, reason, "
        "expected_answer, evidence, status, answer, answered_by_user_id, answered_at, "
        "resume_from, created_at) VALUES (%s, %s, 'missing_fact', %s, %s, "
        "'What is the fabric?', 'The listing needs the fabric.', '{\"kind\": \"text\"}', %s, %s, "
        "%s, %s, %s, 'generate', now() + make_interval(secs => %s))",
        [
            task_id,
            world.tenant(slug),
            product,
            f"facts.fabric_{n}",
            json.dumps([{"kind": "source_value", "text": "call +91 98xxxx"}]),
            status,
            json.dumps("cotton") if answered else None,
            world.user(f"owner@{slug}.test") if answered else None,
            "2026-10-01T09:00:00Z" if answered else None,
            n,
        ],
    )
    return task_id


async def _get(
    api: httpx.AsyncClient, world: GenWorld, email: str = "reviewer@acme.test", **params: Any
) -> httpx.Response:
    return await api.get(URL, headers=bearer_for(world, email), params=params)


@pytest.mark.endpoint("tasks_list", case="success")
async def test_tasks_list_open_default(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    open_one = _task(gen_world, n=0)
    _task(gen_world, status="answered", n=1)
    response = await _get(api, gen_world)
    assert response.status_code == 200, response.text
    assert [r["id"] for r in response.json()["data"]] == [str(open_one)]
    answered = (await _get(api, gen_world, status="answered")).json()["data"]
    assert len(answered) == 1 and answered[0]["status"] == "answered"


async def test_tasks_list_keyset(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    ids = [_task(gen_world, n=n) for n in range(3)]
    first = (await _get(api, gen_world, limit=2)).json()
    assert [r["id"] for r in first["data"]] == [str(ids[2]), str(ids[1])]
    second = (await _get(api, gen_world, limit=2, cursor=first["page"]["next_cursor"])).json()
    assert [r["id"] for r in second["data"]] == [str(ids[0])]


async def test_tasks_list_no_evidence_no_answer(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    _task(gen_world, status="answered")
    response = await _get(api, gen_world, status="answered")
    row = response.json()["data"][0]
    assert "evidence" not in row and "answer" not in row
    assert "98xxxx" not in response.text and "cotton" not in response.text


@pytest.mark.endpoint("tasks_list", case="wrong_tenant")
async def test_tasks_list_bharat_none(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    _task(gen_world)
    assert (await _get(api, gen_world, "reviewer@bharat.test")).json()["data"] == []


@pytest.mark.parametrize("role", ("owner", "operator", "reviewer", "admin"))
async def test_tasks_list_all_roles_200(
    api: httpx.AsyncClient, gen_world: GenWorld, role: str
) -> None:
    assert (await _get(api, gen_world, f"{role}@acme.test")).status_code == 200


@pytest.mark.endpoint("tasks_list", case="validation")
@pytest.mark.parametrize("params", [{"status": "done"}, {"limit": 0}, {"cursor": "!!"}])
async def test_tasks_list_validation_422(
    api: httpx.AsyncClient, gen_world: GenWorld, params: dict[str, Any]
) -> None:
    assert (await _get(api, gen_world, **params)).status_code == 422


@pytest.mark.endpoint("tasks_list", case="missing_auth")
async def test_tasks_list_missing_auth_401(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.get(URL)).status_code == 401


async def _invalid(api: httpx.AsyncClient, world: GenWorld, variant: str) -> None:
    assert (await api.get(URL, headers=bad_bearer(world, variant))).status_code == 401


@pytest.mark.endpoint("tasks_list", case="invalid_token", variant="expired")
async def test_tasks_list_invalid_token_expired(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "expired")


@pytest.mark.endpoint("tasks_list", case="invalid_token", variant="bad_signature")
async def test_tasks_list_invalid_token_bad_signature(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "bad_signature")


@pytest.mark.endpoint("tasks_list", case="invalid_token", variant="wrong_typ")
async def test_tasks_list_invalid_token_wrong_typ(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    await _invalid(api, gen_world, "wrong_typ")
