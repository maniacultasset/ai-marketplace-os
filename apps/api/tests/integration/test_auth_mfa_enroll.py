"""DPP-1 AC17 (ADR-0021): enrolment. The forced enrolment at sign-in (public challenge pair), the
settings enrolment (bearer pair), the grace rules, the lost-phone path with recovery codes, the
15-minute enrol challenge, and the 7-day re-authentication at refresh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from .auth_support import PASSWORD, Conn, World, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS
from .mfa_support import (
    LOGIN_CONFIRM,
    LOGIN_ENROLL,
    LOGIN_MFA,
    RECOVERY_CODES,
    REFRESH,
    STEP_UP,
    TOTP_CONFIRM,
    TOTP_ENROLL,
    TotpClock,
    clock,  # noqa: F401 - the fixture
    count,
    enrol,
    expired_token,
    foreign_tenant_token,
    forged_token,
    lock_state,
    login,
    mark_signed_in_before,
    mfa_on,
    refresh_typ_token,
    secret_of,
    user_id,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

EMAIL = "owner@acme.test"
# the membership tests deactivate a non-owner: `last_owner_guard` keeps acme's only owner active
MEMBER = "operator@acme.test"


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


async def _enroll_challenge(api: httpx.AsyncClient, email: str = EMAIL) -> str:
    body = (await login(api, email)).json()
    assert body["status"] == "mfa_enrollment_required", body
    assert body["challenge_expires_in"] == 900
    return str(body["challenge_token"])


# ---- the forced enrolment (public challenge pair) ----


@pytest.mark.endpoint("auth_login_mfa_enroll", case="success")
@pytest.mark.endpoint("auth_login_mfa_confirm", case="success")
async def test_forced_enrollment_flow_returns_10_codes_and_tokens(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    assert len(user.recovery_codes) == 10 and len(set(user.recovery_codes)) == 10
    assert user.result["status"] == "signed_in" and user.result["access_token"]
    uid = user_id(auth_db, EMAIL)
    assert count(auth_db, "SELECT count(*) FROM user_mfa_factors WHERE user_id = %s "
                 "AND status = 'active'", [uid]) == 1  # fmt: skip
    assert (
        count(auth_db, "SELECT count(*) FROM user_recovery_codes WHERE user_id = %s", [uid]) == 10
    )
    secret = auth_db.execute(
        "SELECT secret_blob FROM user_mfa_factors WHERE user_id = %s", [uid]
    ).fetchone()
    assert secret is not None and bytes(secret[0]).startswith(b"MFA1")
    assert user.secret not in bytes(secret[0])


@pytest.mark.endpoint("auth_login_mfa_enroll", case="validation")
@pytest.mark.parametrize("body", [{}, {"challenge_token": ""}, {"challenge_token": "x" * 257}])
async def test_login_enroll_validation_422(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    mfa_on(monkeypatch)
    assert (await api.post(LOGIN_ENROLL, json=body)).status_code == 422


@pytest.mark.endpoint("auth_login_mfa_confirm", case="validation")
@pytest.mark.parametrize(
    "body", [{}, {"challenge_token": "x"}, {"challenge_token": "x", "code": "1234567"}]
)
async def test_login_confirm_validation_422(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    mfa_on(monkeypatch)
    assert (await api.post(LOGIN_CONFIRM, json=body)).status_code == 422


async def test_admin_no_grace(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch, grace=datetime.now(UTC) + timedelta(days=10))
    mark_signed_in_before(auth_db, EMAIL)
    auth_db.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [EMAIL])
    assert (await login(api, EMAIL)).json()["status"] == "mfa_enrollment_required"


async def test_first_login_no_grace(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch, grace=datetime.now(UTC) + timedelta(days=10))
    assert (await login(api, EMAIL)).json()["status"] == "mfa_enrollment_required"


async def test_existing_user_in_grace_signed_in_with_grace_date(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch, grace=datetime.now(UTC) + timedelta(days=10))
    mark_signed_in_before(auth_db, EMAIL)
    body = (await login(api, EMAIL)).json()
    assert body["status"] == "signed_in" and body["mfa_grace_until"] is not None


async def test_grace_over_requires_enrollment(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch, grace=datetime.now(UTC) - timedelta(seconds=1))
    mark_signed_in_before(auth_db, EMAIL)
    assert (await login(api, EMAIL)).json()["status"] == "mfa_enrollment_required"


async def test_reenroll_replaces_pending(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    challenge = await _enroll_challenge(api)
    first = secret_of((await api.post(LOGIN_ENROLL, json={"challenge_token": challenge})).json())
    second = secret_of((await api.post(LOGIN_ENROLL, json={"challenge_token": challenge})).json())
    assert first != second
    uid = user_id(auth_db, EMAIL)
    assert count(auth_db, "SELECT count(*) FROM user_mfa_factors WHERE user_id = %s", [uid]) == 1
    stale = await api.post(
        LOGIN_CONFIRM, json={"challenge_token": challenge, "code": clock.next_code(first)}
    )
    assert stale.status_code == 401


async def test_confirm_wrong_code_401_counts(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    challenge = await _enroll_challenge(api)
    await api.post(LOGIN_ENROLL, json={"challenge_token": challenge})
    response = await api.post(LOGIN_CONFIRM, json={"challenge_token": challenge, "code": "000000"})
    assert response.status_code == 401
    assert lock_state(auth_db, EMAIL) == (1, False)
    attempts = auth_db.execute("SELECT attempts FROM mfa_challenges").fetchone()
    assert attempts == (1,)


async def test_confirm_consumes_enroll_challenge(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    challenge = await _enroll_challenge(api)
    secret = secret_of((await api.post(LOGIN_ENROLL, json={"challenge_token": challenge})).json())
    ok = await api.post(
        LOGIN_CONFIRM, json={"challenge_token": challenge, "code": clock.next_code(secret)}
    )
    assert ok.status_code == 200
    again = await api.post(
        LOGIN_CONFIRM, json={"challenge_token": challenge, "code": clock.next_code(secret)}
    )
    assert again.status_code == 401


async def test_confirm_rechecks_membership(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    challenge = await _enroll_challenge(api, MEMBER)
    secret = secret_of((await api.post(LOGIN_ENROLL, json={"challenge_token": challenge})).json())
    auth_db.execute(
        "UPDATE tenant_memberships SET is_active = false, deactivated_at = now() WHERE user_id = %s",
        [user_id(auth_db, MEMBER)],
    )
    response = await api.post(
        LOGIN_CONFIRM, json={"challenge_token": challenge, "code": clock.next_code(secret)}
    )
    assert response.status_code == 401


async def test_confirm_revokes_other_families(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    before = (await login(api, EMAIL)).json()  # a session from before MFA (off)
    assert before["status"] == "signed_in"
    mfa_on(monkeypatch)
    await enrol(api, auth_db, EMAIL, clock)
    old = await api.post(REFRESH, json={"refresh_token": before["refresh_token"]})
    assert old.status_code == 401


async def test_enroll_challenge_works_at_14_minutes_and_fails_at_15(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch)
    challenge = await _enroll_challenge(api)
    # `created_at` is the database's transaction start and `expires_at` the app clock at the
    # insert, a few milliseconds later: the lifetime is 15 minutes to within that gap
    expires = auth_db.execute("SELECT expires_at - created_at FROM mfa_challenges").fetchone()
    assert expires is not None
    assert abs(expires[0] - timedelta(minutes=15)) < timedelta(seconds=2), expires[0]
    # 14 minutes in: still open
    auth_db.execute("UPDATE mfa_challenges SET expires_at = now() + interval '1 minute'")
    assert (await api.post(LOGIN_ENROLL, json={"challenge_token": challenge})).status_code == 200
    # 15 minutes in: gone
    auth_db.execute("UPDATE mfa_challenges SET expires_at = now() - interval '1 second'")
    assert (await api.post(LOGIN_ENROLL, json={"challenge_token": challenge})).status_code == 401


async def test_public_enroll_pair_refuses_bearer_only(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch)
    token = tokens_for(world, EMAIL, "acme")
    response = await api.post(LOGIN_ENROLL, json={"challenge_token": "no"}, headers=bearer(token))
    assert response.status_code == 401


async def test_bearer_enroll_pair_refuses_challenge_token(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch)
    challenge = await _enroll_challenge(api)
    response = await api.post(TOTP_ENROLL, json={}, headers=bearer(challenge))
    assert response.status_code == 401


# ---- the settings enrolment (bearer pair) ----


async def _grace_session(
    api: httpx.AsyncClient, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    mfa_on(monkeypatch, grace=datetime.now(UTC) + timedelta(days=10))
    mark_signed_in_before(auth_db, EMAIL)
    body: dict[str, Any] = (await login(api, EMAIL)).json()
    assert body["status"] == "signed_in"
    return body


@pytest.mark.endpoint("auth_mfa_totp_enroll", case="success")
@pytest.mark.endpoint("auth_mfa_totp_confirm", case="success")
async def test_bearer_enroll_needs_current_password(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    session = await _grace_session(api, auth_db, monkeypatch)
    headers = bearer(session["access_token"])
    missing = await api.post(TOTP_ENROLL, json={}, headers=headers)
    assert missing.status_code == 422
    wrong = await api.post(
        TOTP_ENROLL, json={"current_password": "Wrong-Password-0000"}, headers=headers
    )
    assert wrong.status_code == 422 and lock_state(auth_db, EMAIL) == (1, False)
    ok = await api.post(TOTP_ENROLL, json={"current_password": PASSWORD}, headers=headers)
    assert ok.status_code == 200, ok.text
    secret = secret_of(ok.json())
    confirm = await api.post(TOTP_CONFIRM, json={"code": clock.next_code(secret)}, headers=headers)
    assert confirm.status_code == 200, confirm.text
    assert len(confirm.json()["recovery_codes"]) == 10
    # the caller's family recorded the code: the refresh is allowed after grace
    family = auth_db.execute(
        "SELECT amr, mfa_at IS NOT NULL FROM refresh_tokens WHERE revoked_at IS NULL"
    ).fetchall()
    assert family == [(["pwd", "otp"], True)]


@pytest.mark.endpoint("auth_mfa_totp_enroll", case="validation")
async def test_bearer_enroll_validation_422(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch)
    headers = bearer(tokens_for(world, EMAIL, "acme"))
    for body in ({"current_password": ""}, {"current_password": "x" * 1025}, {"x": 1}):
        assert (await api.post(TOTP_ENROLL, json=body, headers=headers)).status_code == 422


@pytest.mark.endpoint("auth_mfa_totp_confirm", case="validation")
async def test_bearer_confirm_validation_422(
    api: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mfa_on(monkeypatch)
    headers = bearer(tokens_for(world, EMAIL, "acme"))
    for body in ({}, {"code": "12345"}, {"code": "abcdef"}):
        assert (await api.post(TOTP_CONFIRM, json=body, headers=headers)).status_code == 422


async def test_bearer_replace_needs_step_up(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    # a token with no fresh code (one minted before MFA) is refused once a factor exists
    stale = bearer(tokens_for(world, EMAIL, "acme"))
    refused = await api.post(TOTP_ENROLL, json={}, headers=stale)
    assert refused.status_code == 403
    assert refused.json()["error"]["type"] == "MFA_STEP_UP_REQUIRED"
    # the enrolment's own token carries a code from seconds ago: fresh
    assert (await api.post(TOTP_ENROLL, json={}, headers=user.headers())).status_code == 200


async def test_bearer_replace_accepts_rc_step_up(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    stepped = await api.post(
        STEP_UP, json={"recovery_code": user.recovery_codes[0]}, headers=user.headers()
    )
    assert stepped.status_code == 200, stepped.text
    headers = bearer(stepped.json()["access_token"])
    assert (await api.post(TOTP_ENROLL, json={}, headers=headers)).status_code == 200


async def test_recovery_codes_regenerate_accepts_rc_step_up(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    stepped = await api.post(
        STEP_UP, json={"recovery_code": user.recovery_codes[1]}, headers=user.headers()
    )
    fresh = await api.post(RECOVERY_CODES, headers=bearer(stepped.json()["access_token"]))
    assert fresh.status_code == 200, fresh.text
    assert not set(fresh.json()["recovery_codes"]) & set(user.recovery_codes)


async def test_lost_phone_recovery_code_restores_account(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    from tests.support.rate_limits import reset_rate_limits

    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    reset_rate_limits(auth_db)
    first = (await login(api, EMAIL)).json()
    signed = await api.post(
        LOGIN_MFA,
        json={"challenge_token": first["challenge_token"], "recovery_code": user.recovery_codes[0]},
    )
    assert signed.status_code == 200
    stepped = await api.post(
        STEP_UP,
        json={"recovery_code": user.recovery_codes[1]},
        headers=bearer(signed.json()["access_token"]),
    )
    assert stepped.status_code == 200
    headers = bearer(stepped.json()["access_token"])
    new_secret = secret_of((await api.post(TOTP_ENROLL, json={}, headers=headers)).json())
    confirmed = await api.post(
        TOTP_CONFIRM, json={"code": clock.next_code(new_secret)}, headers=headers
    )
    assert confirmed.status_code == 200
    regenerated = await api.post(RECOVERY_CODES, headers=headers)
    assert regenerated.status_code == 200
    reset_rate_limits(auth_db)
    again = (await login(api, EMAIL)).json()
    old_code = await api.post(
        LOGIN_MFA,
        json={"challenge_token": again["challenge_token"], "code": clock.next_code(user.secret)},
    )
    assert old_code.status_code == 401
    reset_rate_limits(auth_db)
    third = (await login(api, EMAIL)).json()
    new_code = await api.post(
        LOGIN_MFA,
        json={"challenge_token": third["challenge_token"], "code": clock.next_code(new_secret)},
    )
    assert new_code.status_code == 200, new_code.text


# ---- the 7-day re-authentication ----


async def test_pre_mfa_family_refused_at_refresh_past_grace(
    api: httpx.AsyncClient, world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (await login(api, EMAIL)).json()
    mfa_on(monkeypatch, grace=datetime.now(UTC) - timedelta(seconds=1))
    refused = await api.post(REFRESH, json={"refresh_token": before["refresh_token"]})
    assert refused.status_code == 401
    assert count(auth_db, "SELECT count(*) FROM audit_log WHERE action = "
                 "'auth.refresh_reauth_required'") == 1  # fmt: skip


async def test_family_older_than_7_days_refused(
    api: httpx.AsyncClient,
    world: World,
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    user = await enrol(api, auth_db, EMAIL, clock)
    auth_db.execute("UPDATE refresh_tokens SET mfa_at = now() - interval '7 days'")
    refused = await api.post(REFRESH, json={"refresh_token": user.refresh})
    assert refused.status_code == 401


# ---- bearer auth cases of the two settings routes ----


@pytest.mark.endpoint("auth_mfa_totp_enroll", case="missing_auth")
@pytest.mark.endpoint("auth_mfa_totp_confirm", case="missing_auth")
async def test_totp_routes_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    for path, body in ((TOTP_ENROLL, {}), (TOTP_CONFIRM, {"code": "123456"})):
        assert (await api.post(path, json=body)).status_code == 401


@pytest.mark.endpoint("auth_mfa_totp_enroll", case="invalid_token", variant="expired")
@pytest.mark.endpoint("auth_mfa_totp_confirm", case="invalid_token", variant="expired")
async def test_totp_routes_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    headers = bearer(expired_token(world, EMAIL, "acme"))
    for path, body in ((TOTP_ENROLL, {}), (TOTP_CONFIRM, {"code": "123456"})):
        assert (await api.post(path, json=body, headers=headers)).status_code == 401


@pytest.mark.endpoint("auth_mfa_totp_enroll", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("auth_mfa_totp_confirm", case="invalid_token", variant="bad_signature")
async def test_totp_routes_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    headers = bearer(forged_token(world, EMAIL, "acme"))
    for path, body in ((TOTP_ENROLL, {}), (TOTP_CONFIRM, {"code": "123456"})):
        assert (await api.post(path, json=body, headers=headers)).status_code == 401


@pytest.mark.endpoint("auth_mfa_totp_enroll", case="invalid_token", variant="wrong_typ")
@pytest.mark.endpoint("auth_mfa_totp_confirm", case="invalid_token", variant="wrong_typ")
async def test_totp_routes_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    headers = bearer(refresh_typ_token(world, EMAIL, "acme"))
    for path, body in ((TOTP_ENROLL, {}), (TOTP_CONFIRM, {"code": "123456"})):
        assert (await api.post(path, json=body, headers=headers)).status_code == 401


@pytest.mark.endpoint("auth_mfa_totp_enroll", case="wrong_tenant")
@pytest.mark.endpoint("auth_mfa_totp_confirm", case="wrong_tenant")
async def test_totp_routes_foreign_tenant_401(api: httpx.AsyncClient, world: World) -> None:
    headers = bearer(foreign_tenant_token(world, EMAIL))
    for path, body in ((TOTP_ENROLL, {}), (TOTP_CONFIRM, {"code": "123456"})):
        assert (await api.post(path, json=body, headers=headers)).status_code == 401
