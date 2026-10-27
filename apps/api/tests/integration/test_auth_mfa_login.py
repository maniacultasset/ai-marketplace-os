"""DPP-1 AC14, AC21 (ADR-0021): the sign-in with MFA through the real app as app_rw under FORCE
RLS: the password step answers `mfa_required` with no token, the code step answers tokens with
`amr` and `mfa_at` (and the refresh row stores both), an inactive user or a membership-less
applicant keeps today's 401, and with MFA off the login is today's."""

from __future__ import annotations

from typing import Any

import httpx
import jwt
import pytest

from .auth_support import Conn, World, build_world
from .conftest import LOCAL_HOSTS
from .mfa_support import (
    LOGIN_MFA,
    TotpClock,
    clock,  # noqa: F401 - the fixture
    count,
    enrol,
    lock_state,
    login,
    mark_signed_in_before,
    mfa_on,
    set_failures,
    sign_in_with_code,
    user_id,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

EMAIL = "owner@acme.test"


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _payload(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


async def test_password_step_returns_mfa_required_no_tokens(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    await enrol(api, auth_db, EMAIL, clock)
    body = (await login(api, EMAIL)).json()
    assert body["status"] == "mfa_required"
    assert body["access_token"] is None and body["refresh_token"] is None
    assert body["challenge_token"] and body["challenge_expires_in"] == 300


@pytest.mark.endpoint("auth_login_mfa", case="success")
async def test_code_returns_tokens_amr_pwd_otp_and_mfa_at(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    body = await sign_in_with_code(api, auth_db, user, clock)
    assert body["status"] == "signed_in"
    claims = _payload(body["access_token"])
    assert claims["amr"] == ["pwd", "otp"] and claims["mfa_at"] <= claims["iat"]
    assert claims["sid"]
    # test_refresh_row_stores_amr_mfa_at
    row = auth_db.execute(
        "SELECT amr, mfa_at IS NOT NULL FROM refresh_tokens WHERE family_id = %s",
        [claims["sid"]],
    ).fetchone()
    assert row == (["pwd", "otp"], True)
    # test_challenge_routes_set_app_user_id: the full sign-in cleared the counter, which needs
    # app.user_id set from the challenge
    assert lock_state(auth_db, EMAIL) == (0, False)


@pytest.mark.endpoint("auth_login_mfa", case="validation")
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"challenge_token": "x"},
        {"challenge_token": "x", "code": "12345"},
        {"challenge_token": "x", "code": "123456", "recovery_code": "abcd"},
        {"challenge_token": "x" * 257, "code": "123456"},
    ],
)
async def test_login_mfa_validation_422(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    mfa_on(monkeypatch)
    response = await api.post(LOGIN_MFA, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


async def test_counter_clears_only_after_the_code(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    set_failures(auth_db, EMAIL, 4)
    first = (await login(api, EMAIL)).json()
    assert first["status"] == "mfa_required"
    assert lock_state(auth_db, EMAIL) == (4, False)  # the password alone clears nothing
    await api.post(
        LOGIN_MFA,
        json={"challenge_token": first["challenge_token"], "code": clock.next_code(user.secret)},
    )
    assert lock_state(auth_db, EMAIL) == (0, False)


async def test_off_login_is_signed_in_with_todays_fields(
    api: httpx.AsyncClient, world: World
) -> None:
    body = (await login(api, EMAIL)).json()
    assert body["status"] == "signed_in"
    assert body["challenge_token"] is None and body["mfa_grace_until"] is None
    assert {"access_token", "refresh_token", "token_type", "expires_in", "memberships"} <= set(body)
    claims = _payload(body["access_token"])
    assert set(claims) == {"sub", "tid", "roles", "email", "typ", "jti", "iss", "aud", "iat", "exp"}


async def test_off_enrolled_user_still_signs_in_without_code(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    from .mfa_support import mfa_off

    mfa_on(monkeypatch)
    await enrol(api, auth_db, EMAIL, clock)
    mfa_off(monkeypatch)
    body = (await login(api, EMAIL)).json()
    assert body["status"] == "signed_in" and body["access_token"]


async def test_recovery_code_amr_pwd_rc(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    first = (await login(api, EMAIL)).json()
    second = await api.post(
        LOGIN_MFA,
        json={
            "challenge_token": first["challenge_token"],
            "recovery_code": user.recovery_codes[0].upper().replace("-", " "),
        },
    )
    assert second.status_code == 200, second.text
    assert _payload(second.json()["access_token"])["amr"] == ["pwd", "rc"]


@pytest.mark.parametrize("change", ["inactive", "no_membership"])
async def test_inactive_or_membershipless_right_password_401_not_mfa_required(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
    change: str,
) -> None:
    mfa_on(monkeypatch)
    await enrol(api, auth_db, EMAIL, clock)
    if change == "inactive":
        auth_db.execute("UPDATE users SET is_active = false WHERE email = %s", [EMAIL])
    else:
        auth_db.execute(
            "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() "
            "WHERE user_id = %s",
            [user_id(auth_db, EMAIL)],
        )
    before = count(auth_db, "SELECT count(*) FROM mfa_challenges")
    response = await login(api, EMAIL)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "UNAUTHENTICATED"
    assert lock_state(auth_db, EMAIL) == (0, False)
    assert count(auth_db, "SELECT count(*) FROM mfa_challenges") == before


async def test_login_mfa_rechecks_active(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    first = (await login(api, EMAIL)).json()
    auth_db.execute("UPDATE users SET is_active = false WHERE email = %s", [EMAIL])
    response = await api.post(
        LOGIN_MFA,
        json={"challenge_token": first["challenge_token"], "code": clock.next_code(user.secret)},
    )
    assert response.status_code == 401
    assert lock_state(auth_db, EMAIL) == (0, False)


async def test_grace_user_without_factor_signed_in(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    grace = datetime.now(UTC) + timedelta(days=3)
    mfa_on(monkeypatch, grace=grace)
    mark_signed_in_before(auth_db, EMAIL)
    body = (await login(api, EMAIL)).json()
    assert body["status"] == "signed_in" and body["mfa_grace_until"]
