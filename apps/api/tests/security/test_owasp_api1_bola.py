"""S26 T2 (AC 2, OWASP API1): broken object-level authorisation, across the whole surface.

bharat's owner (the role with the widest grants) sends every op that names a row, with acme's
row in the path or the body. Every answer is a 404 `NOT_FOUND` (an id of another tenant looks
exactly like an unknown id), or for a batch op an item 404, and no answer holds any acme id or
canary. Every list op walked to its last page as bharat holds no acme row. acme's rows are
unchanged afterwards (row fingerprints, not only counts).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.api.v1.surface import OPS, Operation

from .conftest import World2, call_for, ids_of, ops, send, tenant_fingerprint

FOREIGN_PATH_OPS = ops(path_id=True)
FOREIGN_BODY_OPS = [op for op in OPS.values() if op.body_ids]
# Tenant lists only: a platform-admin list (ADR-0019) spans every applicant by design, and its
# admin-only access is covered by tests/integration/test_signup_api.py.
LIST_OPS = [
    op
    for op in OPS.values()
    if op.method == "GET" and "cursor" in op.query and not op.platform_admin
]
BATCH_OPS = {"listings_approve", "listings_publish"}
# A role of bharat allowed to call the op (the owner holds every action but none of these).
OWNER = "owner@bharat.test"


def _assert_no_acme(world: World2, response: httpx.Response, op_id: str, sent: Any = None) -> None:
    """No acme id or canary in the answer, except an id the caller itself sent (a batch item
    echoes its `listing_id`)."""
    text = response.text
    echoed = repr(vars(sent)) if sent is not None else ""
    for value in world.foreign_values("acme"):
        if value in echoed:
            continue
        assert value not in text, (op_id, value, text[:400])


def _assert_not_found(response: httpx.Response, op: Operation) -> None:
    body = response.json()
    if op.op_id in BATCH_OPS and response.status_code in (200, 202):
        items = body["items"]
        assert items and all(i["http_status"] == 404 for i in items), (op.op_id, body)
        assert all(i["error"]["error_code"] == "NOT_FOUND" for i in items), (op.op_id, body)
        return
    assert response.status_code == 404, (op.op_id, response.status_code, response.text)
    assert body["error"]["type"] == "NOT_FOUND", (op.op_id, body)


@pytest.mark.parametrize("op", FOREIGN_PATH_OPS, ids=ids_of(FOREIGN_PATH_OPS))
async def test_api1_foreign_id_404(api: httpx.AsyncClient, world2: World2, op: Operation) -> None:
    conn = world2.base.conn
    acme = world2.side("acme").tenant_id
    before = tenant_fingerprint(conn, acme)
    call = call_for(op, world2, "acme")
    response = await send(api, call, world2.headers(OWNER))
    _assert_not_found(response, op)
    _assert_no_acme(world2, response, op.op_id, call)
    assert tenant_fingerprint(conn, acme) == before, op.op_id


@pytest.mark.parametrize("op", FOREIGN_BODY_OPS, ids=ids_of(FOREIGN_BODY_OPS))
async def test_api1_body_id_foreign_404(
    api: httpx.AsyncClient, world2: World2, op: Operation
) -> None:
    """acme's id inside a bharat body (product_id, listing_id, items[].listing_id, the switch
    target, an acme storage key or image key)."""
    conn = world2.base.conn
    acme = world2.side("acme").tenant_id
    before = tenant_fingerprint(conn, acme)
    call = call_for(op, world2, "acme")
    if op.path_id:  # the path names bharat's own row; only the body is foreign
        call.url = call_for(op, world2, "bharat").url
    if op.op_id == "products_create":
        call.json = {**(call.json or {}), "images": [_acme_key(world2)]}
    if op.op_id == "images_upload":
        call.files = None
        call.data = {
            "product_id": str(world2.side("bharat").ids["product_id"]),
            "slot": "main",
            "storage_key": _acme_key(world2),
        }
    response = await send(api, call, world2.headers(OWNER))
    if op.op_id in {"products_create", "images_upload"}:
        # a foreign storage key is refused before any row is written
        assert response.status_code in (404, 422), (op.op_id, response.text)
    else:
        _assert_not_found(response, op)
    _assert_no_acme(world2, response, op.op_id, call)
    assert tenant_fingerprint(conn, acme) == before, op.op_id


def _acme_key(world: World2) -> str:
    row = world.base.conn.execute(
        "SELECT storage_key FROM images WHERE id = %s", [world.side("acme").ids["image_id"]]
    ).fetchone()
    assert row is not None
    return str(row[0])


async def _walk(
    api: httpx.AsyncClient, op: Operation, call: Any, headers: dict[str, str]
) -> list[httpx.Response]:
    """Every page of a list op at `limit=1` (so a foreign row on a later page is seen too)."""
    pages: list[httpx.Response] = []
    params = {**call.params, "limit": "1"}
    while len(pages) < 50:
        response = await api.get(call.url, params=params, headers={**call.headers, **headers})
        assert response.status_code == 200, (op.op_id, response.text)
        pages.append(response)
        cursor = response.json().get("next_cursor")
        if not cursor:
            break
        params["cursor"] = cursor
    return pages


@pytest.mark.parametrize("op", LIST_OPS, ids=ids_of(LIST_OPS))
async def test_api1_lists_hold_no_foreign_rows(
    api: httpx.AsyncClient, world2: World2, op: Operation
) -> None:
    call = call_for(op, world2, "bharat")
    pages = await _walk(api, op, call, world2.headers(OWNER))
    assert pages
    for response in pages:
        _assert_no_acme(world2, response, op.op_id)


async def test_api1_job_and_import_poll_urls_foreign_404(
    api: httpx.AsyncClient, world2: World2
) -> None:
    acme = world2.side("acme").ids
    for path in (
        f"/api/v1/jobs/{acme['job_id']}",
        f"/api/v1/imports/{acme['import_id']}",
        f"/api/v1/imports/{acme['import_id']}/rows",
    ):
        for role in ("owner", "operator", "reviewer", "admin"):
            response = await api.get(path, headers=world2.headers(f"{role}@bharat.test"))
            assert response.status_code == 404, (path, role, response.text)
            assert response.json()["error"]["type"] == "NOT_FOUND"


async def test_api1_no_side_effect_on_foreign(api: httpx.AsyncClient, world2: World2) -> None:
    """Every write op aimed at acme's rows, in one pass: acme's rows byte-for-byte unchanged."""
    conn = world2.base.conn
    acme = world2.side("acme").tenant_id
    before = tenant_fingerprint(conn, acme)
    from tests.support.rate_limits import reset_rate_limits

    for op in [o for o in OPS.values() if o.method != "GET" and (o.path_id or o.body_ids)]:
        reset_rate_limits(conn)
        await send(api, call_for(op, world2, "acme"), world2.headers(OWNER))
    assert tenant_fingerprint(conn, acme) == before
