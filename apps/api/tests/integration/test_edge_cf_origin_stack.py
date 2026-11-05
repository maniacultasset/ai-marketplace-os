"""EDGE-CF (ADR-0025) through the real stack: `create_app()` over ASGI, the Postgres-backed limiter
and the readiness probe on the test database. AC1, AC3, AC5 and AC12 end to end (tester, round 1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import psycopg
import pytest

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
SECRET = "Tst-edge-origin-secret-current-0001"
PREVIOUS = "Tst-edge-origin-secret-previous-001"
PEER = "203.0.113.7"
ME = "/api/v1/auth/me"
LOGIN = "/api/v1/auth/login"
ORIGIN = {"x-kuinn-origin-auth": SECRET}

Factory = Callable[..., Any]


@pytest.fixture
async def edge_api(auth_db: Conn, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Factory]:
    from app.core.config import get_settings
    from app.main import create_app

    clients: list[httpx.AsyncClient] = []

    def make(mode: str, secret: str | None = SECRET, **env: str) -> httpx.AsyncClient:
        monkeypatch.setenv("ORIGIN_AUTH", mode)
        if secret is None:
            monkeypatch.delenv("EDGE_ORIGIN_SECRET", raising=False)
        else:
            monkeypatch.setenv("EDGE_ORIGIN_SECRET", secret)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        transport = httpx.ASGITransport(app=create_app(), client=(PEER, 40000))
        client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


def _ip_buckets(db: Conn) -> set[str]:
    rows = db.execute(
        "SELECT bucket_key FROM rate_limit_counters WHERE bucket_key LIKE 'ip:%'"
    ).fetchall()
    return {row[0] for row in rows}


def _counter_rows(db: Conn) -> int:
    row = db.execute("SELECT count(*) FROM rate_limit_counters").fetchone()
    assert row is not None
    return int(row[0])


# ---- AC1, AC3: enforce on the real app ----


async def test_enforce_readyz_reaches_postgres_without_header(edge_api: Factory) -> None:
    api = edge_api("enforce")
    response = await api.get("/readyz")
    assert response.status_code == 200, response.text
    assert response.json()["checks"]["postgres"] == "ok"
    assert '"postgres":"ok"' in response.text  # the string deploy.sh smoke greps for


async def test_enforce_api_route_403_without_header_401_with(edge_api: Factory) -> None:
    api = edge_api("enforce")
    denied = await api.get(ME)
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["type"] == "PERMISSION_DENIED"
    assert denied.headers["x-request-id"]
    allowed = await api.get(ME, headers=ORIGIN)
    assert allowed.status_code == 401, allowed.text


async def test_enforce_previous_rotation_value_reaches_routes(edge_api: Factory) -> None:
    api = edge_api("enforce", f"{SECRET} {PREVIOUS}")
    response = await api.get(ME, headers={"x-kuinn-origin-auth": PREVIOUS})
    assert response.status_code == 401, response.text


async def test_enforce_denied_logins_never_touch_the_limiter(
    edge_api: Factory, auth_db: Conn
) -> None:
    api = edge_api("enforce")
    for _ in range(5):
        response = await api.post(
            LOGIN,
            json={"email": "a@example.com", "password": "nope-nope-nope"},
            headers={"x-kuinn-origin-auth": SECRET[:-1] + "X", "x-kuinn-client-ip": "198.51.100.9"},
        )
        assert response.status_code == 403, response.text
    assert _counter_rows(auth_db) == 0


# ---- AC12: the limiter keys on the edge client IP, in Postgres ----


@pytest.mark.parametrize("mode", ["off", "enforce"])
async def test_edge_ips_get_their_own_postgres_buckets(
    edge_api: Factory, auth_db: Conn, mode: str
) -> None:
    api = edge_api(mode)
    for ip in ("198.51.100.21", "198.51.100.22"):
        await api.post(
            LOGIN,
            json={"email": f"{ip}@example.com", "password": "nope-nope-nope"},
            headers={**ORIGIN, "x-kuinn-client-ip": ip},
        )
    assert {"ip:198.51.100.21", "ip:198.51.100.22"} <= _ip_buckets(auth_db)
    assert f"ip:{PEER}" not in _ip_buckets(auth_db)


async def test_forged_client_ip_without_secret_keys_on_the_peer(
    edge_api: Factory, auth_db: Conn
) -> None:
    api = edge_api("off")
    for ip, origin in (("198.51.100.31", None), ("198.51.100.32", "wrong-" + SECRET)):
        headers = {"x-kuinn-client-ip": ip}
        if origin is not None:
            headers["x-kuinn-origin-auth"] = origin
        await api.post(
            LOGIN, json={"email": "z@example.com", "password": "nope-nope-nope"}, headers=headers
        )
    buckets = _ip_buckets(auth_db)
    assert buckets == {f"ip:{PEER}"}, buckets


async def test_one_edge_ip_exhausted_leaves_another_free(edge_api: Factory, auth_db: Conn) -> None:
    api = edge_api("enforce", AUTH_LOGIN_RATE_LIMIT_PER_MIN="2")
    statuses = []
    # The limit counts per clock minute. Six tries split over at most two minutes put at least
    # three in one of them, so one is refused even when the minute rolls over mid-loop.
    for _ in range(6):
        response = await api.post(
            LOGIN,
            json={"email": "victim@example.com", "password": "nope-nope-nope"},
            headers={**ORIGIN, "x-kuinn-client-ip": "198.51.100.41"},
        )
        statuses.append(response.status_code)
    assert 429 in statuses, statuses
    other = await api.post(
        LOGIN,
        json={"email": "bystander@example.com", "password": "nope-nope-nope"},
        headers={**ORIGIN, "x-kuinn-client-ip": "198.51.100.42"},
    )
    assert other.status_code != 429, other.text


async def test_off_without_secret_strips_client_ip(edge_api: Factory, auth_db: Conn) -> None:
    api = edge_api("off", None)
    await api.post(
        LOGIN,
        json={"email": "y@example.com", "password": "nope-nope-nope"},
        headers={"x-kuinn-client-ip": "198.51.100.51", "x-kuinn-origin-auth": SECRET},
    )
    assert _ip_buckets(auth_db) == {f"ip:{PEER}"}
