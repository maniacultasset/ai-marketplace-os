"""ADR-0023: trusted browsers and the narrower step-up, end to end through the HTTP routes against
PostgreSQL (the app as app_rw under FORCE RLS; the admin connection builds and reads the world).

A trusted browser is created only by a TOTP code with `trust_device`, skips only the code (never
enrolment, a password change or a Google link), rotates its token on each use, mints no new `amr`
value (`mfa_at` stays the time of the code that created it), and is revoked by every credential
change, by the user, by eviction past 10, by expiry and by token reuse.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import timedelta
from typing import Any

import httpx
import jwt
import pytest

from .auth_support import PASSWORD, Conn, World, add_member, audit_rows, bearer, build_world
from .conftest import LOCAL_HOSTS
from .mfa_support import (
    LOGIN,
    LOGIN_MFA,
    PASSWORD_CHANGE,
    PASSWORD_EXPIRED,
    REFRESH,
    STEP_UP,
    SWITCH,
    TOTP_CONFIRM,
    TOTP_ENROLL,
    Enrolled,
    TotpClock,
    clock,  # noqa: F401 - the fixture
    count,
    enrol,
    expired_token,
    foreign_tenant_token,
    forged_token,
    lock,
    lock_state,
    mfa_on,
    refresh_typ_token,
    set_failures,
    user_id,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

EMAIL = "owner@acme.test"
OTHER = "operator@acme.test"
DEVICES = "/api/v1/auth/devices"
SIGN_OUT_ALL = f"{DEVICES}/sign-out-all"
MEMBERS = "/api/v1/members"
NEW_PASSWORD = "Fresh-Lantern-Path-4821"
CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


def _reset_limits(conn: Conn) -> None:
    from tests.support.rate_limits import reset_rate_limits

    reset_rate_limits(conn)


async def _login(
    api: httpx.AsyncClient,
    conn: Conn,
    email: str,
    *,
    token: str | None = None,
    password: str = PASSWORD,
) -> httpx.Response:
    _reset_limits(conn)
    body: dict[str, Any] = {"email": email, "password": password}
    if token is not None:
        body["device_token"] = token
    return await api.post(LOGIN, json=body, headers={"User-Agent": CHROME_MAC})


async def _code(
    api: httpx.AsyncClient, user: Enrolled, totp: TotpClock, challenge: str, *, trust: bool
) -> dict[str, Any]:
    response = await api.post(
        LOGIN_MFA,
        json={
            "challenge_token": challenge,
            "code": totp.next_code(user.secret),
            "trust_device": trust,
        },
        headers={"User-Agent": CHROME_MAC},
    )
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def _trust(
    api: httpx.AsyncClient, conn: Conn, user: Enrolled, totp: TotpClock
) -> dict[str, Any]:
    """A password and code sign-in that trusts this browser: the answer carries the token."""
    first = (await _login(api, conn, user.email)).json()
    assert first["status"] == "mfa_required", first
    assert first["device_trust_days"] in (7, 30)
    body = await _code(api, user, totp, first["challenge_token"], trust=True)
    assert body["status"] == "signed_in" and body["device_token"], body
    return body


def _devices(conn: Conn, email: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT d.id, d.token_hash, d.prev_token_hash, d.trusted_at, d.expires_at, d.label, "
        "d.last_used_at FROM trusted_devices d JOIN users u ON u.id = d.user_id "
        "WHERE u.email = %s ORDER BY d.created_at",
        [email],
    ).fetchall()


def _digest(token: str) -> bytes:
    return hashlib.sha256(b"mkos-trusted-device|" + token.encode("ascii")).digest()


async def _enrolled(
    api: httpx.AsyncClient, conn: Conn, monkeypatch: pytest.MonkeyPatch, totp: TotpClock,
    email: str = EMAIL,
) -> Enrolled:  # fmt: skip
    mfa_on(monkeypatch)
    return await enrol(api, conn, email, totp)


# ---- 1. trust, then the next sign-in skips the code ----


async def test_trusted_browser_skips_the_code_and_rotates(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    trusted = await _trust(api, auth_db, user, clock)
    [row] = _devices(auth_db, EMAIL)
    assert bytes(row[1]) == _digest(trusted["device_token"]) and row[2] is None
    assert row[5] == "Chrome on macOS"
    assert row[4] - row[3] == timedelta(days=30)
    first_token = trusted["device_token"]

    again = await _login(api, auth_db, EMAIL, token=first_token)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["status"] == "signed_in" and body["challenge_token"] is None
    claims = _claims(body["access_token"])
    assert claims["amr"] == ["pwd", "otp"]  # no new method: the code that created the trust
    assert claims["mfa_at"] == int(row[3].timestamp())
    assert body["device_token"] and body["device_token"] != first_token
    assert body["device_expires_at"] is not None
    [rotated] = _devices(auth_db, EMAIL)
    assert bytes(rotated[1]) == _digest(body["device_token"])
    assert bytes(rotated[2]) == _digest(first_token)
    assert rotated[4] == row[4] and rotated[6] is not None  # fixed expiry, last used set
    reasons = [r[-1] for r in audit_rows(auth_db, "auth.login")]
    assert reasons[-1] == "trusted_device"
    assert len(audit_rows(auth_db, "auth.device_trusted")) == 1

    # the old value presented again is a theft signal: the browser is forgotten
    replay = await _login(api, auth_db, EMAIL, token=first_token)
    assert replay.status_code == 200
    replayed = replay.json()
    assert replayed["status"] == "mfa_required" and replayed["device_token_invalid"] is True
    assert _devices(auth_db, EMAIL) == []
    assert [r[-1] for r in audit_rows(auth_db, "auth.device_revoked")] == ["reuse_detected"]
    # and the session it began is revoked
    refreshed = await api.post(REFRESH, json={"refresh_token": body["refresh_token"]})
    assert refreshed.status_code == 401


async def test_unticked_box_trusts_nothing(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    first = (await _login(api, auth_db, EMAIL)).json()
    body = await _code(api, user, clock, first["challenge_token"], trust=False)
    assert body["status"] == "signed_in" and body["device_token"] is None
    assert _devices(auth_db, EMAIL) == []


# (2. Google + a trusted browser: test_google_signin_api.py, with the fake Google world.)


# ---- 3, 4. the first factor must pass first: no oracle, nothing rotated ----


async def test_wrong_password_with_a_live_token_is_401_counted_unrotated(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    trusted = await _trust(api, auth_db, user, clock)
    before = _devices(auth_db, EMAIL)
    wrong = await _login(
        api, auth_db, EMAIL, token=trusted["device_token"], password="Wrong-Password-0000"
    )
    assert wrong.status_code == 401
    assert "device" not in wrong.text
    assert _devices(auth_db, EMAIL) == before
    assert lock_state(auth_db, EMAIL) == (1, False)


async def test_locked_or_inactive_with_a_live_token_is_401(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    token = (await _trust(api, auth_db, user, clock))["device_token"]
    before = _devices(auth_db, EMAIL)
    lock(auth_db, EMAIL)
    assert (await _login(api, auth_db, EMAIL, token=token)).status_code == 401
    auth_db.execute("UPDATE users SET locked_until = NULL WHERE email = %s", [EMAIL])
    auth_db.execute("UPDATE users SET is_active = false WHERE email = %s", [EMAIL])
    assert (await _login(api, auth_db, EMAIL, token=token)).status_code == 401
    assert _devices(auth_db, EMAIL) == before


# ---- 5. expired, foreign and garbage tokens: the code step and `device_token_invalid` ----


async def test_expired_foreign_and_garbage_tokens_ask_for_the_code(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    owner = await _enrolled(api, auth_db, monkeypatch, clock)
    other = await enrol(api, auth_db, OTHER, clock)
    owner_token = (await _trust(api, auth_db, owner, clock))["device_token"]
    other_token = (await _trust(api, auth_db, other, clock))["device_token"]
    for token in (other_token, "x" * 43, "not-a-token", "A" * 128):
        answer = (await _login(api, auth_db, EMAIL, token=token)).json()
        assert answer["status"] == "mfa_required", answer
        assert answer["device_token_invalid"] is True and answer["device_trust_days"] == 30
    assert len(_devices(auth_db, OTHER)) == 1  # another user's token touches nothing
    auth_db.execute(
        "UPDATE trusted_devices SET trusted_at = now() - interval '31 days', "
        "expires_at = now() - interval '1 day' WHERE user_id = %s",
        [user_id(auth_db, EMAIL)],
    )
    expired = (await _login(api, auth_db, EMAIL, token=owner_token)).json()
    assert expired["status"] == "mfa_required" and expired["device_token_invalid"] is True
    assert _devices(auth_db, EMAIL) == []
    assert "expired" in [r[-1] for r in audit_rows(auth_db, "auth.device_revoked")]


async def test_device_token_in_the_query_is_ignored(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    token = (await _trust(api, auth_db, user, clock))["device_token"]
    _reset_limits(auth_db)
    response = await api.post(
        LOGIN, params={"device_token": token}, json={"email": EMAIL, "password": PASSWORD}
    )
    assert response.status_code == 200 and response.json()["status"] == "mfa_required"


# ---- 6. a recovery code never trusts the browser ----


async def test_recovery_code_never_trusts(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    first = (await _login(api, auth_db, EMAIL)).json()
    response = await api.post(
        LOGIN_MFA,
        json={
            "challenge_token": first["challenge_token"],
            "recovery_code": user.recovery_codes[0],
            "trust_device": True,
        },
    )
    assert response.status_code == 200 and response.json()["device_token"] is None
    assert _devices(auth_db, EMAIL) == []


# ---- 7. a password change due: the trust rides on the challenge, old trust is gone ----


async def test_password_due_then_trust_after_the_change(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    old_token = (await _trust(api, auth_db, user, clock))["device_token"]
    auth_db.execute("UPDATE users SET password_must_change = true WHERE email = %s", [EMAIL])
    # the live browser never skips a password change
    due = (await _login(api, auth_db, EMAIL, token=old_token)).json()
    assert due["status"] == "mfa_required" and due["device_token_invalid"] is None
    step = await _code(api, user, clock, due["challenge_token"], trust=True)
    assert step["status"] == "password_change_required" and step["device_token"] is None
    changed = await api.post(
        PASSWORD_EXPIRED,
        json={"challenge_token": step["challenge_token"], "new_password": NEW_PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["status"] == "signed_in" and body["device_token"]
    [row] = _devices(auth_db, EMAIL)
    assert bytes(row[1]) == _digest(body["device_token"])  # the old browser row is gone
    assert "password_changed" in [r[-1] for r in audit_rows(auth_db, "auth.device_revoked")]


# ---- 8. platform admins: 7 days, and make-admin forgets ----


async def test_admin_trust_lasts_seven_days(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    auth_db.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [EMAIL])
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    first = (await _login(api, auth_db, EMAIL)).json()
    assert first["device_trust_days"] == 7
    body = await _code(api, user, clock, first["challenge_token"], trust=True)
    [row] = _devices(auth_db, EMAIL)
    assert row[4] - row[3] == timedelta(days=7)
    assert body["device_expires_at"] is not None


# ---- 9. at most 10 per user: the oldest is evicted ----


async def test_eleventh_browser_evicts_the_oldest(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    uid = user_id(auth_db, EMAIL)
    for n in range(10):
        auth_db.execute(
            "INSERT INTO trusted_devices (user_id, token_hash, method, label, trusted_at, "
            "expires_at, created_at) VALUES (%s, %s, 'otp', 'Old', now(), "
            "now() + interval '30 days', now() - make_interval(days => %s))",
            [uid, hashlib.sha256(f"seed-{n}".encode()).digest(), 10 - n],
        )
    oldest = _devices(auth_db, EMAIL)[0][0]
    await _trust(api, auth_db, user, clock)
    rows = _devices(auth_db, EMAIL)
    assert len(rows) == 10 and oldest not in [r[0] for r in rows]
    assert [r[-1] for r in audit_rows(auth_db, "auth.device_revoked")] == ["evicted"]
    assert len(audit_rows(auth_db, "auth.device_trusted")) == 1


# ---- 10. the 7-day rule of a trusted session; the tenant switch copies it ----


async def test_trusted_session_refresh_rules(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    token = (await _trust(api, auth_db, user, clock))["device_token"]
    # the code that created the trust is 20 days old: only the device keeps the session alive
    auth_db.execute(
        "UPDATE trusted_devices SET trusted_at = now() - interval '20 days', "
        "expires_at = now() + interval '10 days' WHERE user_id = %s",
        [user_id(auth_db, EMAIL)],
    )
    session = (await _login(api, auth_db, EMAIL, token=token)).json()
    assert session["status"] == "signed_in"
    renewed = await api.post(REFRESH, json={"refresh_token": session["refresh_token"]})
    assert renewed.status_code == 200, renewed.text
    current = renewed.json()["refresh_token"]
    # 7 days after the trusted sign-in: refused
    auth_db.execute(
        "UPDATE refresh_tokens SET device_auth_at = now() - interval '7 days' "
        "WHERE device_id IS NOT NULL AND revoked_at IS NULL"
    )
    assert (await api.post(REFRESH, json={"refresh_token": current})).status_code == 401


async def test_trusted_session_ends_when_the_browser_expires_or_is_removed(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    token = (await _trust(api, auth_db, user, clock))["device_token"]
    auth_db.execute(
        "UPDATE trusted_devices SET trusted_at = now() - interval '20 days', "
        "expires_at = now() + interval '10 days' WHERE user_id = %s",
        [user_id(auth_db, EMAIL)],
    )
    session = (await _login(api, auth_db, EMAIL, token=token)).json()
    auth_db.execute(
        "UPDATE trusted_devices SET expires_at = now() - interval '1 second' WHERE user_id = %s",
        [user_id(auth_db, EMAIL)],
    )
    assert (
        await api.post(REFRESH, json={"refresh_token": session["refresh_token"]})
    ).status_code == 401


async def test_step_up_moves_a_trusted_session_back_to_the_ordinary_rule(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    token = (await _trust(api, auth_db, user, clock))["device_token"]
    auth_db.execute(
        "UPDATE trusted_devices SET trusted_at = now() - interval '20 days', "
        "expires_at = now() + interval '10 days' WHERE user_id = %s",
        [user_id(auth_db, EMAIL)],
    )
    session = (await _login(api, auth_db, EMAIL, token=token)).json()
    access = session["access_token"]
    # a trusted session's code is old: a guarded route asks for a fresh one
    target = world.user("reviewer@acme.test")
    refused = await api.patch(
        f"{MEMBERS}/{target}", json={"role": "operator"}, headers=bearer(access)
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["type"] == "MFA_STEP_UP_REQUIRED"
    stepped = await api.post(
        STEP_UP, json={"code": clock.next_code(user.secret)}, headers=bearer(access)
    )
    assert stepped.status_code == 200, stepped.text
    ok = await api.patch(
        f"{MEMBERS}/{target}",
        json={"role": "operator"},
        headers=bearer(stepped.json()["access_token"]),
    )
    assert ok.status_code == 200, ok.text
    # the browser goes away, but the stepped-up family now follows the code's 7 days
    auth_db.execute("DELETE FROM trusted_devices")
    renewed = await api.post(REFRESH, json={"refresh_token": session["refresh_token"]})
    assert renewed.status_code == 200, renewed.text


async def test_switch_copies_the_trusted_browser(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    add_member(auth_db, world.tenant("bharat"), world.user(EMAIL), "operator")
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    token = (await _trust(api, auth_db, user, clock))["device_token"]
    auth_db.execute(
        "UPDATE trusted_devices SET trusted_at = now() - interval '20 days', "
        "expires_at = now() + interval '10 days' WHERE user_id = %s",
        [user_id(auth_db, EMAIL)],
    )
    session = (await _login(api, auth_db, EMAIL, token=token)).json()
    claims = _claims(session["access_token"])
    other = "bharat" if claims["tid"] == str(world.tenant("acme")) else "acme"
    switched = await api.post(
        SWITCH,
        json={"target_tenant_id": str(world.tenant(other))},
        headers=bearer(session["access_token"]),
    )
    assert switched.status_code == 200, switched.text

    def family(sid: str) -> Any:
        return auth_db.execute(
            "SELECT device_id, device_auth_at FROM refresh_tokens "
            "WHERE family_id = %s AND revoked_at IS NULL",
            [sid],
        ).fetchone()

    before, after = family(claims["sid"]), family(_claims(switched.json()["access_token"])["sid"])
    assert before is not None and before[0] is not None
    assert after == before  # the new family names the same browser and sign-in time


# ---- 11. step-up: 1800 s, and the admin sign-up decisions no longer ask ----


async def test_step_up_window_is_1800_seconds(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    from app.core.security import encode, issue_claims

    user = await _enrolled(api, auth_db, monkeypatch, clock)
    sid = _claims(user.access)["sid"]
    target = world.user("reviewer@acme.test")
    for age, status in ((1790, 200), (1801, 403)):
        token = issue_claims(
            sub=world.user(EMAIL),
            tid=world.tenant("acme"),
            role="owner",
            email=EMAIL,
            typ="access",
            amr=["pwd", "otp"],
            mfa_at=int(time.time()) - age,
            sid=sid,
        )
        response = await api.patch(
            f"{MEMBERS}/{target}", json={"role": "operator"}, headers=bearer(encode(token))
        )
        assert response.status_code == status, (age, response.text)


async def test_admin_signup_decisions_need_no_step_up(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .auth_support import tokens_for

    auth_db.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [OTHER])
    mfa_on(monkeypatch)
    signup = await api.post(
        "/api/v1/signup",
        json={
            "brand_name": "Kalakar Weaves",
            "owner_name": "Meera Iyer",
            "email": "founder@kalakar.test",
            "password": "Quiet-Harbour-Lamp-7305",
        },
    )
    assert signup.status_code == 202, signup.text
    rid = signup.json()["request_id"]
    stale = tokens_for(world, OTHER, "acme", amr=["pwd", "otp"], mfa_at=int(time.time()) - 86400)
    # a non-admin is still refused
    owner = tokens_for(world, EMAIL, "acme", amr=["pwd", "otp"], mfa_at=int(time.time()))
    queue = "/api/v1/admin/signup-requests"
    refused = await api.post(f"{queue}/{rid}/reject", json={"reason": "No."}, headers=bearer(owner))
    assert refused.status_code == 403
    assert refused.json()["error"]["type"] == "PERMISSION_DENIED"
    rejected = await api.post(
        f"{queue}/{rid}/reject", json={"reason": "Could not verify."}, headers=bearer(stale)
    )
    assert rejected.status_code == 200, rejected.text


# ---- 12. revocation on credential changes ----


async def test_self_password_change_forgets_browsers_keeps_this_session(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    trusted = await _trust(api, auth_db, user, clock)
    auth_db.execute(
        "UPDATE users SET password_changed_at = now() - interval '2 days' WHERE email = %s",
        [EMAIL],
    )
    changed = await api.post(
        PASSWORD_CHANGE,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=bearer(trusted["access_token"]),
    )
    assert changed.status_code == 204, changed.text
    assert _devices(auth_db, EMAIL) == []
    kept = await api.post(REFRESH, json={"refresh_token": trusted["refresh_token"]})
    assert kept.status_code == 200, kept.text
    again = (
        await _login(api, auth_db, EMAIL, token=trusted["device_token"], password=NEW_PASSWORD)
    ).json()
    assert again["status"] == "mfa_required" and again["device_token_invalid"] is True


async def test_authenticator_replace_forgets_browsers(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    from .mfa_support import secret_of

    user = await _enrolled(api, auth_db, monkeypatch, clock)
    trusted = await _trust(api, auth_db, user, clock)
    headers = bearer(trusted["access_token"])
    started = await api.post(TOTP_ENROLL, json={}, headers=headers)
    assert started.status_code == 200, started.text
    confirmed = await api.post(
        TOTP_CONFIRM, json={"code": clock.next_code(secret_of(started.json()))}, headers=headers
    )
    assert confirmed.status_code == 200, confirmed.text
    assert _devices(auth_db, EMAIL) == []
    assert "mfa_changed" in [r[-1] for r in audit_rows(auth_db, "auth.device_revoked")]


# ---- the device routes ----


@pytest.mark.endpoint("auth_devices_list", case="success")
async def test_list_shows_this_browser(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    trusted = await _trust(api, auth_db, user, clock)
    session = (await _login(api, auth_db, EMAIL, token=trusted["device_token"])).json()
    listed = await api.get(DEVICES, headers=bearer(session["access_token"]))
    assert listed.status_code == 200, listed.text
    [device] = listed.json()["devices"]
    assert device["label"] == "Chrome on macOS" and device["current"] is True
    assert device["last_used_at"] is not None and "token" not in str(device)
    # another user's list never shows it
    other = await enrol(api, auth_db, OTHER, clock)
    theirs = await api.get(DEVICES, headers=other.headers())
    assert theirs.json() == {"devices": []}


@pytest.mark.endpoint("auth_devices_remove", case="success")
async def test_remove_forgets_the_browser_and_ends_its_sessions(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    trusted = await _trust(api, auth_db, user, clock)
    session = (await _login(api, auth_db, EMAIL, token=trusted["device_token"])).json()
    [row] = _devices(auth_db, EMAIL)
    other = await enrol(api, auth_db, OTHER, clock)
    # another user's browser id: 404, untouched
    foreign = await api.delete(f"{DEVICES}/{row[0]}", headers=other.headers())
    assert foreign.status_code == 404
    assert foreign.json()["error"]["type"] == "NOT_FOUND"
    removed = await api.delete(f"{DEVICES}/{row[0]}", headers=user.headers())
    assert removed.status_code == 204
    assert _devices(auth_db, EMAIL) == []
    assert (
        await api.post(REFRESH, json={"refresh_token": session["refresh_token"]})
    ).status_code == 401
    assert [r[-1] for r in audit_rows(auth_db, "auth.device_revoked")] == ["removed"]
    again = await api.delete(f"{DEVICES}/{row[0]}", headers=user.headers())
    assert again.status_code == 404
    next_login = (await _login(api, auth_db, EMAIL, token=session["device_token"])).json()
    assert next_login["status"] == "mfa_required"


@pytest.mark.endpoint("auth_devices_remove", case="validation")
async def test_remove_validation_422(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    response = await api.delete(f"{DEVICES}/not-a-uuid", headers=user.headers())
    assert response.status_code == 422


@pytest.mark.endpoint("auth_devices_sign_out_all", case="success")
async def test_sign_out_all_keeps_this_session_only(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    add_member(auth_db, world.tenant("bharat"), world.user(EMAIL), "operator")
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    first = await _trust(api, auth_db, user, clock)
    second = await _trust(api, auth_db, user, clock)
    claims = _claims(second["access_token"])
    other = "bharat" if claims["tid"] == str(world.tenant("acme")) else "acme"
    elsewhere = await api.post(
        SWITCH,
        json={"target_tenant_id": str(world.tenant(other))},
        headers=bearer(first["access_token"]),
    )
    assert elsewhere.status_code == 200
    done = await api.post(SIGN_OUT_ALL, headers=bearer(second["access_token"]))
    assert done.status_code == 204, done.text
    assert _devices(auth_db, EMAIL) == []
    assert (
        await api.post(REFRESH, json={"refresh_token": first["refresh_token"]})
    ).status_code == 401
    assert (
        await api.post(REFRESH, json={"refresh_token": elsewhere.json()["refresh_token"]})
    ).status_code == 401
    kept = await api.post(REFRESH, json={"refresh_token": second["refresh_token"]})
    assert kept.status_code == 200, kept.text
    assert "sign_out_all" in [r[-1] for r in audit_rows(auth_db, "auth.device_revoked")]
    again = (await _login(api, auth_db, EMAIL, token=second["device_token"])).json()
    assert again["status"] == "mfa_required"


# ---- 14. the lockout is untouched ----


async def test_trusted_sign_in_clears_the_counter_like_any_sign_in(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    token = (await _trust(api, auth_db, user, clock))["device_token"]
    set_failures(auth_db, EMAIL, 9)
    signed = (await _login(api, auth_db, EMAIL, token=token)).json()
    assert signed["status"] == "signed_in"
    assert lock_state(auth_db, EMAIL) == (0, False)


async def test_ten_wrong_passwords_still_lock_with_a_live_token(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    user = await _enrolled(api, auth_db, monkeypatch, clock)
    token = (await _trust(api, auth_db, user, clock))["device_token"]
    for _ in range(10):
        await _login(api, auth_db, EMAIL, token=token, password="Wrong-Password-0000")
    assert lock_state(auth_db, EMAIL)[1] is True
    assert (await _login(api, auth_db, EMAIL, token=token)).status_code == 401


# ---- 15. under `off` the fields are ignored and the list works ----


async def test_off_ignores_the_fields(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    from .auth_support import tokens_for

    answer = await _login(api, auth_db, EMAIL, token="A" * 43)
    assert answer.status_code == 200
    body = answer.json()
    assert body["status"] == "signed_in" and body["device_token"] is None
    assert body["device_token_invalid"] is None and body["device_trust_days"] is None
    listed = await api.get(DEVICES, headers=bearer(tokens_for(world, EMAIL, "acme")))
    assert listed.status_code == 200 and listed.json() == {"devices": []}
    assert count(auth_db, "SELECT count(*) FROM trusted_devices") == 0


@pytest.mark.endpoint("auth_devices_sign_out_all", case="missing_auth")
@pytest.mark.endpoint("auth_devices_remove", case="missing_auth")
@pytest.mark.endpoint("auth_devices_list", case="missing_auth")
async def test_device_routes_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    assert (await api.get(DEVICES)).status_code == 401
    assert (await api.delete(f"{DEVICES}/{uuid.uuid4()}")).status_code == 401
    assert (await api.post(SIGN_OUT_ALL)).status_code == 401


async def _all_401(api: httpx.AsyncClient, token: str) -> None:
    headers = bearer(token)
    assert (await api.get(DEVICES, headers=headers)).status_code == 401
    assert (await api.delete(f"{DEVICES}/{uuid.uuid4()}", headers=headers)).status_code == 401
    assert (await api.post(SIGN_OUT_ALL, headers=headers)).status_code == 401


@pytest.mark.endpoint("auth_devices_sign_out_all", case="invalid_token", variant="expired")
@pytest.mark.endpoint("auth_devices_remove", case="invalid_token", variant="expired")
@pytest.mark.endpoint("auth_devices_list", case="invalid_token", variant="expired")
async def test_device_routes_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    await _all_401(api, expired_token(world, EMAIL, "acme"))


@pytest.mark.endpoint("auth_devices_sign_out_all", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("auth_devices_remove", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("auth_devices_list", case="invalid_token", variant="bad_signature")
async def test_device_routes_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    await _all_401(api, forged_token(world, EMAIL, "acme"))


@pytest.mark.endpoint("auth_devices_sign_out_all", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("auth_devices_remove", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("auth_devices_list", case="invalid_token", variant="wrong_typ")
async def test_device_routes_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    await _all_401(api, refresh_typ_token(world, EMAIL, "acme"))


@pytest.mark.endpoint("auth_devices_sign_out_all", case="wrong_tenant")
@pytest.mark.endpoint("auth_devices_remove", case="wrong_tenant")
@pytest.mark.endpoint("auth_devices_list", case="wrong_tenant")
async def test_device_routes_foreign_tenant_401(api: httpx.AsyncClient, world: World) -> None:
    await _all_401(api, foreign_tenant_token(world, EMAIL))
