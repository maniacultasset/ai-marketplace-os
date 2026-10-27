"""S10 C7 (DW7, AC7; ADR-0005:10-16; DATABASE.md:1024): the Postgres fixed-window limiter, as
app_rw. The counter autocommits on its own pooled connection before any business transaction, so
it survives the request's rollback, never waits on a business lock and never needs a second
connection while the request holds one."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
import pytest
from app.api.errors import install_error_handlers
from app.api.v1.deps import public, require
from app.core.asgi import MkosApp
from app.core.rbac import Action, Principal
from fastapi import APIRouter, FastAPI

from .auth_support import Conn, World, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


class Gate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()


def _limited_app(gate: Gate) -> FastAPI:
    from app.repositories.transactions import unit_of_work

    router = APIRouter()

    @router.get("/read")
    async def read(principal: Annotated[Principal, require(Action.TENANT_READ)]) -> Any:
        return {"tenant": str(principal.tenant_id)}

    @router.get("/hold")
    async def hold(principal: Annotated[Principal, require(Action.TENANT_READ)]) -> Any:
        async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
            from sqlalchemy import text

            await session.execute(text("SELECT 1"))
            gate.entered.set()
            await gate.release.wait()
        return {"held": True}

    @router.get("/fail")
    async def fail(principal: Annotated[Principal, require(Action.TENANT_READ)]) -> Any:
        async with unit_of_work(principal.tenant_id, user_id=principal.user_id):
            raise RuntimeError("business failure")

    @router.get("/slow")
    async def slow(principal: Annotated[Principal, require(Action.TENANT_READ)]) -> Any:
        async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
            from sqlalchemy import text

            await session.execute(text("SELECT pg_sleep(0.3)"))
        return {"ok": True}

    @router.get("/open", dependencies=[public()])
    async def open_route() -> Any:
        return {"ok": True}

    app = MkosApp()
    install_error_handlers(app)
    app.include_router(router, prefix="/api/v1/limited")
    return app


@pytest.fixture(autouse=True)
def mid_window_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limiter's windows are fixed minutes: a burst that straddles a boundary (CI ran 121
    calls from 09:32:59 into 09:33:00) resets the count mid-test. Every test here counts inside
    one window, so the clock is pinned mid-minute; the rollover test sets its own."""
    from app.services import rate_limit

    monkeypatch.setattr(rate_limit, "now", lambda: datetime(2026, 10, 6, 12, 0, 30, tzinfo=UTC))


@pytest.fixture
def gate() -> Gate:
    return Gate()


