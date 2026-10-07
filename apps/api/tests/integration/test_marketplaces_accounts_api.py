"""S15 T14 (AC9-10): `GET /api/v1/marketplaces/accounts` through the real app as `app_rw`: the
tenant's accounts, every status (the connection health), keyset pages, no pending row and no
credential field."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .jobs_support import bad_token
from .marketplace_support import (
    MktWorld,
    as_member,
    connect_to_pending,
    connected_account,
    mapi,
    mkt,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]
URL = f"{PREFIX}/marketplaces/accounts"


async def _list(
    api: httpx.AsyncClient, mw: MktWorld, email: str = "owner@acme.test", **params: Any
) -> httpx.Response:
    return await api.get(URL, params=params, headers=as_member(mw, email))


def _revoke_sql(mw: MktWorld, account_id: str) -> None:
    mw.conn.execute(
        "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
        "kms_key_version = NULL, needs_reconnect_since = NULL, revoked_at = now() WHERE id = %s",
        [account_id],
    )


@pytest.mark.endpoint("marketplaces_accounts", case="success")
@pytest.mark.parametrize("role", ["owner", "operator", "reviewer", "admin"])
async def test_accounts_list_success(mkt: MktWorld, mapi: httpx.AsyncClient, role: str) -> None:
    account = await connected_account(mapi, mkt, seller="A1LIST")
    response = await _list(mapi, mkt, f"{role}@acme.test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [a["id"] for a in body["data"]] == [account["id"]]
    assert body["page"] == {"next_cursor": None, "has_more": False, "limit": 50}


@pytest.mark.endpoint("marketplaces_accounts", case="wrong_tenant")
async def test_accounts_list_own_tenant_only(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    await connected_account(mapi, mkt, "owner@acme.test", seller="A1ACME")
    theirs = await connected_account(mapi, mkt, "owner@bharat.test", seller="B1BHARAT")
    body = (await _list(mapi, mkt, "owner@bharat.test")).json()
    assert [a["id"] for a in body["data"]] == [theirs["id"]]
    assert "A1ACME" not in str(body)


async def test_accounts_pending_never_listed(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    await connect_to_pending(mapi, mkt)
    assert (await _list(mapi, mkt)).json()["data"] == []


async def test_accounts_keyset_two_pages(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    first = await connected_account(mapi, mkt, seller="A1ONE")
    _revoke_sql(mkt, first["id"])
    second = await connected_account(mapi, mkt, seller="A1TWO")
    third = await connected_account(mapi, mkt, marketplace="flipkart", seller="F1THREE")
    page1 = (await _list(mapi, mkt, limit=2)).json()
    assert page1["page"]["has_more"] is True and len(page1["data"]) == 2
    page2 = (await _list(mapi, mkt, limit=2, cursor=page1["page"]["next_cursor"])).json()
    assert page2["page"]["has_more"] is False
    ids = [a["id"] for a in [*page1["data"], *page2["data"]]]
    assert ids == [third["id"], second["id"], first["id"]]


@pytest.mark.parametrize("status", ["active", "needs_reconnect", "revoked"])
async def test_accounts_status_is_health(
    mkt: MktWorld, mapi: httpx.AsyncClient, status: str
) -> None:
    account = await connected_account(mapi, mkt)
    if status == "needs_reconnect":
        mkt.conn.execute(
            "UPDATE marketplace_accounts SET status = 'needs_reconnect', "
            "needs_reconnect_since = now() WHERE id = %s",
            [account["id"]],
        )
    if status == "revoked":
        _revoke_sql(mkt, account["id"])
    [listed] = (await _list(mapi, mkt)).json()["data"]
    assert listed["status"] == status
    assert (listed["needs_reconnect_since"] is not None) == (status == "needs_reconnect")


async def test_accounts_no_secret_keys_in_body(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    await connected_account(mapi, mkt)
    response = await _list(mapi, mkt)
    [listed] = response.json()["data"]
    for key in listed:
        for word in ("token", "secret", "enc", "verifier", "hash", "kms", "scope"):
            assert word not in key, key
    assert "CANARYzq9" not in response.text and "MKT1" not in response.text


@pytest.mark.endpoint("marketplaces_accounts", case="validation")
@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"cursor": "not-a-cursor"}])
async def test_accounts_validation_422(
    mkt: MktWorld, mapi: httpx.AsyncClient, params: dict[str, Any]
) -> None:
    assert (await _list(mapi, mkt, **params)).status_code == 422


@pytest.mark.endpoint("marketplaces_accounts", case="missing_auth")
async def test_accounts_missing_auth_401(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    assert (await mapi.get(URL)).status_code == 401


@pytest.mark.endpoint("marketplaces_accounts", case="invalid_token", variant="expired")
@pytest.mark.endpoint("marketplaces_accounts", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("marketplaces_accounts", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_accounts_invalid_token_401(
    mkt: MktWorld, mapi: httpx.AsyncClient, variant: str
) -> None:
    response = await mapi.get(URL, headers=bad_token(mkt, variant))  # type: ignore[arg-type]
    assert response.status_code == 401
