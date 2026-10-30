"""ADR-0020 test world: Google sign-in switched on against the in-process fake Google (ASGI, no
network), the auth world on an emptied test database, and a `GoogleBrowser` that walks start ->
Google's consent -> callback -> complete through the real HTTP routes, keeping its binding secret
the way the app does.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import psycopg
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from tests.support.fake_google import FakeGoogleConfig, build_fake_google

from .auth_support import PREFIX, World, build_world

Conn = psycopg.Connection[tuple[Any, ...]]
FAKE_BASE: Final = "http://fake.google.test"
DASHBOARD: Final = "http://dash.test:3000"
CFG: Final = FakeGoogleConfig(
    client_id="test-google-client.apps.googleusercontent.com",
    client_secret="test-google-client-secret-not-real",
    redirect_uri=f"{DASHBOARD}/api/v1/auth/google/callback",
    issuer=f"{FAKE_BASE}/google",
)
GOOGLE_ENV: Final[dict[str, str]] = {
    "GOOGLE_OAUTH_ENABLED": "true",
    "GOOGLE_OAUTH_CLIENT_ID": CFG.client_id,
    "GOOGLE_OAUTH_CLIENT_SECRET": CFG.client_secret,
    "GOOGLE_AUTHORIZE_URL": f"{FAKE_BASE}/google/authorize",
    "GOOGLE_TOKEN_URL": f"{FAKE_BASE}/google/token",
    "GOOGLE_JWKS_URL": f"{FAKE_BASE}/google/certs",
    "GOOGLE_ISSUER": CFG.issuer,
    "DASHBOARD_BASE_URL": DASHBOARD,
}
START = f"{PREFIX}/auth/google/start"
CALLBACK = f"{PREFIX}/auth/google/callback"
COMPLETE = f"{PREFIX}/auth/google/complete"
LINK = f"{PREFIX}/auth/google/link"
UNLINK = f"{PREFIX}/auth/google/unlink"
SIGNUP_GOOGLE = f"{PREFIX}/signup/google"
GENERIC: Final = (
    "Couldn't sign you in with Google. If you already have an account, sign in with your email "
    "and password."
)


def binding_of(secret: str) -> str:
    digest = hashlib.sha256(secret.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


@dataclass
class GoogleWorld:
    conn: Conn
    world: World
    fake: Any

    @property
    def state(self) -> Any:
        return self.fake.routes[0].app.state.fake


def install_fake(monkeypatch: pytest.MonkeyPatch, app: Starlette) -> None:
    monkeypatch.setattr(
        "app.adapters.oauth.http.make_transport", lambda: httpx.ASGITransport(app=app)
    )


def reset_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import google_signin

    monkeypatch.setattr(google_signin._JWKS, "keys", {})
    monkeypatch.setattr(google_signin._JWKS, "fetched_at", None)
    monkeypatch.setattr(google_signin._JWKS, "last_miss_refetch", None)


@pytest.fixture
async def gw(auth_db: Conn, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[GoogleWorld]:
    from app.core.config import get_settings
    from tests.support.rate_limits import reset_rate_limits

    for key, value in GOOGLE_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    world = build_world(auth_db)
    reset_rate_limits(auth_db)
    fake = Starlette(routes=[Mount("/google", app=build_fake_google(CFG))])
    install_fake(monkeypatch, fake)
    reset_jwks(monkeypatch)
    yield GoogleWorld(auth_db, world, fake)
    get_settings.cache_clear()


@pytest.fixture
async def gapi(gw: GoogleWorld) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import create_app

    transport = httpx.ASGITransport(app=create_app(), client=("203.0.113.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@dataclass
class GoogleBrowser:
    """One browser tab: its binding secret lives here (the app keeps it in sessionStorage)."""

    api: httpx.AsyncClient
    gw: GoogleWorld
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    @property
    def b(self) -> str:
        return binding_of(self.secret)

    async def start(self) -> httpx.Response:
        return await self.api.get(START, params={"b": self.b})

    async def consent(self, authorize_url: str, **control: str) -> str:
        """The user picks an account at (fake) Google: the callback URL Google sends back."""
        url = f"{authorize_url}&{urlencode(control)}" if control else authorize_url
        transport = httpx.ASGITransport(app=self.gw.fake)
        async with httpx.AsyncClient(transport=transport) as google:
            reply = await google.get(url)
        assert reply.status_code == 302, reply.text
        return reply.headers["location"]

    async def callback(self, google_redirect: str) -> httpx.Response:
        return await self.api.get(CALLBACK, params=query_of(google_redirect))

    async def handle(self, **control: str) -> str:
        """Start, consent and callback: the opaque handle the app route receives. The rate-limit
        counters are emptied first (each sign-in spends three of the 10 a minute)."""
        from tests.support.rate_limits import reset_rate_limits

        reset_rate_limits(self.gw.conn)
        started = await self.start()
        assert started.status_code == 302, started.text
        back = await self.callback(await self.consent(started.headers["location"], **control))
        assert back.status_code == 303, back.text
        location = back.headers["location"]
        assert location.startswith(f"{DASHBOARD}/auth/google?"), location
        query = query_of(location)
        assert "handle" in query, query
        return query["handle"]

    async def complete(self, handle: str, *, binding: str | None = None) -> httpx.Response:
        return await self.api.post(
            COMPLETE, json={"handle": handle, "binding": binding or self.secret}
        )

    async def sign_in(self, **control: str) -> httpx.Response:
        return await self.complete(await self.handle(**control))


def approve(conn: Conn, email: str, slug: str) -> Any:
    """What a platform admin's approve does (a trial tenant, the applicant as its owner), done on
    the admin connection so the test needs no admin step-up."""
    from .auth_support import add_member, add_tenant

    tenant = add_tenant(conn, slug, status="trial")
    row = conn.execute("SELECT id FROM users WHERE email = %s", [email]).fetchone()
    assert row is not None
    add_member(conn, tenant, row[0], "owner")
    conn.execute(
        "UPDATE signup_requests SET status = 'approved', decided_at = now(), "
        "created_tenant_id = %s WHERE user_id = %s AND status = 'pending'",
        [tenant, row[0]],
    )
    return tenant


def google_sub_of(conn: Conn, email: str) -> str | None:
    row = conn.execute("SELECT google_sub FROM users WHERE email = %s", [email]).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def flows(conn: Conn, phase: str | None = None) -> int:
    if phase is None:
        row = conn.execute("SELECT count(*) FROM login_flows").fetchone()
    else:
        row = conn.execute("SELECT count(*) FROM login_flows WHERE phase = %s", [phase]).fetchone()
    assert row is not None
    return int(row[0])
