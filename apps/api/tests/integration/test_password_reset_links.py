"""SI5 T8 (AC9, AC10; PLAN D8, D9): copied password reset links through the real app as app_rw.
Under `MFA_ENFORCEMENT=all` an owner issues a link for a sole-tenant member with a TOTP factor;
the member redeems it once. Everyone outside that set is 403 at issue and 404 at redeem."""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

import httpx
import pytest

from .auth_support import (
    PASSWORD,
    PREFIX,
    Conn,
    World,
    add_member,
    audit_rows,
    bearer,
    build_world,
    tokens_for,
)
from .conftest import LOCAL_HOSTS
from .mfa_support import login, mfa_off, mfa_on

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

RESET = f"{PREFIX}/auth/password/reset"
# the fixtures' password with one word changed: meets the DPP rules, not one of the last 10
NEW_PASSWORD = PASSWORD.replace("Test", "Fresh")
NOT_ELIGIBLE = "Kuinn support resets this account."


@pytest.fixture
def world(auth_db: Conn, monkeypatch: pytest.MonkeyPatch) -> World:
    mfa_on(monkeypatch)
    built = build_world(auth_db)
    for email in ("reviewer@acme.test", "operator@acme.test", "admin@acme.test"):
        _factor(auth_db, built.user(email))
    return built


def _factor(conn: Conn, user: uuid.UUID) -> None:
    conn.execute(
        "INSERT INTO user_mfa_factors (user_id, status, secret_blob, key_version, confirmed_at) "
        "VALUES (%s, 'active', %s, 'local-v1', now())",
        [user, b"x" * 40],
    )


def _stepped(world: World, email: str, role: str | None = None) -> dict[str, str]:
    """A bearer whose TOTP step-up is fresh (the `step_up="otp"` guard)."""
    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(world, email, slug, role, amr=["pwd", "otp"], mfa_at=int(time.time())))


def _path(user: uuid.UUID) -> str:
    return f"{PREFIX}/members/{user}/password-reset"


async def _issue(
    api: httpx.AsyncClient, world: World, target: str, caller: str = "owner@acme.test"
) -> httpx.Response:
    return await api.post(_path(world.user(target)), headers=_stepped(world, caller))


async def _link(api: httpx.AsyncClient, world: World, target: str, **kwargs: Any) -> str:
    response = await _issue(api, world, target, **kwargs)
    assert response.status_code == 201, response.text
    url = str(response.json()["reset_url"])
    assert "/reset-password#token=" in url
    return url.split("#token=", 1)[1]


async def _redeem(
    api: httpx.AsyncClient, token: str, password: str = NEW_PASSWORD
) -> httpx.Response:
    return await api.post(RESET, json={"token": token, "new_password": password})


def _sessions(conn: Conn, user: uuid.UUID, tenant: uuid.UUID) -> None:
    for n in range(2):
        conn.execute(
            "INSERT INTO refresh_tokens (tenant_id, user_id, family_id, token_hash, expires_at) "
            "VALUES (%s, %s, %s, %s, now() + interval '1 day')",
            [tenant, user, uuid.uuid4(), hashlib.sha256(f"{user}{n}".encode()).digest()],
        )
    conn.execute(
        "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, expires_at) "
        "VALUES (%s, %s, 'otp', 'Chrome', now(), now() + interval '7 days')",
        [user, hashlib.sha256(f"device{user}".encode()).digest()],
    )


def _reset_limits(conn: Conn) -> None:
    from tests.support.rate_limits import reset_rate_limits

    reset_rate_limits(conn)


# ---- AC9: issue and redeem ----


