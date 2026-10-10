"""The S15 marketplace test world: the auth world (`acme`, `bharat`, one member per role) on an
emptied test database, the app under test as `app_rw`, both marketplaces configured against the
in-process fake marketplace (ASGI, no network), and helpers that walk connect -> consent ->
callback -> confirm through the real HTTP routes.

Rows are read back on the admin (superuser, autocommit) connection, so a row is seen there only
if the code under test committed it.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
import pytest
from tests.support.fake_marketplace import FakeConfig, build_fake

from .auth_support import PREFIX, World, bearer, build_world, tokens_for

Conn = psycopg.Connection[tuple[Any, ...]]
FAKE_BASE: Final = "http://fake.marketplace.test"
# S22 C10: Flipkart's OAuth URLs are pinned (loopback on local), so the fake answers them on a
# loopback origin; the ASGI transport never opens a socket either way.
FK_FAKE_BASE: Final = "http://127.0.0.1:4020"
DASHBOARD: Final = "http://dash.test:3000"
CFG: Final = FakeConfig()
MKT_ENV: Final[dict[str, str]] = {
    "AMAZON_APP_ID": CFG.amazon_application_id,
    "AMAZON_CLIENT_ID": CFG.amazon.client_id,
    "AMAZON_CLIENT_SECRET": CFG.amazon.client_secret,
    "AMAZON_AUTHORIZE_URL": f"{FAKE_BASE}/amazon/authorize",
    "AMAZON_LWA_TOKEN_URL": f"{FAKE_BASE}/amazon/token",
    "FLIPKART_CLIENT_ID": CFG.flipkart.client_id,
    "FLIPKART_CLIENT_SECRET": CFG.flipkart.client_secret,
    "FLIPKART_AUTHORIZE_URL": f"{FK_FAKE_BASE}/flipkart/authorize",
    "FLIPKART_TOKEN_URL": f"{FK_FAKE_BASE}/flipkart/token",
    "FLIPKART_ENABLED": "true",
    "DASHBOARD_BASE_URL": DASHBOARD,
}


@dataclass
class MktWorld:
    conn: Conn
    world: World
    fake: Any

    def tenant(self, slug: str) -> uuid.UUID:
        return self.world.tenant(slug)

    def user(self, email: str) -> uuid.UUID:
        return self.world.user(email)

    @property
    def control(self) -> Any:
        return self.fake.state.fake


def install_fake(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr(
        "app.adapters.oauth.http.make_transport", lambda: httpx.ASGITransport(app=fake)
    )


@pytest.fixture
async def mkt(auth_db: Conn, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MktWorld]:
    from app.core.config import get_settings
    from tests.support.rate_limits import reset_rate_limits

    for key, value in MKT_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    world = build_world(auth_db)
    reset_rate_limits(auth_db)
    fake = build_fake(CFG)
    install_fake(monkeypatch, fake)
    yield MktWorld(auth_db, world, fake)
    get_settings.cache_clear()


@pytest.fixture
async def mapi(mkt: MktWorld) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client on `create_app()` over ASGI (client address 203.0.113.7)."""
    from app.main import create_app

    transport = httpx.ASGITransport(app=create_app(), client=("203.0.113.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def as_member(mw: MktWorld, email: str) -> dict[str, str]:
    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(mw.world, email, slug))


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


async def connect(
    api: httpx.AsyncClient,
    mw: MktWorld,
    email: str = "owner@acme.test",
    marketplace: str = "amazon",
) -> str:
    response = await api.post(
        f"{PREFIX}/marketplaces/connect",
        json={"marketplace": marketplace},
        headers=as_member(mw, email),
    )
    assert response.status_code == 200, response.text
    return str(response.json()["authorize_url"])


async def consent(mw: MktWorld, authorize_url: str, seller: str | None = None) -> dict[str, str]:
    """The browser at the fake's consent page: the callback query it redirects with."""
    url = authorize_url if seller is None else f"{authorize_url}&seller={seller}"
    transport = httpx.ASGITransport(app=mw.fake)
    async with httpx.AsyncClient(transport=transport, base_url=FAKE_BASE) as browser:
        response = await browser.get(url)
    assert response.status_code == 302, response.text
    return query_of(response.headers["location"])


async def callback(
    api: httpx.AsyncClient, marketplace: str, query: dict[str, str], **headers: str
) -> httpx.Response:
    return await api.get(
        f"{PREFIX}/marketplaces/{marketplace}/callback", params=query, headers=headers
    )


def pending_of(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(f"{DASHBOARD}/marketplaces/connected?pending=")
    return query_of(location)["pending"]


async def connect_to_pending(
    api: httpx.AsyncClient,
    mw: MktWorld,
    email: str = "owner@acme.test",
    marketplace: str = "amazon",
    seller: str | None = None,
) -> str:
    url = await connect(api, mw, email, marketplace)
    query = await consent(mw, url, seller)
    return pending_of(await callback(api, marketplace, query))


async def confirm(api: httpx.AsyncClient, mw: MktWorld, email: str, handle: str) -> httpx.Response:
    return await api.post(
        f"{PREFIX}/marketplaces/connect/confirm",
        json={"pending": handle},
        headers=as_member(mw, email),
    )


async def connected_account(
    api: httpx.AsyncClient,
    mw: MktWorld,
    email: str = "owner@acme.test",
    marketplace: str = "amazon",
    seller: str | None = None,
) -> dict[str, Any]:
    handle = await connect_to_pending(api, mw, email, marketplace, seller)
    response = await confirm(api, mw, email, handle)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def oauth_rows(conn: Conn) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM oauth_states ORDER BY created_at")
    names = [d.name for d in cur.description or []]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def account_rows(conn: Conn) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM marketplace_accounts ORDER BY created_at")
    names = [d.name for d in cur.description or []]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
