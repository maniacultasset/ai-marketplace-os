"""DPP-1 AC18 (ADR-0021): sessions. Refresh and the tenant switch keep `amr`, `mfa_at` and `sid`;
the step-up updates only the family named by `sid`; a guarded route answers 403 without a fresh
code and passes within 600 s; the switch refuses a pre-MFA or stale session under `all`."""

from __future__ import annotations

from typing import Any

import httpx
import jwt
import pytest

from .auth_support import Conn, World, add_member, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS
from .mfa_support import (
    RECOVERY_CODES,
    REFRESH,
    SECURITY,
    STEP_UP,
    SWITCH,
    TotpClock,
    clock,  # noqa: F401 - the fixture
    count,
    enrol,
    expired_token,
    foreign_tenant_token,
    forged_token,
    lock,
    mfa_on,
    refresh_typ_token,
    user_id,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

EMAIL = "owner@acme.test"
MEMBERS = "/api/v1/members"


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


async def test_refresh_keeps_amr_mfa_at_sid(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    before = _claims(user.access)
    rotated = await api.post(REFRESH, json={"refresh_token": user.refresh})
    assert rotated.status_code == 200, rotated.text
    after = _claims(rotated.json()["access_token"])
    assert (after["amr"], after["mfa_at"], after["sid"]) == (
        before["amr"],
        before["mfa_at"],
        before["sid"],
    )


async def test_switch_keeps_amr_mfa_at_new_sid(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    add_member(auth_db, world.tenant("bharat"), world.user(EMAIL), "operator")
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    before = _claims(user.access)
    other = "bharat" if before["tid"] == str(world.tenant("acme")) else "acme"
    switched = await api.post(
        SWITCH, json={"target_tenant_id": str(world.tenant(other))}, headers=user.headers()
    )
    assert switched.status_code == 200, switched.text
    after = _claims(switched.json()["access_token"])
    assert after["amr"] == before["amr"] and after["mfa_at"] == before["mfa_at"]
    assert after["sid"] != before["sid"]


async def test_switch_refused_for_pre_mfa_or_stale_mfa_at_under_all(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    add_member(auth_db, world.tenant("bharat"), world.user(EMAIL), "operator")
    target = {"target_tenant_id": str(world.tenant("bharat"))}
    # under off the switch works with any live session
    assert (
        await api.post(SWITCH, json=target, headers=bearer(tokens_for(world, EMAIL, "acme")))
    ).status_code == 200
    mfa_on(monkeypatch)
    families = count(auth_db, "SELECT count(*) FROM refresh_tokens")
    pre_mfa = bearer(tokens_for(world, EMAIL, "acme"))
    assert (await api.post(SWITCH, json=target, headers=pre_mfa)).status_code == 401
    from app.core.security import encode, issue_claims

    stale_claims = issue_claims(
        sub=world.user(EMAIL),
        tid=world.tenant("acme"),
        role="owner",
        email=EMAIL,
        typ="access",
        amr=["pwd", "otp"],
        mfa_at=int(time.time()) - 7 * 24 * 3600,
        sid=world.user(EMAIL),
    )
    stale = bearer(encode(stale_claims))
    assert (await api.post(SWITCH, json=target, headers=stale)).status_code == 401
    assert count(auth_db, "SELECT count(*) FROM refresh_tokens") == families


@pytest.mark.endpoint("auth_mfa_step_up", case="success")
async def test_step_up_updates_only_sid_family(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    from .mfa_support import sign_in_with_code

    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    other = await sign_in_with_code(api, auth_db, user, clock)
    auth_db.execute("UPDATE refresh_tokens SET mfa_at = now() - interval '1 day'")
    stepped = await api.post(
        STEP_UP, json={"code": clock.next_code(user.secret)}, headers=user.headers()
    )
    assert stepped.status_code == 200, stepped.text
    body = stepped.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    claims = _claims(body["access_token"])
    assert claims["sid"] == _claims(user.access)["sid"] and claims["amr"] == ["pwd", "otp"]
    fresh = auth_db.execute(
        "SELECT family_id::text, mfa_at > now() - interval '1 minute' FROM refresh_tokens "
        "WHERE revoked_at IS NULL"
    ).fetchall()
    assert dict(fresh) == {
        _claims(user.access)["sid"]: True,
        _claims(other["access_token"])["sid"]: False,
    }


@pytest.mark.endpoint("auth_mfa_step_up", case="validation")
async def test_step_up_validation_422(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch)
    headers = bearer(tokens_for(world, EMAIL, "acme"))
    for body in ({}, {"code": "12345"}, {"code": "123456", "recovery_code": "x"}):
        assert (await api.post(STEP_UP, json=body, headers=headers)).status_code == 422


async def test_step_up_other_users_sid_401(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    from app.core.security import encode, issue_claims

    mfa_on(monkeypatch)
    victim = await enrol(api, auth_db, "operator@acme.test", clock)
    user = await enrol(api, auth_db, EMAIL, clock)
    forged = issue_claims(
        sub=world.user(EMAIL),
        tid=world.tenant("acme"),
        role="owner",
        email=EMAIL,
        typ="access",
        amr=["pwd", "otp"],
        sid=_claims(victim.access)["sid"],
    )
    response = await api.post(
        STEP_UP, json={"code": clock.next_code(user.secret)}, headers=bearer(encode(forged))
    )
    assert response.status_code == 401


async def test_step_up_revoked_family_401_and_code_not_burned(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    from .mfa_support import sign_in_with_code

    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    live = await sign_in_with_code(api, auth_db, user, clock)
    auth_db.execute(
        "UPDATE refresh_tokens SET revoked_at = now(), revoked_reason = 'logout' "
        "WHERE family_id = %s",
        [_claims(user.access)["sid"]],
    )
    code = clock.next_code(user.secret)
    refused = await api.post(STEP_UP, json={"code": code}, headers=user.headers())
    assert refused.status_code == 401
    accepted = await api.post(STEP_UP, json={"code": code}, headers=bearer(live["access_token"]))
    assert accepted.status_code == 200, accepted.text  # the savepoint rolled the code back


async def test_step_up_without_sid_401(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    no_sid = bearer(tokens_for(world, EMAIL, "acme"))
    response = await api.post(STEP_UP, json={"code": clock.next_code(user.secret)}, headers=no_sid)
    assert response.status_code == 401


async def test_step_up_refused_while_locked_same_body_as_wrong_code(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    wrong = await api.post(STEP_UP, json={"code": "000000"}, headers=user.headers())
    assert wrong.status_code == 422
    lock(auth_db, EMAIL)
    locked = await api.post(
        STEP_UP, json={"code": clock.next_code(user.secret)}, headers=user.headers()
    )
    assert locked.status_code == 422
    assert _body(locked) == _body(wrong)  # review CR2: the lock is not visible


def _body(response: httpx.Response) -> dict[str, Any]:
    error = dict(response.json()["error"])
    error.pop("correlation_id", None)
    report = dict(error.get("error_report") or {})
    report.pop("request_id", None)
    error["error_report"] = report
    return error


async def test_guarded_route_403_step_up_required_then_passes_within_600s(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    from app.core.security import encode, issue_claims

    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    target = world.user("reviewer@acme.test")
    stale = issue_claims(
        sub=world.user(EMAIL),
        tid=world.tenant("acme"),
        role="owner",
        email=EMAIL,
        typ="access",
        amr=["pwd", "otp"],
        mfa_at=_claims(user.access)["mfa_at"] - 601,
        sid=_claims(user.access)["sid"],
    )
    path = f"{MEMBERS}/{target}"
    refused = await api.patch(path, json={"role": "operator"}, headers=bearer(encode(stale)))
    assert refused.status_code == 403
    assert refused.json()["error"]["type"] == "MFA_STEP_UP_REQUIRED"
    stepped = await api.post(
        STEP_UP, json={"code": clock.next_code(user.secret)}, headers=user.headers()
    )
    ok = await api.patch(
        path, json={"role": "operator"}, headers=bearer(stepped.json()["access_token"])
    )
    assert ok.status_code == 200, ok.text


async def test_guarded_route_403_enrollment_required_without_factor(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch)
    target = world.user("reviewer@acme.test")
    response = await api.patch(
        f"{MEMBERS}/{target}",
        json={"role": "operator"},
        headers=bearer(tokens_for(world, EMAIL, "acme")),
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "MFA_ENROLLMENT_REQUIRED"


async def test_admin_route_refuses_rc_step_up(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    from . import factories as f

    account = f.marketplace_account(
        auth_db, world.tenant("acme"), connected_by_user_id=world.user(EMAIL)
    )
    stepped = await api.post(
        STEP_UP, json={"recovery_code": user.recovery_codes[0]}, headers=user.headers()
    )
    response = await api.post(
        f"/api/v1/marketplaces/accounts/{account}/revoke",
        headers=bearer(stepped.json()["access_token"]),
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "MFA_STEP_UP_REQUIRED"


async def test_off_guard_is_noop(api: httpx.AsyncClient, world: World) -> None:
    target = world.user("reviewer@acme.test")
    response = await api.patch(
        f"{MEMBERS}/{target}",
        json={"role": "operator"},
        headers=bearer(tokens_for(world, EMAIL, "acme")),
    )
    assert response.status_code == 200, response.text


# ---- recovery codes and the security card ----


@pytest.mark.endpoint("auth_mfa_recovery_codes", case="success")
async def test_recovery_codes_regenerate_after_step_up(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    fresh = await api.post(RECOVERY_CODES, headers=user.headers())
    assert fresh.status_code == 200, fresh.text
    codes = fresh.json()["recovery_codes"]
    assert len(codes) == 10 and not set(codes) & set(user.recovery_codes)
    uid = user_id(auth_db, EMAIL)
    assert (
        count(auth_db, "SELECT count(*) FROM user_recovery_codes WHERE user_id = %s", [uid]) == 10
    )


@pytest.mark.endpoint("auth_security", case="success")
async def test_security_card(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    off = await api.get(SECURITY, headers=bearer(tokens_for(world, EMAIL, "acme")))
    assert off.status_code == 200 and off.json()["enforcement"] == "off"
    assert off.json()["mfa_enrolled"] is False
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    body = (await api.get(SECURITY, headers=user.headers())).json()
    assert body["mfa_enrolled"] is True and body["recovery_codes_left"] == 10
    assert body["enforcement"] == "all" and body["password_expires_at"]


@pytest.mark.endpoint("auth_mfa_step_up", case="missing_auth")
@pytest.mark.endpoint("auth_mfa_recovery_codes", case="missing_auth")
@pytest.mark.endpoint("auth_security", case="missing_auth")
async def test_session_routes_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    assert (await api.post(STEP_UP, json={"code": "123456"})).status_code == 401
    assert (await api.post(RECOVERY_CODES)).status_code == 401
    assert (await api.get(SECURITY)).status_code == 401


async def _all_401(api: httpx.AsyncClient, token: str) -> None:
    headers = bearer(token)
    assert (await api.post(STEP_UP, json={"code": "123456"}, headers=headers)).status_code == 401
    assert (await api.post(RECOVERY_CODES, headers=headers)).status_code == 401
    assert (await api.get(SECURITY, headers=headers)).status_code == 401


@pytest.mark.endpoint("auth_mfa_step_up", case="invalid_token", variant="expired")
@pytest.mark.endpoint("auth_mfa_recovery_codes", case="invalid_token", variant="expired")
@pytest.mark.endpoint("auth_security", case="invalid_token", variant="expired")
async def test_session_routes_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    await _all_401(api, expired_token(world, EMAIL, "acme"))


@pytest.mark.endpoint("auth_mfa_step_up", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("auth_mfa_recovery_codes", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("auth_security", case="invalid_token", variant="bad_signature")
async def test_session_routes_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    await _all_401(api, forged_token(world, EMAIL, "acme"))


@pytest.mark.endpoint("auth_mfa_step_up", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("auth_mfa_recovery_codes", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("auth_security", case="invalid_token", variant="wrong_typ")
async def test_session_routes_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    await _all_401(api, refresh_typ_token(world, EMAIL, "acme"))


@pytest.mark.endpoint("auth_mfa_step_up", case="wrong_tenant")
@pytest.mark.endpoint("auth_mfa_recovery_codes", case="wrong_tenant")
@pytest.mark.endpoint("auth_security", case="wrong_tenant")
async def test_session_routes_foreign_tenant_401(api: httpx.AsyncClient, world: World) -> None:
    await _all_401(api, foreign_tenant_token(world, EMAIL))
