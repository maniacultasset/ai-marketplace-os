"""DPP-1 AC16 (DPP 1.2.2; ADR-0021): the account lockout. 10 failed passwords or codes lock the
account for 15 minutes behind the same 401 as an unknown email; every verify path refuses while
locked; parallel attempts are serialised by the user row lock; a counted failure survives the
error response; a lock wait past 5 s counts nothing."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from tests.support.rate_limits import reset_rate_limits

from .auth_support import PASSWORD, Conn, World, audit_rows, bearer, build_world
from .conftest import LOCAL_HOSTS
from .mfa_support import (
    LOGIN_CONFIRM,
    LOGIN_ENROLL,
    LOGIN_MFA,
    NEW_PASSWORD,
    PASSWORD_CHANGE,
    PASSWORD_EXPIRED,
    STEP_UP,
    TOTP_CONFIRM,
    TOTP_ENROLL,
    TotpClock,
    clock,  # noqa: F401 - the fixture
    count,
    enrol,
    lock,
    lock_state,
    login,
    mfa_on,
    set_failures,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

EMAIL = "owner@acme.test"
WRONG = "Wrong-Password-0000"


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _shape(response: httpx.Response) -> dict[str, Any]:
    error = dict(response.json()["error"])
    error.pop("correlation_id")
    report = dict(error.get("error_report") or {})
    report.pop("request_id", None)
    error["error_report"] = report
    return error


async def _wrong_logins(api: httpx.AsyncClient, conn: Conn, n: int) -> None:
    for _ in range(n):
        reset_rate_limits(conn)
        assert (await login(api, EMAIL, WRONG)).status_code == 401


async def test_ten_password_failures_lock_under_off(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    await _wrong_logins(api, auth_db, 9)
    assert lock_state(auth_db, EMAIL) == (9, False)
    await _wrong_logins(api, auth_db, 1)
    assert lock_state(auth_db, EMAIL) == (0, True)
    reset_rate_limits(auth_db)
    locked = await login(api, EMAIL)
    unknown = await login(api, "nobody@acme.test", WRONG)
    assert locked.status_code == unknown.status_code == 401
    # test_locked_right_password_same_401_body_as_unknown_email
    assert _shape(locked) == _shape(unknown)
    # after 15 minutes the right password signs in and clears the counter
    auth_db.execute(
        "UPDATE users SET locked_until = now() - interval '1 second' WHERE email = %s", [EMAIL]
    )
    reset_rate_limits(auth_db)
    assert (await login(api, EMAIL)).status_code == 200
    assert lock_state(auth_db, EMAIL) == (0, False)


async def test_ten_mixed_failures_lock(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    await enrol(api, auth_db, EMAIL, clock)
    await _wrong_logins(api, auth_db, 5)
    for _ in range(5):
        reset_rate_limits(auth_db)
        first = (await login(api, EMAIL)).json()
        bad = await api.post(
            LOGIN_MFA, json={"challenge_token": first["challenge_token"], "code": "000000"}
        )
        assert bad.status_code == 401
    assert lock_state(auth_db, EMAIL) == (0, True)
    [event] = audit_rows(auth_db, "auth.locked")
    assert event[1] == "system"


async def test_auth_locked_audit_and_log(
    api: httpx.AsyncClient, world: World, auth_db: Conn, caplog: pytest.LogCaptureFixture
) -> None:
    set_failures(auth_db, EMAIL, 9)
    reset_rate_limits(auth_db)
    await login(api, EMAIL, WRONG)
    assert len(audit_rows(auth_db, "auth.locked")) == 1
    assert "auth.locked" in caplog.text


async def test_locked_path_runs_exactly_one_argon2_verify(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.passwords import verify_password as real

    calls: list[str] = []

    def spy(plain: str, hashed: str) -> bool:
        calls.append(hashed[:9])
        return real(plain, hashed)

    monkeypatch.setattr("app.services.auth.verify_password", spy)
    lock(auth_db, EMAIL)
    assert (await login(api, EMAIL)).status_code == 401
    assert len(calls) == 1


async def test_right_password_wrong_codes_do_not_reset(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    await enrol(api, auth_db, EMAIL, clock)
    for n in range(1, 4):
        reset_rate_limits(auth_db)
        first = (await login(api, EMAIL)).json()
        await api.post(
            LOGIN_MFA, json={"challenge_token": first["challenge_token"], "code": "000000"}
        )
        assert lock_state(auth_db, EMAIL) == (n, False)


async def test_full_sign_in_resets(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    set_failures(auth_db, EMAIL, 7)
    assert (await login(api, EMAIL)).status_code == 200
    assert lock_state(auth_db, EMAIL) == (0, False)


async def test_signup_status_counts_and_honours_lock(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    status = "/api/v1/signup/status"
    wrong = await api.post(status, json={"email": EMAIL, "password": WRONG})
    assert wrong.status_code == 401 and lock_state(auth_db, EMAIL) == (1, False)
    lock(auth_db, EMAIL)
    reset_rate_limits(auth_db)
    right = await api.post(status, json={"email": EMAIL, "password": PASSWORD})
    assert right.status_code == 401


async def test_signup_existing_email_counts_and_honours_lock(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    form = {"brand_name": "Acme Two", "owner_name": "Asha Rao", "email": EMAIL}
    wrong = await api.post("/api/v1/signup", json={**form, "password": WRONG})
    assert wrong.status_code == 202 and lock_state(auth_db, EMAIL) == (1, False)
    lock(auth_db, EMAIL)
    right = await api.post("/api/v1/signup", json={**form, "password": PASSWORD})
    assert right.status_code == 202
    assert count(auth_db, "SELECT count(*) FROM signup_requests") == 0


async def test_signup_success_does_not_reset(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    set_failures(auth_db, EMAIL, 3)
    form = {"brand_name": "Acme Two", "owner_name": "Asha Rao", "email": EMAIL}
    assert (
        await api.post("/api/v1/signup", json={**form, "password": PASSWORD})
    ).status_code == 202
    assert lock_state(auth_db, EMAIL) == (3, False)


async def test_step_up_and_password_change_failures_count(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    await api.post(STEP_UP, json={"code": "000000"}, headers=user.headers())
    auth_db.execute(
        "UPDATE users SET password_changed_at = now() - interval '2 days' WHERE email = %s",
        [EMAIL],
    )
    await api.post(
        PASSWORD_CHANGE,
        json={"current_password": WRONG, "new_password": NEW_PASSWORD},
        headers=user.headers(),
    )
    assert lock_state(auth_db, EMAIL) == (2, False)


async def test_bearer_enroll_password_refused_while_locked(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.core.passwords import verify_password as real

    mfa_on(monkeypatch, grace=datetime.now(UTC) + timedelta(days=5))
    auth_db.execute(
        "UPDATE users SET last_login_at = now() - interval '1 day' WHERE email = %s", [EMAIL]
    )
    session = (await login(api, EMAIL)).json()
    calls: list[int] = []

    def spy(plain: str, hashed: str) -> bool:
        calls.append(1)
        return real(plain, hashed)

    monkeypatch.setattr("app.services.auth.verify_password", spy)
    lock(auth_db, EMAIL)
    response = await api.post(
        TOTP_ENROLL, json={"current_password": PASSWORD}, headers=bearer(session["access_token"])
    )
    assert response.status_code == 422 and calls == []


async def test_locked_every_verify_path_refuses(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    """Each of the 11 lock-checked routes refuses a right password, TOTP code or recovery code
    while the account is locked; the code paths never run `match_step` or the recovery lookup,
    and the counter does not move."""
    import app.core.totp as totp_module
    import app.repositories.mfa as mfa_repositories

    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    first = (await login(api, EMAIL)).json()  # a live login challenge, made before the lock
    login_challenge = first["challenge_token"]
    matches: list[int] = []
    consumes: list[int] = []
    real_match = totp_module.match_step
    real_consume = mfa_repositories.RecoveryCodeRepository.consume

    def spy_match(*args: Any) -> Any:
        matches.append(1)
        return real_match(*args)

    async def spy_consume(self: Any, *args: Any) -> Any:
        consumes.append(1)
        return await real_consume(self, *args)

    monkeypatch.setattr(totp_module, "match_step", spy_match)
    monkeypatch.setattr(mfa_repositories.RecoveryCodeRepository, "consume", spy_consume)
    lock(auth_db, EMAIL)
    reset_rate_limits(auth_db)
    code = clock.next_code(user.secret)
    headers = user.headers()
    form = {"brand_name": "Acme Two", "owner_name": "Asha Rao", "email": EMAIL}
    calls = [
        ("POST", "/api/v1/auth/login", {"email": EMAIL, "password": PASSWORD}, None, 401),
        ("POST", LOGIN_MFA, {"challenge_token": login_challenge, "code": code}, None, 401),
        ("POST", LOGIN_MFA, {"challenge_token": login_challenge,
                             "recovery_code": user.recovery_codes[0]}, None, 401),
        ("POST", LOGIN_ENROLL, {"challenge_token": login_challenge}, None, 401),
        ("POST", LOGIN_CONFIRM, {"challenge_token": login_challenge, "code": code}, None, 401),
        ("POST", STEP_UP, {"code": code}, headers, 422),
        ("POST", STEP_UP, {"recovery_code": user.recovery_codes[1]}, headers, 422),
        ("POST", TOTP_ENROLL, {}, headers, 422),
        ("POST", TOTP_CONFIRM, {"code": code}, headers, 422),
        ("POST", PASSWORD_CHANGE, {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
         headers, 422),
        ("POST", PASSWORD_EXPIRED, {"challenge_token": login_challenge,
                                    "new_password": NEW_PASSWORD}, None, 401),
        ("POST", "/api/v1/signup", {**form, "password": PASSWORD}, None, 202),
        ("POST", "/api/v1/signup/status", {"email": EMAIL, "password": PASSWORD}, None, 401),
    ]  # fmt: skip
    for method, path, body, extra, expected in calls:
        reset_rate_limits(auth_db)
        response = await api.request(method, path, json=body, headers=extra or {})
        assert response.status_code == expected, (path, response.text)
    assert matches == [] and consumes == []
    assert lock_state(auth_db, EMAIL) == (0, True)
    assert count(auth_db, "SELECT count(*) FROM signup_requests") == 0


@pytest.mark.parametrize("route", ["login_mfa", "step_up"])
async def test_parallel_wrong_codes_after_9_call_match_step_once(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
    route: str,
) -> None:
    import app.core.totp as totp_module

    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    first = (await login(api, EMAIL)).json()
    set_failures(auth_db, EMAIL, 9)
    matches: list[int] = []
    real = totp_module.match_step

    def spy(*args: Any) -> Any:
        matches.append(1)
        return real(*args)

    monkeypatch.setattr(totp_module, "match_step", spy)

    async def one() -> httpx.Response:
        if route == "login_mfa":
            return await api.post(
                LOGIN_MFA, json={"challenge_token": first["challenge_token"], "code": "000000"}
            )
        return await api.post(STEP_UP, json={"code": "000000"}, headers=user.headers())

    responses = await asyncio.gather(*(one() for _ in range(20)))
    assert len(matches) == 1
    assert {r.status_code for r in responses} <= {401, 422}
    assert lock_state(auth_db, EMAIL) == (0, True)


async def test_parallel_wrong_passwords_after_9_lock_once(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.passwords import verify_password as real

    monkeypatch.setattr("app.api.v1.routers.auth.enforce_login_limit", _no_limit)
    set_failures(auth_db, EMAIL, 9)
    calls: list[int] = []

    def spy(plain: str, hashed: str) -> bool:
        calls.append(1)
        return real(plain, hashed)

    monkeypatch.setattr("app.services.auth.verify_password", spy)
    responses = await asyncio.gather(*(login(api, EMAIL, WRONG) for _ in range(20)))
    assert {r.status_code for r in responses} == {401}
    assert len(audit_rows(auth_db, "auth.locked")) == 1
    assert lock_state(auth_db, EMAIL) == (0, True)
    assert len(calls) == 20  # argon2 still runs once per request (timing parity)


async def _no_limit(*_: Any, **__: Any) -> None:
    return None


async def test_login_and_login_mfa_same_user_no_deadlock(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    monkeypatch.setattr("app.api.v1.routers.auth.enforce_login_limit", _no_limit)
    mfa_on(monkeypatch)
    await enrol(api, auth_db, EMAIL, clock)
    for _ in range(20):
        first = (await login(api, EMAIL)).json()
        results = await asyncio.gather(
            login(api, EMAIL, WRONG),
            api.post(
                LOGIN_MFA, json={"challenge_token": first["challenge_token"], "code": "000000"}
            ),
        )
        assert all(r.status_code in (200, 401) for r in results)
        set_failures(auth_db, EMAIL, 0)


@pytest.mark.parametrize(
    "path", ["login", "login_mfa", "step_up", "password_change", "totp_confirm"]
)
async def test_counted_failure_survives_the_error_response(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
    path: str,
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    headers = user.headers()
    reset_rate_limits(auth_db)
    if path == "login":
        response = await login(api, EMAIL, WRONG)
    elif path == "login_mfa":
        first = (await login(api, EMAIL)).json()
        response = await api.post(
            LOGIN_MFA, json={"challenge_token": first["challenge_token"], "code": "000000"}
        )
        attempts = auth_db.execute(
            "SELECT attempts FROM mfa_challenges WHERE consumed_at IS NULL"
        ).fetchone()
        assert attempts == (1,)
    elif path == "step_up":
        response = await api.post(STEP_UP, json={"code": "000000"}, headers=headers)
    elif path == "password_change":
        auth_db.execute(
            "UPDATE users SET password_changed_at = now() - interval '2 days' WHERE email = %s",
            [EMAIL],
        )
        response = await api.post(
            PASSWORD_CHANGE,
            json={"current_password": WRONG, "new_password": NEW_PASSWORD},
            headers=headers,
        )
    else:
        await api.post(TOTP_ENROLL, json={}, headers=headers)
        response = await api.post(TOTP_CONFIRM, json={"code": "000000"}, headers=headers)
    assert response.status_code in (401, 422)
    assert lock_state(auth_db, EMAIL) == (1, False)
    # review CR1: a bearer refusal's audit row carries the caller's tenant and commits (it was
    # refused by `audit_insert` while `app.tenant_id` was unset: 500 and the counter rolled back)
    bearer_action = {
        "step_up": "auth.mfa_failed",
        "totp_confirm": "auth.mfa_failed",
        "password_change": "auth.password_failed",
    }.get(path)
    if bearer_action is not None:
        rows = audit_rows(auth_db, bearer_action)
        assert rows and rows[-1][0] == world.tenant("acme")


async def test_busy_attempt_returns_generic_401(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """A second connection holds the user row lock past the function's 5 s lock_timeout: the
    login answers its usual 401, counts nothing and leaves `auth.login_failed` reason busy."""
    import os

    import psycopg

    from .conftest import libpq_url

    holder = psycopg.connect(libpq_url(os.environ["ALEMBIC_DATABASE_URL"]))
    try:
        holder.execute("SELECT 1 FROM users WHERE email = %s FOR UPDATE", [EMAIL])
        reset_rate_limits(auth_db)
        response = await login(api, EMAIL)
        assert response.status_code == 401
        rows = audit_rows(auth_db, "auth.login_failed")
        assert rows and rows[-1][9] == "busy"
    finally:
        holder.rollback()
        holder.close()
    assert lock_state(auth_db, EMAIL) == (0, False)
