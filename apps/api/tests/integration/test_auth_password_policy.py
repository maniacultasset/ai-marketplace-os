"""DPP-1 AC19 (ADR-0021; DPP 1.4.1 read literally): the password rules on every write path. A
change within a day is 422; any of the last 10 is 422 and the 11th-oldest is allowed; past 365
days, staff-set or non-compliant, the login asks for a change, then `/auth/password/expired`,
then tokens; a change revokes the other sessions."""

from __future__ import annotations

import itertools
from typing import Any

import httpx
import pytest

from .auth_support import PASSWORD, Conn, World, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS
from .mfa_support import (
    LOGIN_MFA,
    NEW_PASSWORD,
    PASSWORD_CHANGE,
    PASSWORD_EXPIRED,
    REFRESH,
    TotpClock,
    clock,  # noqa: F401 - the fixture
    enrol,
    expired_token,
    foreign_tenant_token,
    forged_token,
    lock_state,
    login,
    mark_signed_in_before,
    mfa_on,
    refresh_typ_token,
    user_id,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

EMAIL = "owner@acme.test"


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _age(conn: Conn, days: int, email: str = EMAIL) -> None:
    conn.execute(
        "UPDATE users SET password_changed_at = now() - make_interval(days => %s) "
        "WHERE email = %s",
        [days, email],
    )


async def _change(
    api: httpx.AsyncClient, headers: dict[str, str], new: str, current: str = PASSWORD
) -> httpx.Response:
    return await api.post(
        PASSWORD_CHANGE, json={"current_password": current, "new_password": new}, headers=headers
    )


@pytest.mark.endpoint("auth_password_change", case="success")
async def test_change_revokes_other_families(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    _age(auth_db, 2)
    other = (await login(api, EMAIL)).json()
    mine = (await login(api, EMAIL)).json()
    response = await _change(api, bearer(mine["access_token"]), NEW_PASSWORD)
    assert response.status_code == 204, response.text
    # with MFA off the token has no sid, so every family ends, the caller's included
    assert (
        await api.post(REFRESH, json={"refresh_token": other["refresh_token"]})
    ).status_code == 401
    assert (await login(api, EMAIL, NEW_PASSWORD)).status_code == 200
    history = auth_db.execute(
        "SELECT count(*) FROM user_password_history WHERE user_id = %s",
        [user_id(auth_db, EMAIL)],
    ).fetchone()
    assert history == (1,)


@pytest.mark.endpoint("auth_password_change", case="validation")
@pytest.mark.parametrize(
    "body",
    [{}, {"current_password": "x"}, {"current_password": "x", "new_password": ""},
     {"current_password": "x", "new_password": "y" * 1025}],
)  # fmt: skip
async def test_change_validation_422(
    api: httpx.AsyncClient, world: World, body: dict[str, Any]
) -> None:
    headers = bearer(tokens_for(world, EMAIL, "acme"))
    assert (await api.post(PASSWORD_CHANGE, json=body, headers=headers)).status_code == 422


async def test_change_within_1_day_422(api: httpx.AsyncClient, world: World, auth_db: Conn) -> None:
    _age(auth_db, 0)
    response = await _change(api, bearer(tokens_for(world, EMAIL, "acme")), NEW_PASSWORD)
    assert response.status_code == 422
    assert "once a day" in response.json()["error"]["message"]


async def test_policy_breach_422_message_names_rule_not_input(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    _age(auth_db, 2)
    weak = "ownerpassword"
    response = await _change(api, bearer(tokens_for(world, EMAIL, "acme")), weak)
    assert response.status_code == 422
    error = response.json()["error"]
    messages = [d["msg"] for d in error["details"]]
    assert any("upper-case" in m for m in messages)
    assert any("digit" in m for m in messages)
    assert any("name" in m for m in messages)
    assert weak not in response.text and "owner@" not in response.text


async def test_reuse_each_of_last_10_422_11th_ok(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    from tests.support.rate_limits import reset_rate_limits

    headers = bearer(tokens_for(world, EMAIL, "acme"))
    passwords = [PASSWORD] + [f"Rotate-Lantern-{n:02d}x" for n in range(1, 11)]
    for previous, new in itertools.pairwise(passwords):
        _age(auth_db, 2)
        reset_rate_limits(auth_db)
        assert (await _change(api, headers, new, previous)).status_code == 204
    current = passwords[-1]
    for reused in passwords[-10:]:
        _age(auth_db, 2)
        response = await _change(api, headers, reused, current)
        assert response.status_code == 422, reused
        assert "last 10" in response.json()["error"]["message"]
    _age(auth_db, 2)
    assert (await _change(api, headers, passwords[0], current)).status_code == 204


async def test_wrong_current_password_422_counts(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    _age(auth_db, 2)
    response = await _change(
        api, bearer(tokens_for(world, EMAIL, "acme")), NEW_PASSWORD, "Wrong-Password-0000"
    )
    assert response.status_code == 422
    assert lock_state(auth_db, EMAIL) == (1, False)


async def test_expired_365_days_password_change_required_then_tokens(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    mfa_on(monkeypatch, grace=datetime.now(UTC) + timedelta(days=10))
    mark_signed_in_before(auth_db, EMAIL)
    _age(auth_db, 365)
    body = (await login(api, EMAIL)).json()
    assert body["status"] == "password_change_required"
    done = await api.post(
        PASSWORD_EXPIRED,
        json={"challenge_token": body["challenge_token"], "new_password": NEW_PASSWORD},
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "signed_in"


@pytest.mark.endpoint("auth_password_expired", case="success")
async def test_expired_password_after_code_keeps_mfa_at(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    import jwt
    from app.core.passwords import hash_password

    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    # a weak legacy password: the password step persists must-change before mfa_required
    weak = "weakpassword"
    auth_db.execute(
        "UPDATE users SET hashed_password = %s WHERE email = %s",
        [hash_password(weak), EMAIL],
    )
    first = (await login(api, EMAIL, weak)).json()
    assert first["status"] == "mfa_required"
    must = auth_db.execute(
        "SELECT password_must_change FROM users WHERE email = %s", [EMAIL]
    ).fetchone()
    assert must == (True,)
    second = (
        await api.post(
            LOGIN_MFA,
            json={
                "challenge_token": first["challenge_token"],
                "code": clock.next_code(user.secret),
            },
        )
    ).json()
    assert second["status"] == "password_change_required"
    done = await api.post(
        PASSWORD_EXPIRED,
        json={"challenge_token": second["challenge_token"], "new_password": NEW_PASSWORD},
    )
    assert done.status_code == 200, done.text
    claims = jwt.decode(done.json()["access_token"], options={"verify_signature": False})
    assert claims["amr"] == ["pwd", "otp"] and claims["mfa_at"]
    # test_expired_password_revokes_other_families: the enrolment session ended
    assert (await api.post(REFRESH, json={"refresh_token": user.refresh})).status_code == 401
    # the refresh of the new session is allowed
    assert (
        await api.post(REFRESH, json={"refresh_token": done.json()["refresh_token"]})
    ).status_code == 200


@pytest.mark.endpoint("auth_password_expired", case="validation")
@pytest.mark.parametrize(
    "body", [{}, {"challenge_token": "x"}, {"challenge_token": "x", "new_password": ""}]
)
async def test_expired_validation_422(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    mfa_on(monkeypatch)
    assert (await api.post(PASSWORD_EXPIRED, json=body)).status_code == 422


async def test_noncompliant_legacy_password_must_change_under_all(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.core.passwords import hash_password

    weak = "weakpassword"
    auth_db.execute(
        "UPDATE users SET hashed_password = %s WHERE email = %s", [hash_password(weak), EMAIL]
    )
    mfa_on(monkeypatch, grace=datetime.now(UTC) + timedelta(days=10))
    mark_signed_in_before(auth_db, EMAIL)
    body = (await login(api, EMAIL, weak)).json()
    assert body["status"] == "password_change_required"


async def test_noncompliant_password_not_persisted_under_off(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    from app.core.passwords import hash_password

    weak = "weakpassword"
    auth_db.execute(
        "UPDATE users SET hashed_password = %s WHERE email = %s", [hash_password(weak), EMAIL]
    )
    assert (await login(api, EMAIL, weak)).json()["status"] == "signed_in"
    flag = auth_db.execute(
        "SELECT password_must_change FROM users WHERE email = %s", [EMAIL]
    ).fetchone()
    assert flag == (False,)


async def test_signup_weak_password_422_before_lookup(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    form = {
        "brand_name": "Acme Two",
        "owner_name": "Asha Rao",
        "email": EMAIL,
        "password": "asharao-pass-word",
    }
    response = await api.post("/api/v1/signup", json=form)
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "The password does not meet the password rules."
    assert lock_state(auth_db, EMAIL) == (0, False)


@pytest.mark.endpoint("auth_password_change", case="missing_auth")
async def test_change_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    assert (await api.post(PASSWORD_CHANGE, json=body)).status_code == 401


@pytest.mark.endpoint("auth_password_change", case="invalid_token", variant="expired")
async def test_change_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    response = await _change(api, bearer(expired_token(world, EMAIL, "acme")), NEW_PASSWORD)
    assert response.status_code == 401


@pytest.mark.endpoint("auth_password_change", case="invalid_token", variant="bad_signature")
async def test_change_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    response = await _change(api, bearer(forged_token(world, EMAIL, "acme")), NEW_PASSWORD)
    assert response.status_code == 401


@pytest.mark.endpoint("auth_password_change", case="invalid_token", variant="wrong_typ")
async def test_change_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    response = await _change(api, bearer(refresh_typ_token(world, EMAIL, "acme")), NEW_PASSWORD)
    assert response.status_code == 401


@pytest.mark.endpoint("auth_password_change", case="wrong_tenant")
async def test_change_foreign_tenant_401(api: httpx.AsyncClient, world: World) -> None:
    response = await _change(api, bearer(foreign_tenant_token(world, EMAIL)), NEW_PASSWORD)
    assert response.status_code == 401


async def test_change_ends_open_sign_in_challenges(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    """Review E2E-1: a `login` challenge made with the old password dies with it."""
    from tests.support.rate_limits import reset_rate_limits

    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    stale = (await login(api, EMAIL)).json()
    assert stale["status"] == "mfa_required", stale
    _age(auth_db, 2)
    assert (await _change(api, user.headers(), NEW_PASSWORD)).status_code == 204
    reset_rate_limits(auth_db)
    response = await api.post(
        LOGIN_MFA,
        json={"challenge_token": stale["challenge_token"], "code": clock.next_code(user.secret)},
    )
    assert response.status_code == 401
