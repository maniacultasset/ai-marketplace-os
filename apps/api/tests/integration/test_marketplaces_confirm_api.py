"""S15 T13 (DW1-2, AC3-4, AC11): `POST /api/v1/marketplaces/connect/confirm` through the real app
as `app_rw`: the pending handle becomes the tenant's live account, only for the user who started
the flow, in its tenant; the same seller reconnects; another seller, or the seller live in another
tenant, is the uniform 409 `MARKETPLACE_ACCOUNT_CONFLICT` and the handle survives."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from .auth_support import PREFIX, audit_rows
from .conftest import LOCAL_HOSTS
from .jobs_support import bad_token
from .marketplace_support import (
    MktWorld,
    account_rows,
    as_member,
    confirm,
    connect,
    connect_to_pending,
    connected_account,
    mapi,
    mkt,
    oauth_rows,
    query_of,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]
CONFLICT_MESSAGE = "This seller account already has a live connection; revoke it first."


def _pending(mw: MktWorld) -> list[dict[str, Any]]:
    return [r for r in oauth_rows(mw.conn) if r["token_enc"] is not None]


def _assert_bad_state(response: httpx.Response) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == "OAUTH_STATE_INVALID"


def _assert_seller_conflict(response: httpx.Response) -> None:
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["type"] == "MARKETPLACE_ACCOUNT_CONFLICT"
    assert error["message"] == CONFLICT_MESSAGE


def _revoke(mw: MktWorld, account_id: str) -> None:
    """Revoked by the admin connection (the revoke route is T14's suite)."""
    mw.conn.execute(
        "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, "
        "kms_key_version = NULL, needs_reconnect_since = NULL, revoked_at = now() WHERE id = %s",
        [account_id],
    )


@pytest.mark.endpoint("marketplaces_connect_confirm", case="success")
async def test_confirm_activates(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    handle = await connect_to_pending(mapi, mkt, seller="A5ACTIVE")
    pending = _pending(mkt)[0]
    response = await confirm(mapi, mkt, "owner@acme.test", handle)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["marketplace"], body["seller_id"], body["status"]) == (
        "amazon",
        "A5ACTIVE",
        "active",
    )
    assert set(body) == {
        "id",
        "marketplace",
        "seller_id",
        "region",
        "status",
        "needs_reconnect_since",
        "expires_at",
        "created_at",
    }
    [row] = account_rows(mkt.conn)
    assert str(row["id"]) == body["id"]
    assert row["tenant_id"] == mkt.tenant("acme")
    assert bytes(row["token_enc"]) == bytes(pending["token_enc"])
    assert row["kms_key_version"] == "local-1"
    assert row["scopes"] == ["sellingpartnerapi::listings"]
    assert row["expires_at"] == pending["token_expires_at"]
    assert row["connected_by_user_id"] == mkt.user("owner@acme.test")
    assert _pending(mkt) == []
    [audit] = audit_rows(mkt.conn, "marketplace_account.connected")
    assert audit[4] == "marketplace_account" and str(audit[5]) == body["id"]
    assert audit[7] == {"status": "active"}


async def test_confirm_same_seller_reconnects_same_id(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    first = await connected_account(mapi, mkt, seller="A6SAME")
    mkt.conn.execute(
        "UPDATE marketplace_accounts SET status = 'needs_reconnect', needs_reconnect_since = now(), "
        "last_refresh_error_code = 'AUTH_EXPIRED' WHERE id = %s",
        [first["id"]],
    )
    mkt.conn.execute(
        "INSERT INTO human_tasks (tenant_id, kind, target_path, marketplace_account_id, question, "
        "reason, expected_answer, resume_from) VALUES (%s, 'reconnect_account', 'account.oauth', "
        "%s, 'Reconnect it.', 'Refresh refused.', '{\"kind\": \"reconnect\"}', 'publish')",
        [mkt.tenant("acme"), first["id"]],
    )
    again = await connected_account(mapi, mkt, seller="A6SAME")
    assert again["id"] == first["id"] and again["status"] == "active"
    assert again["needs_reconnect_since"] is None
    [row] = account_rows(mkt.conn)
    assert row["last_refresh_error_code"] is None
    task = mkt.conn.execute(
        "SELECT status, answered_by_user_id FROM human_tasks WHERE marketplace_account_id = %s",
        [first["id"]],
    ).fetchone()
    assert task == ("applied", mkt.user("owner@acme.test"))
    [audit] = audit_rows(mkt.conn, "marketplace_account.reconnected")
    assert (audit[6], audit[7]) == ({"status": "needs_reconnect"}, {"status": "active"})


@pytest.mark.endpoint("marketplaces_connect_confirm", case="validation")
@pytest.mark.parametrize(
    "body", [{"pending": "short"}, {"pending": "A" * 42 + "="}, {}, {"pending": "A" * 43, "x": 1}]
)
async def test_confirm_validation_422(
    mkt: MktWorld, mapi: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    response = await mapi.post(
        f"{PREFIX}/marketplaces/connect/confirm",
        json=body,
        headers=as_member(mkt, "owner@acme.test"),
    )
    assert response.status_code == 422


@pytest.mark.endpoint("marketplaces_connect_confirm", case="missing_auth")
async def test_confirm_missing_auth_401(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    handle = await connect_to_pending(mapi, mkt)
    response = await mapi.post(f"{PREFIX}/marketplaces/connect/confirm", json={"pending": handle})
    assert response.status_code == 401
    assert len(_pending(mkt)) == 1


@pytest.mark.endpoint("marketplaces_connect_confirm", case="invalid_token", variant="expired")
@pytest.mark.endpoint("marketplaces_connect_confirm", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("marketplaces_connect_confirm", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_confirm_invalid_token_401(
    mkt: MktWorld, mapi: httpx.AsyncClient, variant: str
) -> None:
    handle = await connect_to_pending(mapi, mkt)
    response = await mapi.post(
        f"{PREFIX}/marketplaces/connect/confirm",
        json={"pending": handle},
        headers=bad_token(mkt, variant),  # type: ignore[arg-type]
    )
    assert response.status_code == 401
    assert len(_pending(mkt)) == 1


@pytest.mark.endpoint("marketplaces_connect_confirm", case="wrong_role")
@pytest.mark.parametrize("role", ["operator", "reviewer"])
async def test_confirm_wrong_role_403_handle_kept(
    mkt: MktWorld, mapi: httpx.AsyncClient, role: str
) -> None:
    handle = await connect_to_pending(mapi, mkt)
    response = await confirm(mapi, mkt, f"{role}@acme.test", handle)
    assert response.status_code == 403
    assert len(_pending(mkt)) == 1
    assert (await confirm(mapi, mkt, "owner@acme.test", handle)).status_code == 200


@pytest.mark.endpoint("marketplaces_connect_confirm", case="wrong_tenant")
async def test_oauth_state_bharat_confirms_acme_handle_409(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    handle = await connect_to_pending(mapi, mkt)
    _assert_bad_state(await confirm(mapi, mkt, "owner@bharat.test", handle))
    assert account_rows(mkt.conn) == []
    assert len(_pending(mkt)) == 1


@pytest.mark.endpoint("marketplaces_connect_confirm", case="conflict")
async def test_confirm_other_seller_409_handle_kept(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    live = await connected_account(mapi, mkt, seller="A7FIRST")
    handle = await connect_to_pending(mapi, mkt, seller="A7SECOND")
    _assert_seller_conflict(await confirm(mapi, mkt, "owner@acme.test", handle))
    assert len(_pending(mkt)) == 1  # rolled back: the handle survives
    _revoke(mkt, live["id"])
    retry = await confirm(mapi, mkt, "owner@acme.test", handle)
    assert retry.status_code == 200 and retry.json()["seller_id"] == "A7SECOND"


async def test_confirm_seller_live_in_other_tenant_409(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    acme = await connected_account(mapi, mkt, seller="A8SHARED")
    handle = await connect_to_pending(mapi, mkt, "owner@bharat.test", seller="A8SHARED")
    response = await confirm(mapi, mkt, "owner@bharat.test", handle)
    _assert_seller_conflict(response)
    exposed = response.text + json.dumps(dict(response.headers))
    for value in (acme["id"], str(mkt.tenant("acme")), "acme", "Acme"):
        assert value not in exposed
    assert [r["tenant_id"] for r in account_rows(mkt.conn)] == [mkt.tenant("acme")]
    _revoke(mkt, acme["id"])
    retry = await confirm(mapi, mkt, "owner@bharat.test", handle)
    assert retry.status_code == 200, retry.text


@pytest.mark.endpoint("marketplaces_connect_confirm", case="bad_state", variant="replayed")
async def test_oauth_state_confirm_twice_409(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    handle = await connect_to_pending(mapi, mkt)
    assert (await confirm(mapi, mkt, "owner@acme.test", handle)).status_code == 200
    _assert_bad_state(await confirm(mapi, mkt, "owner@acme.test", handle))


async def test_oauth_state_confirm_by_other_acme_user_409(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    """CSRF / account linking (s3 M7): an admin of the same tenant cannot finish the owner's flow."""
    handle = await connect_to_pending(mapi, mkt, "owner@acme.test")
    _assert_bad_state(await confirm(mapi, mkt, "admin@acme.test", handle))
    assert len(_pending(mkt)) == 1


@pytest.mark.endpoint("marketplaces_connect_confirm", case="bad_state", variant="expired")
async def test_oauth_state_confirm_after_ttl_409(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    handle = await connect_to_pending(mapi, mkt)
    mkt.conn.execute("UPDATE oauth_states SET expires_at = now() - interval '1 second'")
    _assert_bad_state(await confirm(mapi, mkt, "owner@acme.test", handle))


@pytest.mark.endpoint("marketplaces_connect_confirm", case="bad_state", variant="forged")
async def test_oauth_state_confirm_unknown_handle_409(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    _assert_bad_state(await confirm(mapi, mkt, "owner@acme.test", "Z" * 43))


# The confirm has no marketplace in its input: its "mismatch" is the other row kind, a state's
# value sent as a pending handle.
@pytest.mark.endpoint(
    "marketplaces_connect_confirm", case="bad_state", variant="marketplace_mismatch"
)
async def test_oauth_state_state_as_pending_409(mkt: MktWorld, mapi: httpx.AsyncClient) -> None:
    state = query_of(await connect(mapi, mkt))["state"]
    _assert_bad_state(await confirm(mapi, mkt, "owner@acme.test", state))
    assert len(oauth_rows(mkt.conn)) == 1  # the state row is untouched


async def test_confirm_two_concurrent_same_handle_one_wins(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    handle = await connect_to_pending(mapi, mkt)
    results = await asyncio.gather(
        *(confirm(mapi, mkt, "owner@acme.test", handle) for _ in range(3))
    )
    assert sorted(r.status_code for r in results) == [200, 409, 409]
    assert len(account_rows(mkt.conn)) == 1


async def test_confirm_two_handles_same_seller_one_row(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    one = await connect_to_pending(mapi, mkt, seller="A9TWICE")
    two = await connect_to_pending(mapi, mkt, seller="A9TWICE")
    results = await asyncio.gather(
        confirm(mapi, mkt, "owner@acme.test", one), confirm(mapi, mkt, "owner@acme.test", two)
    )
    assert [r.status_code for r in results] == [200, 200]
    assert len(account_rows(mkt.conn)) == 1


async def test_confirm_two_tenants_same_seller_concurrent_one_row(
    mkt: MktWorld, mapi: httpx.AsyncClient
) -> None:
    acme = await connect_to_pending(mapi, mkt, "owner@acme.test", seller="A0RACE")
    bharat = await connect_to_pending(mapi, mkt, "owner@bharat.test", seller="A0RACE")
    results = await asyncio.gather(
        confirm(mapi, mkt, "owner@acme.test", acme), confirm(mapi, mkt, "owner@bharat.test", bharat)
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    conflict = next(r for r in results if r.status_code == 409)
    assert conflict.json()["error"]["type"] == "MARKETPLACE_ACCOUNT_CONFLICT"
    assert len(account_rows(mkt.conn)) == 1
