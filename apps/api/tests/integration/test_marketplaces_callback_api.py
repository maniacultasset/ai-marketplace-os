"""S15 T12 (DW1-2, AC2, AC4, AC11): `GET /api/v1/marketplaces/{marketplace}/callback` through the
real app as `app_rw`: the state is consumed once, the code exchanged against the fake marketplace,
the sealed tokens stored as a pending row in the state's tenant, and the browser sent on with only
an opaque handle."""

from __future__ import annotations

from typing import Any

import httpx
import psycopg
import pytest

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .marketplace_support import (
    DASHBOARD,
    MktWorld,
    account_rows,
    as_member,
    callback,
    connect,
    consent,
    digest,
    install_fake,
    mapi,
    mkt,
    oauth_rows,
    pending_of,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]


def _pending_rows(mw: MktWorld) -> list[dict[str, Any]]:
    return [r for r in oauth_rows(mw.conn) if r["token_enc"] is not None]


def _state_rows(mw: MktWorld) -> list[dict[str, Any]]:
    return [r for r in oauth_rows(mw.conn) if r["token_enc"] is None]


async def _consented(
    api: httpx.AsyncClient, mw: MktWorld, marketplace: str = "amazon", seller: str | None = None
) -> dict[str, str]:
    return await consent(mw, await connect(api, mw, marketplace=marketplace), seller)


