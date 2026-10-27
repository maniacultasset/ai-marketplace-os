"""DPP-1 AC15 (ADR-0021): abuse of the second step. A replayed code fails, the 6th attempt burns the
challenge even with the right code, an expired challenge fails, a recovery code works once, a
challenge token is not a bearer (and an access token is not a challenge), every challenge is bound
to its purpose, and one user's challenge never touches another user."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from tests.integration.auth_support import Conn, World, bearer, build_world
from tests.integration.mfa_support import (
    LOGIN_CONFIRM,
    LOGIN_ENROLL,
    LOGIN_MFA,
    NEW_PASSWORD,
    PASSWORD_EXPIRED,
    SECURITY,
    TotpClock,
    clock,  # noqa: F401 - the fixture
    enrol,
    login,
    mfa_on,
)
from tests.support.rate_limits import reset_rate_limits

EMAIL = "owner@acme.test"


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


async def _challenge(api: httpx.AsyncClient, conn: Conn, email: str = EMAIL) -> dict[str, Any]:
    reset_rate_limits(conn)
    body: dict[str, Any] = (await login(api, email)).json()
    return body


async def test_replayed_code_same_step_401(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    code = clock.next_code(user.secret)
    first = await _challenge(api, auth_db)
    ok = await api.post(LOGIN_MFA, json={"challenge_token": first["challenge_token"], "code": code})
    assert ok.status_code == 200
    second = await _challenge(api, auth_db)
    replay = await api.post(
        LOGIN_MFA, json={"challenge_token": second["challenge_token"], "code": code}
    )
    assert replay.status_code == 401


async def test_sixth_attempt_burns_challenge_even_with_right_code(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    first = await _challenge(api, auth_db)
    token = first["challenge_token"]
    for _ in range(5):
        bad = await api.post(LOGIN_MFA, json={"challenge_token": token, "code": "000000"})
        assert bad.status_code == 401
    right = await api.post(
        LOGIN_MFA, json={"challenge_token": token, "code": clock.next_code(user.secret)}
    )
    assert right.status_code == 401


async def test_expired_challenge_401(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    first = await _challenge(api, auth_db)
    auth_db.execute("UPDATE mfa_challenges SET expires_at = now() - interval '1 second'")
    response = await api.post(
        LOGIN_MFA,
        json={"challenge_token": first["challenge_token"], "code": clock.next_code(user.secret)},
    )
    assert response.status_code == 401


async def test_recovery_code_second_use_401(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    first = await _challenge(api, auth_db)
    used = {"challenge_token": first["challenge_token"], "recovery_code": user.recovery_codes[3]}
    assert (await api.post(LOGIN_MFA, json=used)).status_code == 200
    second = await _challenge(api, auth_db)
    again = {"challenge_token": second["challenge_token"], "recovery_code": user.recovery_codes[3]}
    assert (await api.post(LOGIN_MFA, json=again)).status_code == 401


async def test_challenge_token_as_bearer_401(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    await enrol(api, auth_db, EMAIL, clock)
    first = await _challenge(api, auth_db)
    response = await api.get(SECURITY, headers=bearer(first["challenge_token"]))
    assert response.status_code == 401


async def test_access_token_as_challenge_401(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    response = await api.post(
        LOGIN_MFA, json={"challenge_token": user.access[:256], "code": clock.next_code(user.secret)}
    )
    assert response.status_code == 401


async def test_enroll_challenge_on_login_mfa_401(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch)
    first = (await login(api, EMAIL)).json()
    assert first["status"] == "mfa_enrollment_required"
    response = await api.post(
        LOGIN_MFA, json={"challenge_token": first["challenge_token"], "code": "123456"}
    )
    assert response.status_code == 401


async def test_login_challenge_on_enroll_and_confirm_401(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    await enrol(api, auth_db, EMAIL, clock)
    first = await _challenge(api, auth_db)
    token = first["challenge_token"]
    # test_login_challenge_on_enroll_401
    assert (await api.post(LOGIN_ENROLL, json={"challenge_token": token})).status_code == 401
    # test_login_challenge_on_confirm_401
    confirm = await api.post(LOGIN_CONFIRM, json={"challenge_token": token, "code": "123456"})
    assert confirm.status_code == 401
    # test_login_challenge_on_password_expired_401
    expired = await api.post(
        PASSWORD_EXPIRED, json={"challenge_token": token, "new_password": NEW_PASSWORD}
    )
    assert expired.status_code == 401


async def test_password_change_challenge_on_enroll_and_login_mfa_401(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    mfa_on(monkeypatch, grace=datetime.now(UTC) + timedelta(days=5))
    auth_db.execute(
        "UPDATE users SET last_login_at = now() - interval '1 day', "
        "password_must_change = true WHERE email = %s",
        [EMAIL],
    )
    first = (await login(api, EMAIL)).json()
    assert first["status"] == "password_change_required"
    token = first["challenge_token"]
    assert (await api.post(LOGIN_ENROLL, json={"challenge_token": token})).status_code == 401
    mfa = await api.post(LOGIN_MFA, json={"challenge_token": token, "code": "123456"})
    assert mfa.status_code == 401


async def test_enroll_challenge_refused_when_active_factor_exists(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    first = (await login(api, EMAIL)).json()
    stale_enroll = first["challenge_token"]
    await enrol(api, auth_db, EMAIL, clock)  # binds a factor through another challenge
    response = await api.post(LOGIN_ENROLL, json={"challenge_token": stale_enroll})
    assert response.status_code == 401


async def test_two_parallel_submits_one_wins(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    first = await _challenge(api, auth_db)
    code = clock.next_code(user.secret)
    body = {"challenge_token": first["challenge_token"], "code": code}
    results = await asyncio.gather(api.post(LOGIN_MFA, json=body), api.post(LOGIN_MFA, json=body))
    assert sorted(r.status_code for r in results) == [200, 401]


async def test_challenge_of_user_a_cannot_touch_user_b(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    victim = await enrol(api, auth_db, "operator@acme.test", clock)
    await enrol(api, auth_db, EMAIL, clock)
    mine = await _challenge(api, auth_db)
    response = await api.post(
        LOGIN_MFA,
        json={"challenge_token": mine["challenge_token"], "code": clock.next_code(victim.secret)},
    )
    assert response.status_code == 401
