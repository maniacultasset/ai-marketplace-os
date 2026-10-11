"""S26 T5 (AC 2, OWASP API4): unrestricted resource consumption, across the surface.

Every op with a body refuses 10 MiB + 1 (declared and streamed) with the 413 envelope; every
list op refuses `limit=201` and a 513-character cursor; the tenant and IP limiters answer 429
with an integer `Retry-After` in 1..60 (C3); a 10,001-row CSV and a third live import are refused;
regeneration instructions over 2,000 characters are refused.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from app.api.v1.surface import OPS, Operation

from .conftest import World2, call_for, ids_of, send

CAP = 10 * 1024 * 1024
BODY_OPS = [op for op in OPS.values() if op.has_body]
LIST_OPS = [op for op in OPS.values() if op.method == "GET" and "limit" in op.query]


def _headers(world: World2, op: Operation) -> dict[str, str]:
    return world.caller_headers(op)


FORM_HEAD = (
    b'--s26cap\r\nContent-Disposition: form-data; name="file"; filename="x.csv"\r\n'
    b"Content-Type: text/csv\r\n\r\n"
)


async def _stream(total: int, head: bytes = b"") -> AsyncIterator[bytes]:
    """`head`, then filler: `total` bytes in all (a multipart file part for a form op)."""
    if head:
        yield head
    chunk = b"x" * 65536
    sent = len(head)
    while sent < total:
        part = chunk[: min(len(chunk), total - sent)]
        sent += len(part)
        yield part


@pytest.mark.parametrize("op", BODY_OPS, ids=ids_of(BODY_OPS))
async def test_api4_body_over_cap_413(
    api: httpx.AsyncClient, world2: World2, op: Operation
) -> None:
    call = call_for(op, world2, "acme")
    # the body is parsed (so a stream is read up to the cap) only under the op's own type
    kind = "multipart/form-data; boundary=s26cap" if call.json is None else "application/json"
    headers = {**call.headers, **_headers(world2, op), "Content-Type": kind}
    head = FORM_HEAD if call.json is None else b""
    body = head + b"x" * (CAP + 1 - len(head))
    declared = await api.request(call.method, call.url, headers=headers, content=body)
    streamed = await api.request(
        call.method, call.url, headers=headers, content=_stream(CAP + 1, head)
    )
    for response in (declared, streamed):
        assert response.status_code == 413, (op.op_id, response.text[:200])
        assert response.json()["error"]["type"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.parametrize("op", LIST_OPS, ids=ids_of(LIST_OPS))
async def test_api4_limit_201_422(api: httpx.AsyncClient, world2: World2, op: Operation) -> None:
    call = call_for(op, world2, "acme")
    for params in ({"limit": "201"}, {"cursor": "c" * 513}):
        response = await send(
            api,
            type(call)(call.method, call.url, call.headers, {**call.params, **params}),
            _headers(world2, op),
        )
        assert response.status_code == 422, (op.op_id, params, response.text)
        assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    ok = await send(
        api,
        type(call)(call.method, call.url, call.headers, {**call.params, "limit": "200"}),
        _headers(world2, op),
    )
    assert ok.status_code == 200, (op.op_id, ok.text)


async def test_api4_cursor_513_chars_422(api: httpx.AsyncClient, world2: World2) -> None:
    response = await api.get(
        "/api/v1/products", params={"cursor": "c" * 513}, headers=world2.headers("owner@acme.test")
    )
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


def _pin_clock(monkeypatch: pytest.MonkeyPatch, second: int) -> None:
    from app.services import rate_limit

    moment = datetime(2026, 10, 6, 12, 0, second, tzinfo=UTC)
    monkeypatch.setattr(rate_limit, "now", lambda: moment)


async def test_api4_121st_tenant_request_429(
    api: httpx.AsyncClient, world2: World2, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_clock(monkeypatch, 15)
    headers = world2.headers("reviewer@acme.test")
    statuses = [(await api.get("/api/v1/tasks", headers=headers)).status_code for _ in range(120)]
    assert set(statuses) == {200}
    refused = await api.get("/api/v1/tasks", headers=headers)
    assert refused.status_code == 429
    assert refused.json()["error"]["type"] == "RATE_LIMITED"
    assert refused.headers["retry-after"] == "45"
    # bharat's budget is its own
    other = await api.get("/api/v1/tasks", headers=world2.headers("reviewer@bharat.test"))
    assert other.status_code == 200


async def test_api4_61st_ip_request_429_public(
    api: httpx.AsyncClient, world2: World2, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_clock(monkeypatch, 59)
    body = {"refresh_token": "s26-not-a-token"}
    statuses = [(await api.post("/api/v1/auth/refresh", json=body)).status_code for _ in range(60)]
    assert set(statuses) == {401}, statuses
    refused = await api.post("/api/v1/auth/refresh", json=body)
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1"


async def test_api4_csv_10001_rows_refused(api: httpx.AsyncClient, world2: World2) -> None:
    rows = "".join(f"S26-{n},Kurta,kurtas\n" for n in range(10_001))
    data = ("seller_sku,product_name,category\n" + rows).encode()
    response = await api.post(
        "/api/v1/products/bulk",
        headers={**world2.headers("operator@acme.test"), "Idempotency-Key": str(uuid.uuid4())},
        files={"file": ("products.csv", data, "text/csv")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "IMPORT_FAILED"
    assert "10000" in response.text
    count = world2.base.conn.execute(
        "SELECT count(*) FROM imports WHERE tenant_id = %s AND filename = 'products.csv'",
        [world2.side("acme").tenant_id],
    ).fetchone()
    assert count is not None and count[0] == 0


async def test_api4_third_live_import_429(api: httpx.AsyncClient, world2: World2) -> None:
    import tests.integration.ops_support as ops_support

    conn = world2.base.conn
    acme = world2.side("acme").tenant_id
    # world2 holds one queued import job already; one more makes the cap of two live imports
    ops_support.job_run(conn, acme)
    live = conn.execute(
        "SELECT count(*) FROM job_runs WHERE tenant_id = %s AND kind = 'bulk_import' "
        "AND status IN ('queued', 'running')",
        [acme],
    ).fetchone()
    assert live is not None and live[0] >= 2
    response = await api.post(
        "/api/v1/products/bulk",
        headers={**world2.headers("operator@acme.test"), "Idempotency-Key": str(uuid.uuid4())},
        files={
            "file": ("p.csv", b"seller_sku,product_name,category\nS26-1,K,kurtas\n", "text/csv")
        },
    )
    assert response.status_code == 429, response.text
    assert response.json()["error"]["type"] == "RATE_LIMITED"


async def test_api4_regenerate_instructions_2001_422(
    api: httpx.AsyncClient, world2: World2
) -> None:
    listing = world2.side("acme").ids["listing_id"]
    response = await api.post(
        f"/api/v1/listings/{listing}/regenerate",
        json={"instructions": "x" * 2001},
        headers={**world2.headers("operator@acme.test"), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