async def test_oauth_connect_flow(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    """DW1: connect -> fake consent -> callback -> 303; one sealed pending row for acme, no
    account yet, and `Location` carries only `pending`."""
    query = await _consented(mapi, mkt, seller="A3FLOWSELLER")
    response = await callback(mapi, "amazon", query)
    handle = pending_of(response)
    assert response.headers["location"] == f"{DASHBOARD}/marketplaces/connected?pending={handle}"
    assert len(handle) == 43
    rows = _pending_rows(mkt)
    assert len(rows) == 1 and _state_rows(mkt) == []
    row = rows[0]
    assert row["tenant_id"] == mkt.tenant("acme")
    assert row["user_id"] == mkt.user("owner@acme.test")
    assert bytes(row["state_hash"]) == digest(handle)
    assert row["seller_id"] == "A3FLOWSELLER" and row["kms_key_version"] == "local-1"
    assert bytes(row["token_enc"]).startswith(b"MKT1")
    assert b"CANARYzq9" not in bytes(row["token_enc"])
    assert row["code_verifier"] is None and row["token_expires_at"] is not None
    assert account_rows(mkt.conn) == []


@pytest.mark.endpoint("marketplaces_callback", case="success")
@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
async def test_callback_success(mkt: MktWorld, mapi: httpx.AsyncClient, marketplace: str) -> None:
    query = await _consented(mapi, mkt, marketplace)
    pending_of(await callback(mapi, marketplace, query))
    rows = _pending_rows(mkt)
    assert [r["marketplace"] for r in rows] == [marketplace]
    assert rows[0]["seller_id"] == "A1FAKESELLER"


async def test_callback_headers_no_store_no_referrer(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    response = await callback(mapi, "amazon", await _consented(mapi, mkt))
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.content == b""


@pytest.mark.endpoint("marketplaces_callback", case="validation")
@pytest.mark.parametrize(
    "params",
    [
        {"spapi_oauth_code": "c", "selling_partner_id": "A1"},
        {"state": "s"},
        {"state": "s", "code": "c", "spapi_oauth_code": "c"},
        {"state": "s", "code": "c" * 2049},
        {"state": "s", "spapi_oauth_code": "c", "selling_partner_id": "A1/../B"},
        {"state": "s", "error": "access_denied"},
    ],
    ids=["no-state", "neither-code", "both-codes", "2049", "bad-seller", "denied"],
)
async def test_callback_validation_422(
    mkt: MktWorld, mapi: httpx.AsyncClient, params: dict[str, str]
) -> None:
    await connect(mapi, mkt)
    response = await callback(mapi, "amazon", params)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert len(_state_rows(mkt)) == 1


async def test_callback_unknown_marketplace_path_422(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    response = await callback(mapi, "ebay", {"state": "s", "code": "c"})
    assert response.status_code == 422


def _assert_bad_state(response: httpx.Response, mw: MktWorld) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "OAUTH_STATE_INVALID"
    assert _pending_rows(mw) == []
    assert "location" not in response.headers


@pytest.mark.endpoint("marketplaces_callback", case="bad_state", variant="replayed")
async def test_oauth_state_replayed(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    query = await _consented(mapi, mkt)
    pending_of(await callback(mapi, "amazon", query))
    mkt.conn.execute("DELETE FROM oauth_states")
    _assert_bad_state(await callback(mapi, "amazon", query), mkt)


@pytest.mark.endpoint("marketplaces_callback", case="bad_state", variant="expired")
async def test_oauth_state_expired(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    query = await _consented(mapi, mkt)
    mkt.conn.execute("UPDATE oauth_states SET expires_at = now() - interval '1 second'")
    _assert_bad_state(await callback(mapi, "amazon", query), mkt)


@pytest.mark.endpoint("marketplaces_callback", case="bad_state", variant="forged")
async def test_oauth_state_unknown(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    query = await _consented(mapi, mkt)
    query["state"] = "forged-" + query["state"][7:]
    _assert_bad_state(await callback(mapi, "amazon", query), mkt)
    assert len(_state_rows(mkt)) == 1


@pytest.mark.endpoint("marketplaces_callback", case="bad_state", variant="marketplace_mismatch")
async def test_oauth_state_marketplace_mismatch(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    query = await _consented(mapi, mkt, "amazon")
    flipkart_shaped = {"state": query["state"], "code": query["spapi_oauth_code"]}
    _assert_bad_state(await callback(mapi, "flipkart", flipkart_shaped), mkt)
    assert len(_state_rows(mkt)) == 1  # the amazon state is not burnt by the mismatch


async def test_oauth_state_pending_handle_as_state_409(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    query = await _consented(mapi, mkt)
    handle = pending_of(await callback(mapi, "amazon", query))
    forged = {**query, "state": handle}
    response = await callback(mapi, "amazon", forged)
    assert response.status_code == 409
    assert len(_pending_rows(mkt)) == 1  # the pending row is untouched


@pytest.mark.endpoint("marketplaces_callback", case="wrong_tenant")
async def test_oauth_state_acme_cannot_bind_bharat(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    """DW2: the callback carries bharat's credentials; the pending row is acme's, whose owner
    started the flow (the tenant comes only from the consumed state row)."""
    query = await _consented(mapi, mkt)
    response = await callback(
        mapi, "amazon", query, **as_member(mkt, "owner@bharat.test"), Cookie="tid=bharat"
    )
    pending_of(response)
    rows = _pending_rows(mkt)
    assert [r["tenant_id"] for r in rows] == [mkt.tenant("acme")]
    assert rows[0]["user_id"] == mkt.user("owner@acme.test")


async def test_callback_exchange_5xx_unavailable_no_pending(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    query = await _consented(mapi, mkt)
    mkt.control.next_exchange = "server_error"
    response = await callback(mapi, "amazon", query)
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "MARKETPLACE_UNAVAILABLE"
    assert oauth_rows(mkt.conn) == []  # the state is burnt; the user restarts the connect
    retry = await callback(mapi, "amazon", query)
    assert retry.status_code == 409


async def test_callback_exchange_rejected_409(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    query = await _consented(mapi, mkt)
    mkt.control.next_exchange = "invalid_grant"
    _assert_bad_state(await callback(mapi, "amazon", query), mkt)


async def test_callback_amazon_without_seller_param(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    query = await _consented(mapi, mkt)
    del query["selling_partner_id"]
    response = await callback(mapi, "amazon", query)
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "MARKETPLACE_UNKNOWN_ERROR"
    assert oauth_rows(mkt.conn) == []


@pytest.mark.parametrize(
    "seller", ["A1/B", "..", "A1 B", "A" * 129], ids=["slash", "dotdot", "space", "129"]
)
async def test_callback_seller_id_pattern(
    mkt: MktWorld, mapi: httpx.AsyncClient, seller: str
) -> None:
    """D4: an Amazon seller param outside the pattern is 422 and the state row is kept; a
    Flipkart token reply naming such a seller is 409 and nothing is sealed."""
    amazon = await _consented(mapi, mkt, "amazon", seller=None)
    amazon["selling_partner_id"] = seller
    response = await callback(mapi, "amazon", amazon)
    assert response.status_code == 422
    assert len(_state_rows(mkt)) == 1
    flipkart = await _consented(mapi, mkt, "flipkart")
    mkt.control.next_exchange_seller = seller
    _assert_bad_state(await callback(mapi, "flipkart", flipkart), mkt)


@pytest.mark.xfail(strict=True, reason="DW9: verify the seller id with a marketplace call")
async def test_amazon_seller_param_mismatch(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    query = await _consented(mapi, mkt, "amazon", seller="A1CONSENTED")
    query["selling_partner_id"] = "A2SWAPPED"
    response = await callback(mapi, "amazon", query)
    assert response.status_code == 409


async def test_callback_unconfigured_501(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    query = await _consented(mapi, mkt)
    monkeypatch.delenv("AMAZON_AUTHORIZE_URL")
    get_settings.cache_clear()
    response = await callback(mapi, "amazon", query)
    assert response.status_code == 501
    assert len(_state_rows(mkt)) == 1


async def test_callback_location_from_settings_only(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    query = await _consented(mapi, mkt)
    query.update({"redirect": "https://evil.example/", "next": "https://evil.example/"})
    response = await mapi.get(
        f"{PREFIX}/marketplaces/amazon/callback",
        params=query,
        headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"},
    )
    handle = pending_of(response)
    assert response.headers["location"] == f"{DASHBOARD}/marketplaces/connected?pending={handle}"
    assert "evil" not in response.headers["location"]


class _Probe(httpx.AsyncBaseTransport):
    """Wraps the fake: at the token call, read pg_stat_activity for open app_rw transactions."""

    def __init__(self, fake: Any, admin: psycopg.Connection[tuple[Any, ...]]) -> None:
        self.inner = httpx.ASGITransport(app=fake)
        self.admin = admin
        self.seen: list[int] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            row = self.admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_rw' "
                "AND datname = current_database() AND state LIKE 'idle in transaction%'"
            ).fetchone()
            self.seen.append(int(row[0]) if row else -1)
        return await self.inner.handle_async_request(request)


async def test_callback_no_transaction_during_exchange(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = await _consented(mapi, mkt)
    probe = _Probe(mkt.fake, mkt.conn)
    monkeypatch.setattr("app.adapters.oauth.http.make_transport", lambda: probe)
    pending_of(await callback(mapi, "amazon", query))
    assert probe.seen == [0]
    install_fake(monkeypatch, mkt.fake)
