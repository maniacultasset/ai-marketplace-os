"""S20 T15 (AC10, AC12, AC13; C15, C17): `open_adapter`, as app_rw against the in-process fake
marketplace (LWA and SP-API over ASGI, no network)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from tests.adapters.amazon_support import amazon_family, amazon_item
from tests.conftest import LogCapture
from tests.support.fake_marketplace import CANARY

from .auth_support import audit_rows
from .conftest import LOCAL_HOSTS
from .marketplace_support import MktWorld, as_member, connected_account, mapi, mkt

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]
FAKE_SPAPI = "http://127.0.0.1:4020"


@pytest.fixture
def spapi_env(mkt: MktWorld, monkeypatch: pytest.MonkeyPatch) -> MktWorld:
    from app.core.config import get_settings

    monkeypatch.setenv("AMAZON_SPAPI_ENDPOINT", FAKE_SPAPI)
    monkeypatch.setenv("AMAZON_USE_SANDBOX", "true")
    get_settings.cache_clear()
    return mkt


async def _open(mw: MktWorld, account_id: str, slug: str = "acme", transport: Any = None) -> Any:
    from app.workers.adapters import open_adapter

    return await open_adapter(
        mw.tenant(slug),
        uuid.UUID(account_id),
        transport=transport or httpx.ASGITransport(app=mw.fake),
    )


def _used(mw: MktWorld) -> list[tuple[Any, ...]]:
    return audit_rows(mw.conn, "marketplace_account.credential_used")


async def test_open_adapter_wrong_tenant(spapi_env: MktWorld, mapi: httpx.AsyncClient) -> None:
    from app.workers.adapters import AccountNotFound

    account = await connected_account(mapi, spapi_env)
    with pytest.raises(AccountNotFound):
        await _open(spapi_env, account["id"], slug="bharat")
    with pytest.raises(AccountNotFound):
        await _open(spapi_env, str(uuid.uuid4()))


@pytest.mark.parametrize("status", ["revoked", "needs_reconnect"])
async def test_open_adapter_not_active(
    spapi_env: MktWorld, mapi: httpx.AsyncClient, status: str
) -> None:
    from app.adapters.base import AuthExpired

    account = await connected_account(mapi, spapi_env)
    if status == "revoked":
        response = await mapi.post(
            f"/api/v1/marketplaces/accounts/{account['id']}/revoke",
            headers=as_member(spapi_env, "owner@acme.test"),
        )
        assert response.status_code == 200, response.text
    else:
        spapi_env.conn.execute(
            "UPDATE marketplace_accounts SET status = 'needs_reconnect', "
            "needs_reconnect_since = now() WHERE id = %s",
            [account["id"]],
        )
    with pytest.raises(AuthExpired):
        await _open(spapi_env, account["id"])
    assert spapi_env.control.calls == [] and _used(spapi_env) == []


async def test_open_adapter_create_end_to_end_seller_from_row(
    spapi_env: MktWorld, mapi: httpx.AsyncClient
) -> None:
    account = await connected_account(mapi, spapi_env, seller="A1ROWSELLER")
    adapter = await _open(spapi_env, account["id"])
    result = await adapter.create_listing(amazon_item())
    await adapter.aclose()
    assert result.status == "published"
    (call,) = spapi_env.control.calls
    assert call["raw_path"].startswith("/listings/2021-08-01/items/A1ROWSELLER/")
    assert next(iter(spapi_env.control.spapi_items))[0] == "A1ROWSELLER"


async def test_open_adapter_credential_used_once(
    spapi_env: MktWorld, mapi: httpx.AsyncClient
) -> None:
    account = await connected_account(mapi, spapi_env)
    adapter = await _open(spapi_env, account["id"])
    await adapter.create_listing(amazon_family())
    assert len(spapi_env.control.calls) == 5
    (used,) = _used(spapi_env)
    assert used[9] == "marketplace_call"


async def test_open_adapter_credential_used_twice_with_refresh(
    spapi_env: MktWorld, mapi: httpx.AsyncClient
) -> None:
    account = await connected_account(mapi, spapi_env)
    spapi_env.control.spapi_auth_mode, spapi_env.control.spapi_auth_times = "expired", 1
    adapter = await _open(spapi_env, account["id"])
    result = await adapter.create_listing(amazon_item())
    assert result.status == "published"
    assert spapi_env.control.refreshes == 1
    assert sorted(u[9] for u in _used(spapi_env)) == ["marketplace_call", "token_refresh"]


async def test_open_adapter_refresh_rejected_auth_expired(
    spapi_env: MktWorld, mapi: httpx.AsyncClient
) -> None:
    from app.adapters.base import AuthExpired

    account = await connected_account(mapi, spapi_env)
    spapi_env.control.spapi_auth_mode, spapi_env.control.spapi_auth_times = "expired", 1
    spapi_env.control.next_refresh = "invalid_grant"
    adapter = await _open(spapi_env, account["id"])
    with pytest.raises(AuthExpired):
        await adapter.create_listing(amazon_item())
    row = spapi_env.conn.execute(
        "SELECT status FROM marketplace_accounts WHERE id = %s", [account["id"]]
    ).fetchone()
    assert row == ("needs_reconnect",)


async def test_open_adapter_tokens_never_leak(
    spapi_env: MktWorld, mapi: httpx.AsyncClient, log_lines: Callable[..., LogCapture]
) -> None:
    account = await connected_account(mapi, spapi_env)
    capture = log_lines()
    spapi_env.control.spapi_auth_mode, spapi_env.control.spapi_auth_times = "expired", 1
    adapter = await _open(spapi_env, account["id"])
    result = await adapter.create_listing(amazon_item())
    assert spapi_env.control.access_tokens and all(t.startswith(CANARY) for t in spapi_env.control.access_tokens)  # fmt: skip
    for text in (capture.text(), result.model_dump_json(), repr(adapter)):
        assert CANARY not in text


class _TxProbe(httpx.AsyncBaseTransport):
    def __init__(self, mw: MktWorld) -> None:
        self.inner = httpx.ASGITransport(app=mw.fake)
        self.conn = mw.conn
        self.seen: list[int] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        row = self.conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_rw' "
            "AND datname = current_database() AND state LIKE 'idle in transaction%'"
        ).fetchone()
        self.seen.append(int(row[0]) if row else -1)
        return await self.inner.handle_async_request(request)


async def test_open_adapter_no_transaction_during_http(
    spapi_env: MktWorld, mapi: httpx.AsyncClient
) -> None:
    account = await connected_account(mapi, spapi_env)
    probe = _TxProbe(spapi_env)
    adapter = await _open(spapi_env, account["id"], transport=probe)
    await adapter.create_listing(amazon_family())
    assert probe.seen == [0] * 5


async def test_open_adapter_flipkart_not_implemented(
    spapi_env: MktWorld, mapi: httpx.AsyncClient
) -> None:
    account = await connected_account(mapi, spapi_env, marketplace="flipkart")
    with pytest.raises(NotImplementedError):
        await _open(spapi_env, account["id"])


async def test_open_adapter_endpoint_mismatch_refused_on_local(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `.env.example` pair (production host, sandbox true) on local: refused before any
    unseal or call."""
    from app.adapters.base import MarketplaceUnavailable
    from app.core.config import get_settings

    monkeypatch.setenv("AMAZON_SPAPI_ENDPOINT", "https://sellingpartnerapi-eu.amazon.com")
    monkeypatch.setenv("AMAZON_USE_SANDBOX", "true")
    get_settings.cache_clear()
    account = await connected_account(mapi, mkt)
    with pytest.raises(MarketplaceUnavailable) as caught:
        await _open(mkt, account["id"])
    message = caught.value.report.errors[0].message
    assert "AMAZON_SPAPI_ENDPOINT" in message and "amazon.com" not in message
    assert mkt.control.calls == [] and _used(mkt) == []
