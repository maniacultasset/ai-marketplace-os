"""S15 T11 (AC1, AC11): `POST /api/v1/marketplaces/connect` through the real app as `app_rw`: the
consent URL per marketplace, the stored state row (its hash, never the state), the TTL, the
no-store header, the RBAC matrix and the 501 for an unconfigured marketplace."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .jobs_support import bad_token
from .marketplace_support import (
    CFG,
    FAKE_BASE,
    MktWorld,
    as_member,
    connect,
    digest,
    mapi,
    mkt,
    oauth_rows,
    query_of,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]
URL = f"{PREFIX}/marketplaces/connect"


async def _post(api: httpx.AsyncClient, headers: dict[str, str], body: Any) -> httpx.Response:
    return await api.post(URL, json=body, headers=headers)


@pytest.mark.endpoint("marketplaces_connect", case="success")
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_connect_success(mkt: MktWorld, mapi: httpx.AsyncClient, role: str) -> None:
    url = await connect(mapi, mkt, f"{role}@acme.test", "amazon")
    assert url.startswith(f"{FAKE_BASE}/amazon/authorize?")
    rows = oauth_rows(mkt.conn)
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == mkt.tenant("acme")
    assert rows[0]["user_id"] == mkt.user(f"{role}@acme.test")
    assert rows[0]["token_enc"] is None


async def test_connect_authorize_url_params(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    amazon = query_of(await connect(mapi, mkt, marketplace="amazon"))
    assert set(amazon) == {"application_id", "redirect_uri", "state"}
    assert amazon["application_id"] == CFG.amazon_application_id
    assert amazon["redirect_uri"] == CFG.amazon.redirect_uri
    flipkart = query_of(await connect(mapi, mkt, marketplace="flipkart"))
    assert set(flipkart) == {
        "client_id",
        "redirect_uri",
        "response_type",
        "state",
        "code_challenge",
        "code_challenge_method",
    }
    assert flipkart["code_challenge_method"] == "S256"


async def test_connect_row_holds_hash_not_state(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    state = query_of(await connect(mapi, mkt))["state"]
    row = oauth_rows(mkt.conn)[0]
    assert bytes(row["state_hash"]) == digest(state)
    assert len(bytes(row["state_hash"])) == 32
    assert state.encode() not in bytes(row["state_hash"])
    assert len(state) == 43


async def test_connect_ttl_600(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    await connect(mapi, mkt)
    row = mkt.conn.execute(
        "SELECT extract(epoch FROM expires_at - created_at) FROM oauth_states"
    ).fetchone()
    assert row is not None and float(row[0]) == 600.0


async def test_connect_no_store_header(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    response = await _post(mapi, as_member(mkt, "owner@acme.test"), {"marketplace": "amazon"})
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.endpoint("marketplaces_connect", case="validation")
@pytest.mark.parametrize(
    "body", [{"marketplace": "ebay"}, {"marketplace": "amazon", "tenant_id": "x"}, {}]
)
async def test_connect_validation_422(
    mkt: MktWorld, mapi: httpx.AsyncClient, body: dict[str, str]
) -> None:
    response = await _post(mapi, as_member(mkt, "owner@acme.test"), body)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert oauth_rows(mkt.conn) == []


@pytest.mark.endpoint("marketplaces_connect", case="missing_auth")
async def test_connect_missing_auth_401(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    response = await mapi.post(URL, json={"marketplace": "amazon"})
    assert response.status_code == 401
    assert oauth_rows(mkt.conn) == []


@pytest.mark.endpoint("marketplaces_connect", case="invalid_token", variant="expired")
@pytest.mark.endpoint("marketplaces_connect", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("marketplaces_connect", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_connect_invalid_token_401(
    mkt: MktWorld, mapi: httpx.AsyncClient, variant: str
) -> None:
    response = await _post(mapi, bad_token(mkt, variant), {"marketplace": "amazon"})  # type: ignore[arg-type]
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"


@pytest.mark.endpoint("marketplaces_connect", case="wrong_role")
@pytest.mark.parametrize("role", ["operator", "reviewer"])
async def test_connect_wrong_role_403(mkt: MktWorld, mapi: httpx.AsyncClient, role: str) -> None:
    response = await _post(mapi, as_member(mkt, f"{role}@acme.test"), {"marketplace": "amazon"})
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert oauth_rows(mkt.conn) == []


@pytest.mark.endpoint("marketplaces_connect", case="wrong_tenant")
async def test_connect_wrong_tenant_binds_own_tenant(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    await connect(mapi, mkt, "owner@bharat.test")
    rows = oauth_rows(mkt.conn)
    assert [r["tenant_id"] for r in rows] == [mkt.tenant("bharat")]


async def test_connect_unconfigured_501_no_row(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.delenv("FLIPKART_AUTHORIZE_URL")
    get_settings.cache_clear()
    response = await _post(mapi, as_member(mkt, "owner@acme.test"), {"marketplace": "flipkart"})
    assert response.status_code == 501
    assert response.json()["error"]["type"] == "NOT_IMPLEMENTED"
    assert oauth_rows(mkt.conn) == []


async def test_connect_amazon_no_app_id_501_no_row(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("AMAZON_APP_ID", "")
    get_settings.cache_clear()
    response = await _post(mapi, as_member(mkt, "owner@acme.test"), {"marketplace": "amazon"})
    assert response.status_code == 501
    assert oauth_rows(mkt.conn) == []


async def test_connect_amazon_state_verifier_null(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    params = query_of(await connect(mapi, mkt, marketplace="amazon"))
    assert "code_challenge" not in params and "client_id" not in params
    assert oauth_rows(mkt.conn)[0]["code_verifier"] is None


async def test_connect_flipkart_state_has_verifier(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    from app.services.marketplaces import s256_challenge

    params = query_of(await connect(mapi, mkt, marketplace="flipkart"))
    verifier = oauth_rows(mkt.conn)[0]["code_verifier"]
    assert verifier is not None and len(verifier) == 64
    assert params["code_challenge"] == s256_challenge(verifier)


async def test_connect_two_calls_two_states(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    one = query_of(await connect(mapi, mkt))["state"]
    two = query_of(await connect(mapi, mkt))["state"]
    assert one != two
    assert len(oauth_rows(mkt.conn)) == 2
