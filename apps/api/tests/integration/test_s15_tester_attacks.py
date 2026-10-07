"""S15 tester attacks (independent of the implementer's suites): concurrency on the one-time
consume, duplicate refresh delivery, refresh-token rotation, the full reconnect cycle, revoke then
re-connect, tenant lifecycle between connect and callback, and hostile callback input. Real stack:
create_app() over ASGI as `app_rw`, the in-process fake marketplace, LocalKmsStub."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from .auth_support import PREFIX, audit_rows
from .conftest import LOCAL_HOSTS
from .marketplace_support import (
    MktWorld,
    account_rows,
    as_member,
    callback,
    confirm,
    connect,
    connect_to_pending,
    connected_account,
    consent,
    mapi,
    mkt,
    oauth_rows,
    pending_of,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]


def _row(mw: MktWorld, account_id: str) -> dict[str, Any]:
    return next(r for r in account_rows(mw.conn) if str(r["id"]) == account_id)


def _due(mw: MktWorld, account_id: str) -> None:
    mw.conn.execute(
        "UPDATE marketplace_accounts SET expires_at = now() + interval '1 minute' WHERE id = %s",
        [account_id],
    )


async def _refresh(mw: MktWorld, account_id: str, slug: str = "acme") -> str:
    from app.workers.token_refresh import refresh_account

    return await refresh_account(mw.tenant(slug), uuid.UUID(account_id))


async def _stored_refresh_token(mw: MktWorld, account_id: str, slug: str = "acme") -> str:
    from app.adapters.credentials import get_key_unwrapper, unseal

    row = _row(mw, account_id)
    pair = await unseal(
        bytes(row["token_enc"]),
        key_version=row["kms_key_version"],
        tenant_id=mw.tenant(slug),
        unwrapper=get_key_unwrapper(),
    )
    return pair.refresh_token.get_secret_value()


# ---- the one-time consume under concurrency ----


async def test_attack_callback_same_state_concurrent_one_pending(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """Two browsers (or a replaying attacker) hit the callback with the same state at once:
    exactly one 303 and one pending row; the other is the uniform 409."""
    query = await consent(mkt, await connect(mapi, mkt, marketplace="flipkart"))
    responses = await asyncio.gather(*(callback(mapi, "flipkart", query) for _ in range(4)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [303, 409, 409, 409], [r.text for r in responses]
    for r in responses:
        if r.status_code == 409:
            assert r.json()["error"]["type"] == "OAUTH_STATE_INVALID"
    pending = [r for r in oauth_rows(mkt.conn) if r["token_enc"] is not None]
    assert len(pending) == 1
    assert mkt.control.exchanges == 1


# ---- duplicate refresh delivery ----


async def test_attack_two_refresh_passes_concurrent_refresh_once(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """Two periodic passes delivered at once (a duplicate job): the account is refreshed once,
    one `credential_used` row, one token request to the marketplace."""
    from app.workers.periodic import refresh_marketplace_tokens

    account = await connected_account(mapi, mkt, seller="A1DUPPASS")
    _due(mkt, account["id"])
    before = mkt.control.refreshes
    results = await asyncio.gather(refresh_marketplace_tokens(), refresh_marketplace_tokens())
    assert mkt.control.refreshes - before == 1, results
    assert len(audit_rows(mkt.conn, "marketplace_account.credential_used")) == 1
    assert _row(mkt, account["id"])["status"] == "active"


async def test_attack_refresh_twice_sequential_second_skipped(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """A redelivered refresh after a successful one: the account is no longer due, so nothing
    is unsealed or sent."""
    account = await connected_account(mapi, mkt, seller="A1REDELIV")
    _due(mkt, account["id"])
    assert await _refresh(mkt, account["id"]) == "refreshed"
    assert await _refresh(mkt, account["id"]) == "skipped"
    assert mkt.control.refreshes == 1
    assert len(audit_rows(mkt.conn, "marketplace_account.credential_used")) == 1


# ---- refresh-token rotation ----


class _Rotating(httpx.AsyncBaseTransport):
    """A token endpoint that rotates the refresh token on every refresh and accepts only the
    newest one (as LWA may): proves the worker stores the rotated token."""

    def __init__(self, first: str) -> None:
        self.current = first
        self.seen: list[str] = []
        self.n = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert form["grant_type"] == "refresh_token"
        self.seen.append(form["refresh_token"])
        if form["refresh_token"] != self.current:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.n += 1
        self.current = f"ROTATEDrefresh{self.n}x" * 2
        body = {
            "access_token": f"ROTATEDaccess{self.n}x",
            "refresh_token": self.current,
            "expires_in": 3600,
        }
        return httpx.Response(200, content=json.dumps(body).encode())


async def test_attack_refresh_rotation_is_persisted(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await connected_account(mapi, mkt, seller="A1ROTATE")
    first = await _stored_refresh_token(mkt, account["id"])
    rotating = _Rotating(first)
    monkeypatch.setattr("app.adapters.oauth.http.make_transport", lambda: rotating)
    for _ in range(3):
        _due(mkt, account["id"])
        assert await _refresh(mkt, account["id"]) == "refreshed"
    assert rotating.seen[0] == first
    assert rotating.seen[1:] == ["ROTATEDrefresh1x" * 2, "ROTATEDrefresh2x" * 2]
    assert await _stored_refresh_token(mkt, account["id"]) == rotating.current
    blob = bytes(_row(mkt, account["id"])["token_enc"])
    assert rotating.current.encode() not in blob


# ---- the full reconnect cycle and revoke then re-connect ----


async def test_attack_rejected_then_reconnect_then_refresh_cycle(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """invalid_grant -> needs_reconnect + one open task; the same seller reconnects (same id,
    task applied, error cleared); the new tokens refresh fine afterwards."""
    account = await connected_account(mapi, mkt, seller="A1CYCLE")
    _due(mkt, account["id"])
    mkt.control.next_refresh = "invalid_grant"
    assert await _refresh(mkt, account["id"]) == "rejected"
    handle = await connect_to_pending(mapi, mkt, seller="A1CYCLE")
    response = await confirm(mapi, mkt, "owner@acme.test", handle)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == account["id"]
    row = _row(mkt, account["id"])
    assert row["status"] == "active"
    assert row["needs_reconnect_since"] is None and row["last_refresh_error_code"] is None
    tasks = mkt.conn.execute(
        "SELECT status FROM human_tasks WHERE marketplace_account_id = %s", [account["id"]]
    ).fetchall()
    assert tasks == [("applied",)]
    _due(mkt, account["id"])
    assert await _refresh(mkt, account["id"]) == "refreshed"
    assert len(audit_rows(mkt.conn, "marketplace_account.reconnected")) == 1


async def test_attack_revoke_then_reconnect_same_seller_new_row(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """After revoke, the same seller connects again: a new live row, the revoked one stays
    shredded, and the revoked account is never refreshed."""
    account = await connected_account(mapi, mkt, seller="A1AGAIN")
    revoked = await mapi.post(
        f"{PREFIX}/marketplaces/accounts/{account['id']}/revoke",
        headers=as_member(mkt, "owner@acme.test"),
    )
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    again = await connected_account(mapi, mkt, seller="A1AGAIN")
    assert again["id"] != account["id"] and again["status"] == "active"
    old = _row(mkt, account["id"])
    assert old["status"] == "revoked" and old["token_enc"] is None
    _due(mkt, account["id"])
    assert await _refresh(mkt, account["id"]) == "skipped"
    listed = await mapi.get(
        f"{PREFIX}/marketplaces/accounts", headers=as_member(mkt, "owner@acme.test")
    )
    statuses = sorted(a["status"] for a in listed.json()["data"])
    assert statuses == ["active", "revoked"]


async def test_attack_revoked_seller_can_go_live_in_other_tenant(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """T31 after revoke: once acme revokes seller X, bharat may connect X (AC14 'allows one
    after revoke' at the API level)."""
    account = await connected_account(mapi, mkt, seller="A1MOVE")
    handle = await connect_to_pending(mapi, mkt, email="owner@bharat.test", seller="A1MOVE")
    blocked = await confirm(mapi, mkt, "owner@bharat.test", handle)
    assert blocked.status_code == 409
    assert "acme" not in blocked.text.lower()
    await mapi.post(
        f"{PREFIX}/marketplaces/accounts/{account['id']}/revoke",
        headers=as_member(mkt, "owner@acme.test"),
    )
    retried = await confirm(mapi, mkt, "owner@bharat.test", handle)
    assert retried.status_code == 200, retried.text
    assert retried.json()["seller_id"] == "A1MOVE"


# ---- tenant lifecycle between connect and callback ----


async def test_attack_callback_after_tenant_suspended_no_500(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """The tenant is suspended between connect and the callback: the callback must not crash
    (no 500), and a pending row it stores can never be confirmed into a live account."""
    query = await consent(mkt, await connect(mapi, mkt, marketplace="flipkart"))
    mkt.conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", [mkt.tenant("acme")])
    response = await callback(mapi, "flipkart", query)
    assert response.status_code < 500, (response.status_code, response.text)
    if response.status_code == 303:
        handle = pending_of(response)
        confirmed = await confirm(mapi, mkt, "owner@acme.test", handle)
        assert confirmed.status_code in (401, 403, 409), confirmed.text
        assert account_rows(mkt.conn) == []


# ---- hostile callback input ----


@pytest.mark.parametrize(
    "seller",
    ["A1B\n", "A1B%0A", "A1\u0661", "\uff211B", "A1B\x00", "../A1"],
    ids=["newline", "pct-newline", "arabic-digit", "fullwidth", "nul", "dotdot"],
)
async def test_attack_amazon_seller_param_unicode_and_newline_422(
    mkt: MktWorld, mapi: httpx.AsyncClient, seller: str
) -> None:
    """The D4 pattern is ASCII letters and digits only: a trailing newline, a non-ASCII digit
    or a fullwidth letter must not slip through (Python's `\\d`/`$` pitfalls)."""
    query = await consent(mkt, await connect(mapi, mkt))
    query["selling_partner_id"] = seller
    response = await callback(mapi, "amazon", query)
    assert response.status_code == 422, response.text
    assert [r for r in oauth_rows(mkt.conn) if r["token_enc"] is not None] == []


@pytest.mark.parametrize(
    "seller",
    ["A1B\n", "A1\u0661", "\uff211B", " A1B"],
    ids=["newline", "arabic", "fullwidth", "sp"],
)
async def test_attack_flipkart_token_seller_unicode_refused(
    mkt: MktWorld, mapi: httpx.AsyncClient, seller: str
) -> None:
    """The Flipkart seller id comes from the token reply: the same pattern, refused before
    sealing (no pending row)."""
    query = await consent(mkt, await connect(mapi, mkt, marketplace="flipkart"))
    mkt.control.next_exchange_seller = seller
    response = await callback(mapi, "flipkart", query)
    assert response.status_code == 409, response.text
    assert [r for r in oauth_rows(mkt.conn) if r["token_enc"] is not None] == []


@pytest.mark.parametrize(
    "state", ["é" * 600, "x" * 2048, "\U0001f600state", "a%00b"], ids=["e", "max", "emoji", "nul"]
)
async def test_attack_callback_hostile_state_is_409_not_500(
    mkt: MktWorld, mapi: httpx.AsyncClient, state: str
) -> None:
    response = await callback(mapi, "flipkart", {"state": state, "code": "c"})
    assert response.status_code in (409, 422), (response.status_code, response.text)


async def test_attack_callback_both_code_names_422(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    query = await consent(mkt, await connect(mapi, mkt, marketplace="flipkart"))
    query["spapi_oauth_code"] = query["code"]
    response = await callback(mapi, "flipkart", query)
    assert response.status_code == 422
    # the state was not consumed by the refused request: the honest retry still works
    del query["spapi_oauth_code"]
    pending_of(await callback(mapi, "flipkart", query))


# ---- role escalation on the read and the write ----


@pytest.mark.parametrize("email", ["operator@acme.test", "reviewer@acme.test"])
async def test_attack_non_admin_cannot_confirm_or_revoke(
    mkt: MktWorld, mapi: httpx.AsyncClient, email: str
) -> None:
    account = await connected_account(mapi, mkt, seller="A1ROLES")
    handle = await connect_to_pending(mapi, mkt, seller="A1ROLES")
    assert (await confirm(mapi, mkt, email, handle)).status_code == 403
    revoke = await mapi.post(
        f"{PREFIX}/marketplaces/accounts/{account['id']}/revoke", headers=as_member(mkt, email)
    )
    assert revoke.status_code == 403
    assert _row(mkt, account["id"])["status"] == "active"
    listed = await mapi.get(f"{PREFIX}/marketplaces/accounts", headers=as_member(mkt, email))
    assert listed.status_code == 200
    assert "token" not in json.dumps(listed.json()).lower()


# ---- a reconnect that lands while a refresh is in flight ----


async def test_attack_reconnect_mid_refresh_keeps_reconnect_tokens(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker unsealed the old pair and is at the token endpoint when the owner reconnects
    the same seller through the real confirm route: the refresh's write is stale (CAS on
    `updated_at`, the account stays `active`) and the reconnect's new tokens survive."""
    account = await connected_account(mapi, mkt, seller="A1RACE")
    _due(mkt, account["id"])
    handle = await connect_to_pending(mapi, mkt, seller="A1RACE")
    seen: dict[str, bytes] = {}

    class _ReconnectFirst(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.inner = httpx.ASGITransport(app=mkt.fake)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            confirmed = await confirm(mapi, mkt, "owner@acme.test", handle)
            assert confirmed.status_code == 200, confirmed.text
            seen["blob"] = bytes(_row(mkt, account["id"])["token_enc"])
            return await self.inner.handle_async_request(request)

    monkeypatch.setattr("app.adapters.oauth.http.make_transport", _ReconnectFirst)
    assert await _refresh(mkt, account["id"]) == "stale"
    row = _row(mkt, account["id"])
    assert row["status"] == "active"
    assert bytes(row["token_enc"]) == seen["blob"]
    assert row["last_refresh_at"] is None
