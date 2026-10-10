"""S22 T16 (AC14; §3.1): the Flipkart connect, callback and confirm case matrix with
`FLIPKART_ENABLED=true` (the S15 test world turns it on), against the in-process fake (ASGI, no
network). The flag-off column is `test_flipkart_flag_off.py`."""

from __future__ import annotations

import httpx
import pytest

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .marketplace_support import (
    MktWorld,
    account_rows,
    as_member,
    callback,
    confirm,
    connect,
    connect_to_pending,
    consent,
    mapi,
    mkt,
    oauth_rows,
    pending_of,
    query_of,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]
OWNER = "owner@acme.test"


def _code(response: httpx.Response) -> str:
    return str(response.json()["error"]["type"])


# ---- marketplaces_connect ----


@pytest.mark.endpoint("marketplaces_connect", case="success")
async def test_marketplaces_connect_flipkart_success(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    url = await connect(mapi, mkt, OWNER, "flipkart")
    query = query_of(url)
    assert url.startswith("http://127.0.0.1:4020/flipkart/authorize?")
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    (row,) = oauth_rows(mkt.conn)
    assert row["marketplace"] == "flipkart" and row["tenant_id"] == mkt.tenant("acme")


@pytest.mark.endpoint("marketplaces_connect", case="validation")
@pytest.mark.parametrize(
    "body", [{"marketplace": "ebay"}, {"marketplace": "flipkart", "extra": 1}, {}]
)
async def test_marketplaces_connect_flipkart_validation(
    mkt: MktWorld, mapi: httpx.AsyncClient, body: dict[str, object]
) -> None:
    response = await mapi.post(
        f"{PREFIX}/marketplaces/connect", json=body, headers=as_member(mkt, OWNER)
    )
    assert response.status_code == 422, response.text
    assert oauth_rows(mkt.conn) == []


@pytest.mark.endpoint("marketplaces_connect", case="missing_auth")
async def test_marketplaces_connect_flipkart_missing_auth(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    response = await mapi.post(f"{PREFIX}/marketplaces/connect", json={"marketplace": "flipkart"})
    assert response.status_code == 401 and oauth_rows(mkt.conn) == []


@pytest.mark.endpoint("marketplaces_connect", case="wrong_role")
@pytest.mark.parametrize("email", ["operator@acme.test", "reviewer@acme.test"])
async def test_marketplaces_connect_flipkart_wrong_role(
    mkt: MktWorld, mapi: httpx.AsyncClient, email: str
) -> None:
    response = await mapi.post(
        f"{PREFIX}/marketplaces/connect",
        json={"marketplace": "flipkart"},
        headers=as_member(mkt, email),
    )
    assert response.status_code == 403 and oauth_rows(mkt.conn) == []


@pytest.mark.endpoint("marketplaces_connect", case="wrong_tenant")
async def test_marketplaces_connect_flipkart_wrong_tenant(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """bharat's connect writes bharat's state only."""
    await connect(mapi, mkt, "owner@bharat.test", "flipkart")
    (row,) = oauth_rows(mkt.conn)
    assert row["tenant_id"] == mkt.tenant("bharat")


# ---- marketplaces_callback ----


@pytest.mark.endpoint("marketplaces_callback", case="success")
async def test_marketplaces_callback_flipkart_success(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    url = await connect(mapi, mkt, OWNER, "flipkart")
    response = await callback(mapi, "flipkart", await consent(mkt, url, "F1CB"))
    pending_of(response)
    (row,) = oauth_rows(mkt.conn)
    assert row["token_enc"] is not None and row["seller_id"] == "F1CB"
    (request,) = [r for r in mkt.control.token_requests if r["grant_type"] == "authorization_code"]
    assert request["code_verifier"]  # PKCE


@pytest.mark.endpoint("marketplaces_callback", case="validation")
@pytest.mark.parametrize(
    "query", [{"code": "c"}, {"state": "s", "code": "a", "spapi_oauth_code": "b"}]
)
async def test_marketplaces_callback_flipkart_validation(
    mkt: MktWorld, mapi: httpx.AsyncClient, query: dict[str, str]
) -> None:
    assert (await callback(mapi, "flipkart", query)).status_code == 422


@pytest.mark.endpoint("marketplaces_callback", case="bad_state")
async def test_marketplaces_callback_flipkart_replayed_state(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    url = await connect(mapi, mkt, OWNER, "flipkart")
    query = await consent(mkt, url)
    pending_of(await callback(mapi, "flipkart", query))
    again = await callback(mapi, "flipkart", query)
    assert again.status_code == 409 and _code(again) == "OAUTH_STATE_INVALID"


async def test_marketplaces_callback_flipkart_state_of_amazon_refused(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """A state started for Amazon never completes on the Flipkart callback (no exchange)."""
    url = await connect(mapi, mkt, OWNER, "amazon")
    state = query_of(url)["state"]
    response = await callback(mapi, "flipkart", {"state": state, "code": "x"})
    assert response.status_code == 409 and mkt.control.exchanges == 0


# ---- marketplaces_connect_confirm ----


@pytest.mark.endpoint("marketplaces_connect_confirm", case="success")
async def test_marketplaces_connect_confirm_flipkart_success(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    handle = await connect_to_pending(mapi, mkt, OWNER, "flipkart", "F1OK")
    response = await confirm(mapi, mkt, OWNER, handle)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["marketplace"], body["seller_id"], body["status"]) == (
        "flipkart",
        "F1OK",
        "active",
    )
    (row,) = account_rows(mkt.conn)
    assert row["tenant_id"] == mkt.tenant("acme") and row["token_enc"] is not None


@pytest.mark.endpoint("marketplaces_connect_confirm", case="validation")
async def test_marketplaces_connect_confirm_flipkart_validation(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    response = await confirm(mapi, mkt, OWNER, "not a handle")
    assert response.status_code == 422


@pytest.mark.endpoint("marketplaces_connect_confirm", case="missing_auth")
async def test_marketplaces_connect_confirm_flipkart_missing_auth(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    handle = await connect_to_pending(mapi, mkt, OWNER, "flipkart")
    response = await mapi.post(f"{PREFIX}/marketplaces/connect/confirm", json={"pending": handle})
    assert response.status_code == 401 and account_rows(mkt.conn) == []


@pytest.mark.endpoint("marketplaces_connect_confirm", case="wrong_role")
@pytest.mark.parametrize("email", ["operator@acme.test", "reviewer@acme.test"])
async def test_marketplaces_connect_confirm_flipkart_wrong_role(
    mkt: MktWorld, mapi: httpx.AsyncClient, email: str
) -> None:
    handle = await connect_to_pending(mapi, mkt, OWNER, "flipkart")
    assert (await confirm(mapi, mkt, email, handle)).status_code == 403
    assert account_rows(mkt.conn) == []


@pytest.mark.endpoint("marketplaces_connect_confirm", case="wrong_tenant")
async def test_marketplaces_connect_confirm_flipkart_wrong_tenant(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """bharat names acme's handle: the uniform 409, nothing joins bharat, the handle survives."""
    handle = await connect_to_pending(mapi, mkt, OWNER, "flipkart")
    response = await confirm(mapi, mkt, "owner@bharat.test", handle)
    assert response.status_code == 409 and _code(response) == "OAUTH_STATE_INVALID"
    assert account_rows(mkt.conn) == []
    assert (await confirm(mapi, mkt, OWNER, handle)).status_code == 200


@pytest.mark.endpoint("marketplaces_connect_confirm", case="conflict")
async def test_marketplaces_connect_confirm_flipkart_other_seller_conflict(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    first = await connect_to_pending(mapi, mkt, OWNER, "flipkart", "F1FIRST")
    assert (await confirm(mapi, mkt, OWNER, first)).status_code == 200
    second = await connect_to_pending(mapi, mkt, OWNER, "flipkart", "F1OTHER")
    response = await confirm(mapi, mkt, OWNER, second)
    assert response.status_code == 409 and _code(response) == "MARKETPLACE_ACCOUNT_CONFLICT"
    assert [r["seller_id"] for r in account_rows(mkt.conn)] == ["F1FIRST"]


@pytest.mark.endpoint("marketplaces_connect_confirm", case="bad_state")
async def test_marketplaces_connect_confirm_flipkart_used_handle(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    handle = await connect_to_pending(mapi, mkt, OWNER, "flipkart")
    assert (await confirm(mapi, mkt, OWNER, handle)).status_code == 200
    again = await confirm(mapi, mkt, OWNER, handle)
    assert again.status_code == 409 and _code(again) == "OAUTH_STATE_INVALID"
