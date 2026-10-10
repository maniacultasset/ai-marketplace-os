"""S22 T4 (AC3; D5; C5, C10): with `FLIPKART_ENABLED=false` the token refresh leaves Flipkart
accounts alone (nothing unsealed, nothing sent, the status kept); revoke and the accounts list
still work. With the flag on, a token URL off the pin never receives a refresh token."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .auth_support import PREFIX, audit_rows
from .conftest import LOCAL_HOSTS
from .marketplace_support import MktWorld, account_rows, as_member, connected_account, mapi, mkt

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]
OWNER = "owner@acme.test"


def _flag(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("FLIPKART_ENABLED", "true" if on else "false")
    get_settings.cache_clear()


def _due(mw: MktWorld, account_id: str) -> None:
    mw.conn.execute(
        "UPDATE marketplace_accounts SET expires_at = now() + interval '1 minute' WHERE id = %s",
        [account_id],
    )


def _row(mw: MktWorld, account_id: str) -> dict[str, Any]:
    return next(r for r in account_rows(mw.conn) if str(r["id"]) == account_id)


def _used(mw: MktWorld) -> list[tuple[Any, ...]]:
    return audit_rows(mw.conn, "marketplace_account.credential_used")


async def _refresh(mw: MktWorld, account_id: str) -> str:
    from app.workers.token_refresh import refresh_account

    return await refresh_account(mw.tenant("acme"), uuid.UUID(account_id))


@pytest.fixture
async def flipkart_due(mkt: MktWorld, mapi: httpx.AsyncClient) -> dict[str, Any]:
    account = await connected_account(mapi, mkt, OWNER, "flipkart", seller="F1DUE")
    _due(mkt, account["id"])
    mkt.control.token_requests.clear()
    return account


async def test_flipkart_flag_off_refresh_skipped(
    mkt: MktWorld, flipkart_due: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _row(mkt, flipkart_due["id"])
    _flag(monkeypatch, False)
    assert await _refresh(mkt, flipkart_due["id"]) == "skipped"
    after = _row(mkt, flipkart_due["id"])
    assert after["status"] == "active" and after["last_refresh_at"] is None
    assert bytes(after["token_enc"]) == bytes(before["token_enc"])
    assert _used(mkt) == [] and mkt.control.token_requests == []
    tasks = mkt.conn.execute(
        "SELECT count(*) FROM human_tasks WHERE marketplace_account_id = %s", [flipkart_due["id"]]
    ).fetchone()
    assert tasks == (0,)
    # forced (the adapter's path) is refused too
    from app.workers.token_refresh import refresh_account

    forced = await refresh_account(mkt.tenant("acme"), uuid.UUID(flipkart_due["id"]), force=True)
    assert forced == "skipped" and mkt.control.token_requests == []


async def test_flipkart_flag_off_periodic_skips_flipkart(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers.periodic import refresh_marketplace_tokens

    amazon = await connected_account(mapi, mkt, OWNER, "amazon", seller="A1DUE")
    flipkart = await connected_account(mapi, mkt, OWNER, "flipkart", seller="F1DUE")
    _due(mkt, amazon["id"])
    _due(mkt, flipkart["id"])
    _flag(monkeypatch, False)
    assert await refresh_marketplace_tokens() == {"refreshed": 1}
    assert _row(mkt, amazon["id"])["last_refresh_at"] is not None
    assert _row(mkt, flipkart["id"])["last_refresh_at"] is None
    _flag(monkeypatch, True)
    assert await refresh_marketplace_tokens() == {"refreshed": 1}
    assert _row(mkt, flipkart["id"])["last_refresh_at"] is not None


async def test_flipkart_revoke_when_off(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await connected_account(mapi, mkt, OWNER, "flipkart", seller="F1REVOKE")
    _flag(monkeypatch, False)
    response = await mapi.post(
        f"{PREFIX}/marketplaces/accounts/{account['id']}/revoke", headers=as_member(mkt, OWNER)
    )
    assert response.status_code == 200, response.text
    row = _row(mkt, account["id"])
    assert row["status"] == "revoked" and row["token_enc"] is None
    assert audit_rows(mkt.conn, "marketplace_account.revoked") != []


async def test_flipkart_accounts_list_when_off(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await connected_account(mapi, mkt, OWNER, "flipkart", seller="F1LIST")
    _flag(monkeypatch, False)
    response = await mapi.get(f"{PREFIX}/marketplaces/accounts", headers=as_member(mkt, OWNER))
    assert response.status_code == 200, response.text
    assert [a["id"] for a in response.json()["data"]] == [account["id"]]


async def test_flipkart_refresh_token_url_mismatch_refused(
    mkt: MktWorld, flipkart_due: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag on, local, a non-loopback token URL off the pin: no unseal, no call, no secret sent."""
    from app.core.config import get_settings

    monkeypatch.setenv("FLIPKART_TOKEN_URL", "http://fake.marketplace.test/flipkart/token")
    get_settings.cache_clear()
    assert await _refresh(mkt, flipkart_due["id"]) == "skipped"
    assert _used(mkt) == [] and mkt.control.token_requests == []
    assert _row(mkt, flipkart_due["id"])["last_refresh_at"] is None