@pytest.fixture
async def client(auth_db: Conn, gate: Gate) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_limited_app(gate), client=("198.51.100.20", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _count(conn: Conn, prefix: str) -> int:
    row = conn.execute(
        "SELECT coalesce(sum(n), 0) FROM rate_limit_counters WHERE bucket_key LIKE %s",
        [prefix + "%"],
    ).fetchone()
    assert row is not None
    return int(row[0])


def _assert_429(response: httpx.Response) -> None:
    assert response.status_code == 429, response.text
    error = response.json()["error"]
    assert error["type"] == "RATE_LIMITED"
    assert error["error_report"]["errors"][0]["source"] == "system"
    assert 1 <= int(response.headers["retry-after"]) <= 60


async def test_rate_limit_tenant_121st_on_include_router_route(
    client: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = bearer(tokens_for(world, "reviewer@acme.test", "acme"))
    other = bearer(tokens_for(world, "owner@acme.test", "acme"))
    for _ in range(119):
        assert (await client.get("/api/v1/limited/read", headers=token)).status_code == 200
    # the bucket is the tenant's, whoever in it calls
    assert (await client.get("/api/v1/limited/read", headers=other)).status_code == 200
    _assert_429(await client.get("/api/v1/limited/read", headers=token))
    assert _count(auth_db, f"t:{world.tenant('acme')}") == 121
    # another tenant has its own bucket
    bharat = bearer(tokens_for(world, "reviewer@bharat.test", "bharat"))
    assert (await client.get("/api/v1/limited/read", headers=bharat)).status_code == 200


async def test_rate_limit_bad_token_never_counts(
    client: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    for _ in range(3):
        bad = await client.get("/api/v1/limited/read", headers=bearer("not.a.token"))
        assert bad.status_code == 401
    assert _count(auth_db, "t:") == 0


async def test_rate_limit_ip_61st_unauthenticated(
    client: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    for _ in range(60):
        assert (await client.get("/api/v1/limited/open")).status_code == 200
    _assert_429(await client.get("/api/v1/limited/open"))
    assert _count(auth_db, "ip:198.51.100.20") == 61


async def test_rate_limit_window_rollover(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import rate_limit

    moment = {"now": datetime(2026, 10, 6, 12, 0, 59, tzinfo=UTC)}
    monkeypatch.setattr(rate_limit, "now", lambda: moment["now"])
    for _ in range(60):
        assert (await client.get("/api/v1/limited/open")).status_code == 200
    limited = await client.get("/api/v1/limited/open")
    _assert_429(limited)
    assert limited.headers["retry-after"] == "1"
    moment["now"] = datetime(2026, 10, 6, 12, 1, 0, tzinfo=UTC)
    assert (await client.get("/api/v1/limited/open")).status_code == 200


async def test_rate_limit_held_open_request_does_not_block(
    client: httpx.AsyncClient, world: World, gate: Gate
) -> None:
    token = bearer(tokens_for(world, "operator@acme.test", "acme"))
    held = asyncio.create_task(client.get("/api/v1/limited/hold", headers=token))
    await asyncio.wait_for(gate.entered.wait(), timeout=10)
    try:
        # the same tenant bucket, while the first request's business transaction is open
        second = await asyncio.wait_for(
            client.get("/api/v1/limited/read", headers=token), timeout=5
        )
        assert second.status_code == 200
    finally:
        gate.release.set()
    assert (await held).status_code == 200


async def test_rate_limit_counter_survives_business_rollback(
    client: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = bearer(tokens_for(world, "operator@acme.test", "acme"))
    response = await client.get("/api/v1/limited/fail", headers=token)
    assert response.status_code == 500
    assert _count(auth_db, f"t:{world.tenant('acme')}") == 1


async def test_rate_limit_concurrency_pool_plus_one(
    client: httpx.AsyncClient, world: World
) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    n = settings.database_pool_size + settings.database_max_overflow + 1
    token = bearer(tokens_for(world, "operator@acme.test", "acme"))
    started = time.monotonic()
    responses = await asyncio.gather(
        *(client.get("/api/v1/limited/slow", headers=token) for _ in range(n))
    )
    elapsed = time.monotonic() - started
    assert [r.status_code for r in responses] == [200] * n
    assert elapsed < settings.database_pool_timeout


async def test_rate_limit_public_unlimited_skips_counter(auth_db: Conn) -> None:
    router = APIRouter()

    @router.get("/probe", dependencies=[public(rate_limited=False)])
    async def probe() -> Any:
        return {"ok": True}

    app = MkosApp()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        for _ in range(70):
            assert (await c.get("/probe")).status_code == 200
    assert _count(auth_db, "") == 0


# ---- the login bucket (DW7), on the real app ----

LOGIN = "/api/v1/auth/login"


async def _bad_login(api: httpx.AsyncClient, email: str, xff: str | None = None) -> httpx.Response:
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    return await api.post(LOGIN, json={"email": email, "password": "wrong-pw"}, headers=headers)


async def test_rate_limit_sixth_login_429(api: httpx.AsyncClient, world: World) -> None:
    for _ in range(5):
        assert (await _bad_login(api, "owner@acme.test")).status_code == 401
    _assert_429(await _bad_login(api, "owner@acme.test"))
    # the right password does not get past the limit either
    blocked = await api.post(
        LOGIN, json={"email": "owner@acme.test", "password": "Test-Password-0123"}
    )
    _assert_429(blocked)


async def test_rate_limit_login_bucket_folds_case(api: httpx.AsyncClient, world: World) -> None:
    for email in (
        "owner@acme.test",
        "OWNER@acme.test",
        "Owner@Acme.Test",
        "owner@ACME.test",
        "oWner@acme.test",
    ):
        assert (await _bad_login(api, email)).status_code == 401
    _assert_429(await _bad_login(api, "OWNER@ACME.TEST"))


@pytest.mark.parametrize("env", ["local", "production"])
async def test_rate_limit_forged_leftmost_xff_same_bucket(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    from types import MappingProxyType

    from app.api import rate_limit

    # the app runs under APP_ENV=local; give `local` the hop count of the row under test
    monkeypatch.setattr(
        rate_limit,
        "HOPS",
        MappingProxyType({**rate_limit.HOPS, "local": rate_limit.HOPS[env]}),  # type: ignore[index]
    )
    # what the load balancer appends in production: <client>,<lb>
    proxy_tail = "198.51.100.77, 130.211.0.9"
    for n in range(5):
        assert (
            await _bad_login(api, "owner@acme.test", f"10.0.0.{n}, {proxy_tail}")
        ).status_code == 401
    _assert_429(await _bad_login(api, "owner@acme.test", f"6.6.6.6, {proxy_tail}"))
    _assert_429(await _bad_login(api, "owner@acme.test", proxy_tail))


async def test_rate_limit_other_email_own_bucket(api: httpx.AsyncClient, world: World) -> None:
    for _ in range(6):
        await _bad_login(api, "owner@acme.test")
    assert (await _bad_login(api, "reviewer@acme.test")).status_code == 401
    ok = await api.post(
        LOGIN, json={"email": "operator@acme.test", "password": "Test-Password-0123"}
    )
    assert ok.status_code == 200


async def test_rate_limit_refused_member_never_spends_tenant_budget(
    client: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """AUTH-2 / TENANCY-2: a deactivated member's still-valid access token gets 401s that never
    count against the tenant's shared bucket, so it cannot hold the active members at 429; its
    own refusals are capped in a bucket of its own."""
    tenant = world.tenant("acme")
    fired = bearer(tokens_for(world, "operator@acme.test", "acme"))  # issued while a member
    owner = bearer(tokens_for(world, "owner@acme.test", "acme"))
    auth_db.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
        "WHERE tenant_id = %s AND user_id = %s",
        [tenant, world.user("operator@acme.test")],
    )
    statuses = {
        (await client.get("/api/v1/limited/read", headers=fired)).status_code for _ in range(120)
    }
    assert statuses == {401}
    assert _count(auth_db, f"t:{tenant}") == 0
    assert (await client.get("/api/v1/limited/read", headers=owner)).status_code == 200
    assert _count(auth_db, f"t:{tenant}") == 1
    # the refused token's own bucket: the 121st refusal is a 429, the tenant is untouched
    _assert_429(await client.get("/api/v1/limited/read", headers=fired))
    assert _count(auth_db, f"t:{tenant}") == 1