@pytest.mark.endpoint("members_password_reset", case="success")
@pytest.mark.endpoint("auth_password_reset", case="success")
async def test_issue_and_redeem(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    target = world.user("reviewer@acme.test")
    _sessions(auth_db, target, world.tenant("acme"))
    factor_before = auth_db.execute(
        "SELECT id, secret_blob, status FROM user_mfa_factors WHERE user_id = %s", [target]
    ).fetchall()
    response = await _issue(api, world, "reviewer@acme.test")
    assert response.status_code == 201, response.text
    token = response.json()["reset_url"].split("#token=", 1)[1]
    (issued,) = audit_rows(auth_db, "user.password_reset_issued")
    assert issued[1:3] == ("user", world.user("owner@acme.test")) and issued[5] == target
    redeemed = await _redeem(api, token)
    assert redeemed.status_code == 204, redeemed.text
    _reset_limits(auth_db)
    assert (await login(api, "reviewer@acme.test", PASSWORD)).status_code == 401
    fresh = await login(api, "reviewer@acme.test", NEW_PASSWORD)
    assert fresh.status_code == 200 and fresh.json()["status"] == "mfa_required", fresh.text
    live = auth_db.execute(
        "SELECT count(*) FILTER (WHERE revoked_at IS NULL), "
        "count(*) FILTER (WHERE revoked_reason = 'password_changed') "
        "FROM refresh_tokens WHERE user_id = %s",
        [target],
    ).fetchone()
    assert live == (0, 2)
    devices = auth_db.execute(
        "SELECT count(*) FROM trusted_devices WHERE user_id = %s", [target]
    ).fetchone()
    assert devices == (0,)
    assert (
        auth_db.execute(
            "SELECT id, secret_blob, status FROM user_mfa_factors WHERE user_id = %s", [target]
        ).fetchall()
        == factor_before
    )
    (done,) = audit_rows(auth_db, "user.password_reset")
    assert done[1:3] == ("user", target)
    assert (await _redeem(api, token, NEW_PASSWORD.replace("Fresh", "Other"))).status_code == 404


async def test_a_new_link_replaces_the_old(api: httpx.AsyncClient, world: World) -> None:
    old = await _link(api, world, "reviewer@acme.test")
    new = await _link(api, world, "reviewer@acme.test")
    assert (await _redeem(api, old)).status_code == 404
    assert (await _redeem(api, new)).status_code == 204


async def test_concurrent_issues_for_one_member_leave_one_live_link(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """A double-clicked reset (tester R2): the per-member lock queues the issues, so every
    caller gets 201, the last link wins and only one stays live (no 500 on
    `uq_password_reset_links_live`)."""
    import asyncio

    responses = await asyncio.gather(*(_issue(api, world, "reviewer@acme.test") for _ in range(4)))
    assert [r.status_code for r in responses] == [201] * 4, [r.text for r in responses]
    tokens = [str(r.json()["reset_url"]).split("#token=", 1)[1] for r in responses]
    live = auth_db.execute(
        "SELECT token_hash FROM password_reset_links WHERE user_id = %s "
        "AND used_at IS NULL AND revoked_at IS NULL",
        [world.user("reviewer@acme.test")],
    ).fetchall()
    assert len(live) == 1
    (winner,) = [t for t in tokens if hashlib.sha256(t.encode()).digest() == bytes(live[0][0])]
    assert (await _redeem(api, winner)).status_code == 204


async def test_redeem_rules_keep_the_link_live(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _link(api, world, "reviewer@acme.test")
    weak = await _redeem(api, token, "short")
    assert weak.status_code == 422
    assert weak.json()["error"]["details"][0]["loc"] == ["body", "new_password"]
    reused = await _redeem(api, token, PASSWORD)  # the current password is one of the last 10
    assert reused.status_code == 422
    assert reused.json()["error"]["message"] == "The password must not be one of the last 10."
    assert (await _redeem(api, token)).status_code == 204


# ---- AC10: who gets a link ----


@pytest.mark.endpoint("members_password_reset", case="validation")
async def test_self_is_422(api: httpx.AsyncClient, world: World) -> None:
    response = await _issue(api, world, "owner@acme.test")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "user_id"]
    bad = await api.post(
        f"{PREFIX}/members/not-a-uuid/password-reset", headers=_stepped(world, "owner@acme.test")
    )
    assert bad.status_code == 422


@pytest.mark.endpoint("members_password_reset", case="wrong_role")
@pytest.mark.parametrize("caller", ["operator@acme.test", "reviewer@acme.test"])
async def test_needs_team_manage(api: httpx.AsyncClient, world: World, caller: str) -> None:
    response = await _issue(api, world, "admin@acme.test", caller=caller)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"


async def test_admin_cannot_reset_an_owner(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    _factor(auth_db, world.user("owner@acme.test"))
    response = await _issue(api, world, "owner@acme.test", caller="admin@acme.test")
    assert response.status_code == 403
    assert response.json()["error"]["message"] != NOT_ELIGIBLE


@pytest.mark.parametrize("case", ["two_tenants", "inactive_elsewhere", "platform_admin", "no_totp"])
async def test_not_eligible_is_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn, case: str
) -> None:
    target = "reviewer@acme.test"
    if case == "two_tenants":
        add_member(auth_db, world.tenant("bharat"), world.user(target), "reviewer")
    elif case == "inactive_elsewhere":
        add_member(auth_db, world.tenant("bharat"), world.user(target), "reviewer", active=False)
    elif case == "platform_admin":
        auth_db.execute(
            "UPDATE users SET is_platform_admin = true WHERE id = %s", [world.user(target)]
        )
    else:
        auth_db.execute("DELETE FROM user_mfa_factors WHERE user_id = %s", [world.user(target)])
    response = await _issue(api, world, target)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["message"] == NOT_ELIGIBLE


async def test_google_only_member_is_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    auth_db.execute(
        "UPDATE users SET hashed_password = NULL, auth_provider = 'google', google_sub = 'g-sub-1', "
        "email_verified_at = now() WHERE id = %s",
        [world.user("reviewer@acme.test")],
    )
    response = await _issue(api, world, "reviewer@acme.test")
    assert response.status_code == 403


@pytest.mark.parametrize("factor", [True, False])
async def test_every_link_is_refused_under_off(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    factor: bool,
) -> None:
    if not factor:
        auth_db.execute(
            "DELETE FROM user_mfa_factors WHERE user_id = %s", [world.user("reviewer@acme.test")]
        )
    mfa_off(monkeypatch)
    response = await api.post(
        _path(world.user("reviewer@acme.test")), headers=_stepped(world, "owner@acme.test")
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == NOT_ELIGIBLE


@pytest.mark.endpoint("members_password_reset", case="wrong_tenant")
@pytest.mark.endpoint("members_password_reset", case="not_found")
async def test_member_of_another_tenant_is_404(api: httpx.AsyncClient, world: World) -> None:
    other = await _issue(api, world, "reviewer@bharat.test")
    assert other.status_code == 404
    unknown = await api.post(_path(uuid.uuid4()), headers=_stepped(world, "owner@acme.test"))
    assert unknown.status_code == 404


@pytest.mark.endpoint("members_password_reset", case="missing_auth")
async def test_issue_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    assert (await api.post(_path(world.user("reviewer@acme.test")))).status_code == 401


async def test_issue_needs_a_totp_step_up(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    _factor(auth_db, world.user("owner@acme.test"))
    plain = bearer(tokens_for(world, "owner@acme.test", "acme"))
    response = await api.post(_path(world.user("reviewer@acme.test")), headers=plain)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "MFA_STEP_UP_REQUIRED"
    recovery = bearer(
        tokens_for(world, "owner@acme.test", "acme", amr=["pwd", "rc"], mfa_at=int(time.time()))
    )
    rc = await api.post(_path(world.user("reviewer@acme.test")), headers=recovery)
    assert rc.status_code == 403 and rc.json()["error"]["type"] == "MFA_STEP_UP_REQUIRED"


# ---- AC10: what kills a link at redeem ----


@pytest.mark.parametrize(
    "spoil",
    [
        "expired",
        "target_deactivated",
        "mfa_turned_off",
        "issuer_deactivated",
        "issuer_demoted",
        "target_promoted_by_admin_issuer",
        "target_joined_another_tenant",
    ],
)
async def test_redeem_refusals_are_404(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    spoil: str,
) -> None:
    caller = "admin@acme.test" if spoil.startswith(("issuer", "target_promoted")) else None
    token = await _link(api, world, "reviewer@acme.test", **({"caller": caller} if caller else {}))
    target, admin = world.user("reviewer@acme.test"), world.user("admin@acme.test")
    if spoil == "expired":
        auth_db.execute(
            "UPDATE password_reset_links SET expires_at = now() WHERE user_id = %s", [target]
        )
    elif spoil == "target_deactivated":
        auth_db.execute(
            "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
            "WHERE user_id = %s",
            [target],
        )
    elif spoil == "mfa_turned_off":
        mfa_off(monkeypatch)
    elif spoil == "issuer_deactivated":
        auth_db.execute(
            "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
            "WHERE user_id = %s",
            [admin],
        )
    elif spoil == "issuer_demoted":
        auth_db.execute(
            "UPDATE tenant_memberships SET role = 'operator' WHERE user_id = %s", [admin]
        )
    elif spoil == "target_promoted_by_admin_issuer":
        auth_db.execute("UPDATE tenant_memberships SET role = 'owner' WHERE user_id = %s", [target])
    else:
        add_member(auth_db, world.tenant("bharat"), target, "operator")
    response = await _redeem(api, token)
    assert response.status_code == 404, (spoil, response.text)
    assert response.json()["error"]["type"] == "NOT_FOUND"


async def test_owner_issued_link_survives_a_promotion(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    token = await _link(api, world, "reviewer@acme.test")
    auth_db.execute(
        "UPDATE tenant_memberships SET role = 'owner' WHERE user_id = %s",
        [world.user("reviewer@acme.test")],
    )
    assert (await _redeem(api, token)).status_code == 204


@pytest.mark.endpoint("auth_password_reset", case="validation")
@pytest.mark.parametrize("token", ["R" * 42, "R" * 42 + "="])
async def test_redeem_malformed_token_422(api: httpx.AsyncClient, world: World, token: str) -> None:
    assert (await _redeem(api, token)).status_code == 422


async def test_redeem_unknown_token_404(api: httpx.AsyncClient, world: World) -> None:
    assert (await _redeem(api, "Q" * 43)).status_code == 404


async def test_sixth_reset_call_from_one_ip_is_429(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    _reset_limits(auth_db)
    statuses = [(await _redeem(api, "Q" * 43)).status_code for _ in range(5)]
    assert statuses == [404] * 5
    limited = await _redeem(api, "Q" * 43)
    assert limited.status_code == 429 and limited.json()["error"]["type"] == "RATE_LIMITED"
    # the invite bucket is separate
    assert (await api.post(f"{PREFIX}/invites/lookup", json={"token": "Q" * 43})).status_code == 404
