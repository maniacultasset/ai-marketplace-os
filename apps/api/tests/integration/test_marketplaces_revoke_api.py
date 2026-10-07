"""S15 T14 (DW6, AC9-10): `POST /api/v1/marketplaces/accounts/{account_id}/revoke` through the
real app as `app_rw`. Revoke deletes the live copy of the tokens (`token_enc` and the key version
NULL), cancels the open reconnect task and is idempotent; another tenant's id is 404."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .auth_support import PREFIX, audit_rows
from .conftest import LOCAL_HOSTS
from .jobs_support import bad_token
from .marketplace_support import MktWorld, account_rows, as_member, connected_account, mapi, mkt

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]


def _url(account_id: str) -> str:
    return f"{PREFIX}/marketplaces/accounts/{account_id}/revoke"


async def _revoke(
    api: httpx.AsyncClient, mw: MktWorld, account_id: str, email: str = "owner@acme.test"
) -> httpx.Response:
    return await api.post(_url(account_id), headers=as_member(mw, email))


def _row(mw: MktWorld, account_id: str) -> dict[str, Any]:
    return next(r for r in account_rows(mw.conn) if str(r["id"]) == account_id)


def _open_task(mw: MktWorld, account_id: str) -> None:
    mw.conn.execute(
        "INSERT INTO human_tasks (tenant_id, kind, target_path, marketplace_account_id, question, "
        "reason, expected_answer, resume_from) SELECT tenant_id, 'reconnect_account', "
        "'account.oauth', id, 'Reconnect it.', 'Refresh refused.', '{\"kind\": \"reconnect\"}', "
        "'publish' FROM marketplace_accounts WHERE id = %s",
        [account_id],
    )


@pytest.mark.endpoint("marketplaces_accounts_revoke", case="success")
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_revoke_crypto_shreds(mkt: MktWorld, mapi: httpx.AsyncClient, role: str) -> None:
    """DW6 (name kept): the live copy of the tokens is deleted, not a crypto-shred (D9)."""
    account = await connected_account(mapi, mkt)
    _open_task(mkt, account["id"])
    response = await _revoke(mapi, mkt, account["id"], f"{role}@acme.test")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "revoked" and response.json()["id"] == account["id"]
    row = _row(mkt, account["id"])
    assert row["status"] == "revoked"
    assert row["token_enc"] is None and row["kms_key_version"] is None
    assert row["revoked_at"] is not None
    task = mkt.conn.execute(
        "SELECT status, cancelled_reason FROM human_tasks WHERE marketplace_account_id = %s",
        [account["id"]],
    ).fetchone()
    assert task == ("cancelled", "superseded")
    [audit] = audit_rows(mkt.conn, "marketplace_account.revoked")
    assert str(audit[5]) == account["id"] and audit[2] == mkt.user(f"{role}@acme.test")
    assert (audit[6], audit[7]) == ({"status": "active"}, {"status": "revoked"})


async def test_revoke_repeat_same_200_no_write(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    account = await connected_account(mapi, mkt)
    first = await _revoke(mapi, mkt, account["id"])
    updated_at = _row(mkt, account["id"])["updated_at"]
    second = await _revoke(mapi, mkt, account["id"])
    assert second.status_code == 200 and second.json() == first.json()
    assert _row(mkt, account["id"])["updated_at"] == updated_at
    assert len(audit_rows(mkt.conn, "marketplace_account.revoked")) == 1


async def test_revoke_needs_reconnect_account_clears_since(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    account = await connected_account(mapi, mkt)
    mkt.conn.execute(
        "UPDATE marketplace_accounts SET status = 'needs_reconnect', needs_reconnect_since = now() "
        "WHERE id = %s",
        [account["id"]],
    )
    response = await _revoke(mapi, mkt, account["id"])
    assert response.status_code == 200
    assert response.json()["needs_reconnect_since"] is None
    assert _row(mkt, account["id"])["needs_reconnect_since"] is None


@pytest.mark.endpoint("marketplaces_accounts_revoke", case="validation")
async def test_revoke_bad_uuid_422(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    response = await _revoke(mapi, mkt, "not-a-uuid")
    assert response.status_code == 422


@pytest.mark.endpoint("marketplaces_accounts_revoke", case="missing_auth")
async def test_revoke_missing_auth_401(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    account = await connected_account(mapi, mkt)
    assert (await mapi.post(_url(account["id"]))).status_code == 401
    assert _row(mkt, account["id"])["status"] == "active"


@pytest.mark.endpoint("marketplaces_accounts_revoke", case="invalid_token", variant="expired")
@pytest.mark.endpoint("marketplaces_accounts_revoke", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("marketplaces_accounts_revoke", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_revoke_invalid_token_401(
    mkt: MktWorld, mapi: httpx.AsyncClient, variant: str
) -> None:
    account = await connected_account(mapi, mkt)
    response = await mapi.post(_url(account["id"]), headers=bad_token(mkt, variant))  # type: ignore[arg-type]
    assert response.status_code == 401


@pytest.mark.endpoint("marketplaces_accounts_revoke", case="wrong_role")
@pytest.mark.parametrize("role", ["operator", "reviewer"])
async def test_revoke_wrong_role_403(mkt: MktWorld, mapi: httpx.AsyncClient, role: str) -> None:
    account = await connected_account(mapi, mkt)
    response = await _revoke(mapi, mkt, account["id"], f"{role}@acme.test")
    assert response.status_code == 403
    assert _row(mkt, account["id"])["token_enc"] is not None


@pytest.mark.endpoint("marketplaces_accounts_revoke", case="wrong_tenant")
async def test_revoke_other_tenant_404(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    theirs = await connected_account(mapi, mkt, "owner@bharat.test")
    response = await _revoke(mapi, mkt, theirs["id"], "owner@acme.test")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NOT_FOUND"
    assert _row(mkt, theirs["id"])["status"] == "active"


@pytest.mark.endpoint("marketplaces_accounts_revoke", case="not_found")
async def test_revoke_unknown_404(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    response = await _revoke(mapi, mkt, str(uuid.uuid4()))
    assert response.status_code == 404
