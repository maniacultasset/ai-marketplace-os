"""SI5 T7 (AC4, PLAN D7): accept an invite with the Google identity of a `needs_details` handle,
against the in-process fake Google. A matching email gives a Google-only user and the membership;
a mismatch, a wrong binding, the flag off or a taken Google account writes nothing."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .auth_support import PREFIX, bearer, tokens_for
from .conftest import LOCAL_HOSTS
from .google_support import GoogleBrowser, GoogleWorld, gapi, gw  # noqa: F401

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

INVITES = f"{PREFIX}/invites"
GMAIL = "meera.iyer@gmail.com"


async def _invite(api: httpx.AsyncClient, world: Any, email: str) -> str:
    response = await api.post(
        INVITES,
        json={"email": email, "role": "reviewer"},
        headers=bearer(tokens_for(world, "owner@acme.test", "acme")),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["invite_url"]).split("#token=", 1)[1]


async def _details(browser: GoogleBrowser, email: str, **control: str) -> str:
    done = (await browser.sign_in(**{"as": email, **control})).json()
    assert done["outcome"] == "needs_details", done
    return str(done["details"]["details_handle"])


async def _accept(api: httpx.AsyncClient, token: str, handle: str, binding: str) -> httpx.Response:
    return await api.post(
        f"{INVITES}/accept",
        json={"token": token, "google": {"handle": handle, "binding": binding}},
    )


def _users(conn: Any, email: str) -> Any:
    return conn.execute(
        "SELECT hashed_password, google_sub, auth_provider, email_verified_at IS NOT NULL "
        "FROM users WHERE email = %s",
        [email],
    ).fetchall()


async def test_matching_google_email_makes_a_google_only_member(
    gapi: httpx.AsyncClient, gw: GoogleWorld  # noqa: F811
) -> None:
    token = await _invite(gapi, gw.world, GMAIL)
    browser = GoogleBrowser(gapi, gw)
    handle = await _details(browser, GMAIL)
    response = await _accept(gapi, token, handle, browser.secret)
    assert response.status_code == 200, response.text
    assert response.json() == {"email": GMAIL, "tenant_name": "Acme"}
    ((password, sub, provider, verified),) = _users(gw.conn, GMAIL)
    assert password is None and sub and provider == "google" and verified
    member = gw.conn.execute(
        "SELECT m.tenant_id, m.role FROM tenant_memberships m JOIN users u ON u.id = m.user_id "
        "WHERE u.email = %s",
        [GMAIL],
    ).fetchall()
    assert member == [(gw.world.tenant("acme"), "reviewer")]
    again = await GoogleBrowser(gapi, gw).sign_in(**{"as": GMAIL})
    assert again.json()["outcome"] == "sign_in", again.text


async def test_mismatched_google_email_is_422_and_writes_nothing(
    gapi: httpx.AsyncClient, gw: GoogleWorld  # noqa: F811
) -> None:
    token = await _invite(gapi, gw.world, GMAIL)
    browser = GoogleBrowser(gapi, gw)
    handle = await _details(browser, "someone.else@gmail.com")
    response = await _accept(gapi, token, handle, browser.secret)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "google"]
    assert _users(gw.conn, "someone.else@gmail.com") == []
    assert _users(gw.conn, GMAIL) == []
    pending = gw.conn.execute(
        "SELECT accepted_at FROM invitations WHERE email = %s", [GMAIL]
    ).fetchone()
    assert pending == (None,)


async def test_wrong_binding_is_401(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:  # noqa: F811
    token = await _invite(gapi, gw.world, GMAIL)
    browser = GoogleBrowser(gapi, gw)
    handle = await _details(browser, GMAIL)
    response = await _accept(gapi, token, handle, "x" * 43)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "OAUTH_STATE_INVALID"
    assert _users(gw.conn, GMAIL) == []


async def test_google_off_is_501(
    gapi: httpx.AsyncClient, gw: GoogleWorld, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from app.core.config import get_settings

    token = await _invite(gapi, gw.world, GMAIL)
    browser = GoogleBrowser(gapi, gw)
    handle = await _details(browser, GMAIL)
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "false")
    get_settings.cache_clear()
    response = await _accept(gapi, token, handle, browser.secret)
    assert response.status_code == 501
    assert _users(gw.conn, GMAIL) == []


async def test_google_account_linked_elsewhere_is_409(
    gapi: httpx.AsyncClient, gw: GoogleWorld  # noqa: F811
) -> None:
    token = await _invite(gapi, gw.world, GMAIL)
    browser = GoogleBrowser(gapi, gw)
    handle = await _details(browser, GMAIL, sub="google-sub-taken")
    gw.conn.execute(
        "UPDATE users SET google_sub = 'google-sub-taken' WHERE email = 'admin@bharat.test'"
    )
    response = await _accept(gapi, token, handle, browser.secret)
    assert response.status_code == 409
    assert _users(gw.conn, GMAIL) == []
