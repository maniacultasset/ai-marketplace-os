"""SI5 (AC8): the public invite steps share one per-IP bucket (`invite:`), five a minute; another
address is not affected. The reset redeem has its own bucket (`reset:`, T8)."""

from __future__ import annotations

import httpx
import pytest

from .auth_support import PREFIX, Conn
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


async def test_sixth_invite_call_from_one_ip_is_429(auth_db: Conn) -> None:
    from app.main import create_app

    app = create_app()
    one = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 40000)),
        base_url="http://testserver",
    )
    other = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.8", 40000)),
        base_url="http://testserver",
    )
    async with one, other:
        body = {"token": "T" * 43}
        statuses = [
            (await one.post(f"{PREFIX}/invites/lookup", json=body)).status_code for _ in range(3)
        ]
        statuses += [
            (
                await one.post(
                    f"{PREFIX}/invites/accept",
                    json={**body, "password": "Brass-Lantern-Path-0123"},
                )
            ).status_code
            for _ in range(2)
        ]
        assert statuses == [404] * 5
        limited = await one.post(f"{PREFIX}/invites/lookup", json=body)
        assert limited.status_code == 429
        assert limited.json()["error"]["type"] == "RATE_LIMITED"
        assert int(limited.headers["Retry-After"]) >= 1
        assert (await other.post(f"{PREFIX}/invites/lookup", json=body)).status_code == 404
