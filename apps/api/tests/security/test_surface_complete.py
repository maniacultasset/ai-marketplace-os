"""S26 T1 (AC 1): the whole `/api/v1` surface is implemented, and the security world names a row
for every path and body id the registry declares, so no later suite skips an op."""

from __future__ import annotations

from typing import Any

from app.api.v1.surface import OPS, is_stub
from fastapi.routing import iter_route_contexts

from .conftest import SLUGS, World2, call_for, resolve_id, send, tenant_counts


def _v1_routes() -> list[Any]:
    """Every API route (through the include_router mounts) with an operation id."""
    from app.main import create_app

    routes = iter_route_contexts(list(create_app().routes))
    return [r for r in routes if getattr(r, "operation_id", None)]


def test_v1_surface_has_no_stubs(auth_db: Any) -> None:
    stubs = sorted(str(route.operation_id) for route in _v1_routes() if is_stub(route.endpoint))
    assert stubs == [], f"stub handlers left on the surface: {stubs}"
    mounted = {route.operation_id for route in _v1_routes()}
    assert set(OPS) <= mounted


def test_world2_has_every_path_id(world2: World2) -> None:
    for op in OPS.values():
        names = [n for n in (op.path_id, *op.body_ids) if n]
        for name in names:
            for slug in SLUGS:
                assert resolve_id(world2, slug, name), (op.op_id, name, slug)
    # the two sides never share an id
    acme = set(world2.foreign_values("acme"))
    assert not acme & set(world2.foreign_values("bharat"))


def test_bharat_counts_stable_helper(world2: World2) -> None:
    conn = world2.base.conn
    bharat = world2.side("bharat").tenant_id
    before = tenant_counts(conn, bharat)
    assert before["products"] == 1 and before["listings"] == 1
    assert before["tenant_memberships"] == 4
    conn.execute(
        "INSERT INTO idempotency_keys (tenant_id, key, endpoint, request_hash, user_id, expires_at)"
        " VALUES (%s, 's26-probe', 'POST /x', sha256('x'), %s, now())",
        [bharat, world2.base.user("owner@bharat.test")],
    )
    after = tenant_counts(conn, bharat)
    assert after["idempotency_keys"] == before["idempotency_keys"] + 1
    assert {k: v for k, v in after.items() if k != "idempotency_keys"} == {
        k: v for k, v in before.items() if k != "idempotency_keys"
    }


async def test_world2_task_is_answerable_in_its_own_tenant(api: Any, world2: World2) -> None:
    """SI3 gate fix: the seeded question takes the registry's valid answer in its own tenant, so
    a 404 from the other tenant (API1) is the tenant scope, never a task the world got wrong."""
    op = OPS["tasks_answer"]
    response = await send(api, call_for(op, world2, "acme"), world2.caller_headers(op))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(world2.side("acme").ids["task_id"])
    assert body["status"] in {"answered", "applied"}
