"""ADR-0020: Sign in and sign up with Google through the real app as app_rw under FORCE RLS,
against the fake Google (no network): start, the callback, complete and its four outcomes, the
prefilled sign-up, the link step, unlink, the flag, the binding (login CSRF), single use, expiry,
the Kuinn code after every Google sign-in, the lockout, the amr and the audit rows."""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
import pytest

from .auth_support import PASSWORD, World, audit_rows, bearer, tokens_for
from .conftest import LOCAL_HOSTS
from .google_support import (
    CFG,
    COMPLETE,
    DASHBOARD,
    FAKE_BASE,
    GENERIC,
    LINK,
    SIGNUP_GOOGLE,
    START,
    UNLINK,
    GoogleBrowser,
    GoogleWorld,
    approve,
    flows,
    google_sub_of,
    query_of,
)
from .mfa_support import (
    LOGIN_CONFIRM,
    LOGIN_ENROLL,
    LOGIN_MFA,
    SECURITY,
    STEP_UP,
    TotpClock,
    clock,  # noqa: F401 - the fixture
    enrol,
    expired_token,
    foreign_tenant_token,
    forged_token,
    lock,
    lock_state,
    mfa_on,
    refresh_typ_token,
    secret_of,
    user_id,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

GMAIL = "asha.rao@gmail.com"
OWNER = "owner@acme.test"
FORM = {"brand_name": "Asha Botanicals", "owner_name": "Asha Rao", "phone": "+91 98765 43210"}


def _payload(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


async def _new_applicant(browser: GoogleBrowser, email: str = GMAIL) -> dict[str, Any]:
    """A new Google user through the prefilled sign-up: the 202 body."""
    done = (await browser.sign_in(**{"as": email})).json()
    assert done["outcome"] == "needs_details", done
    response = await browser.api.post(
        SIGNUP_GOOGLE,
        json={
            "details_handle": done["details"]["details_handle"],
            "binding": browser.secret,
            **FORM,
        },
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def _enrol_google(browser: GoogleBrowser, totp: TotpClock, **control: str) -> dict[str, Any]:
    """A Google sign-in under `all` with no factor: enrol, confirm; the signed-in result."""
    done = (await browser.sign_in(**control)).json()
    assert done["outcome"] == "sign_in", done
    login = done["login"]
    assert login["status"] == "mfa_enrollment_required", login
    challenge = login["challenge_token"]
    enrolment = await browser.api.post(LOGIN_ENROLL, json={"challenge_token": challenge})
    assert enrolment.status_code == 200, enrolment.text
    secret = secret_of(enrolment.json())
    confirm = await browser.api.post(
        LOGIN_CONFIRM, json={"challenge_token": challenge, "code": totp.next_code(secret)}
    )
    assert confirm.status_code == 200, confirm.text
    result: dict[str, Any] = confirm.json()["result"]
    result["_secret"] = secret
    return result


# ---- start ----


@pytest.mark.endpoint("auth_google_start", case="success")
async def test_start_redirects_to_google_with_pkce(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    browser = GoogleBrowser(gapi, gw)
    response = await browser.start()
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    location = response.headers["location"]
    assert location.startswith(f"{FAKE_BASE}/google/authorize?")
    query = query_of(location)
    assert query["client_id"] == CFG.client_id
    assert query["redirect_uri"] == f"{DASHBOARD}/api/v1/auth/google/callback"
    assert query["scope"] == "openid email profile"
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert query["prompt"] == "select_account" and query["state"] and query["nonce"]
    # only hashes are stored: never the state, the nonce or the browser's binding secret
    row = gw.conn.execute(
        "SELECT phase, flow_hash, binding_hash, nonce_hash, expires_at - now() " "FROM login_flows"
    ).fetchone()
    assert row is not None
    assert row[0] == "authorize"
    assert bytes(row[1]) == hashlib.sha256(query["state"].encode()).digest()
    assert base64.urlsafe_b64encode(bytes(row[2])).rstrip(b"=").decode() == browser.b
    assert bytes(row[3]) == hashlib.sha256(query["nonce"].encode()).digest()
    assert 590 <= row[4].total_seconds() <= 600


@pytest.mark.endpoint("auth_google_start", case="validation")
async def test_start_bad_binding_422(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    for b in ("short", "+" * 43, "a" * 44):
        response = await gapi.get(START, params={"b": b})
        assert response.status_code == 422, b
    assert flows(gw.conn) == 0


async def test_start_flag_off_redirects_to_login(
    gapi: httpx.AsyncClient, gw: GoogleWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "false")
    get_settings.cache_clear()
    response = await GoogleBrowser(gapi, gw).start()
    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=google_unavailable"
    assert flows(gw.conn) == 0


async def test_start_rate_limited_per_ip(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    browser = GoogleBrowser(gapi, gw)
    statuses = [(await browser.start()).status_code for _ in range(11)]
    assert statuses[:10] == [302] * 10
    assert statuses[10] == 429


# ---- callback ----


@pytest.mark.endpoint("auth_google_callback", case="success")
async def test_callback_issues_a_bound_handle(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    browser = GoogleBrowser(gapi, gw)
    started = await browser.start()
    redirect = await browser.consent(started.headers["location"])
    back = await browser.callback(redirect)
    assert back.status_code == 303
    location = back.headers["location"]
    assert urlsplit(location).path == "/auth/google"
    assert set(query_of(location)) == {"handle"}
    assert "code=" not in location and "state=" not in location and "@" not in location
    assert flows(gw.conn, "authorize") == 0 and flows(gw.conn, "handle") == 1
    expiry = gw.conn.execute("SELECT expires_at - now() FROM login_flows").fetchone()
    assert expiry is not None and 50 <= expiry[0].total_seconds() <= 60
    # the state is single use: the same callback again fails
    again = await browser.callback(redirect)
    assert query_of(again.headers["location"]) == {"error": "failed"}
    assert [r[-1] for r in audit_rows(gw.conn, "auth.login_failed")] == ["state_invalid"]


@pytest.mark.endpoint("auth_google_callback", case="validation")
async def test_callback_oversized_param_422(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    response = await gapi.get("/api/v1/auth/google/callback", params={"state": "s" * 2049})
    assert response.status_code == 422


async def test_callback_forged_state_fails(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    response = await gapi.get(
        "/api/v1/auth/google/callback", params={"code": "g-code-x", "state": "forged"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"{DASHBOARD}/auth/google?error=failed"


async def test_callback_cancel_at_google(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    browser = GoogleBrowser(gapi, gw)
    started = await browser.start()
    back = await browser.callback(await browser.consent(started.headers["location"], deny="1"))
    assert query_of(back.headers["location"]) == {"error": "cancelled"}
    assert flows(gw.conn) == 0
    assert audit_rows(gw.conn, "auth.login_failed") == []


@pytest.mark.parametrize(
    ("control", "reason"),
    [
        ({"as": "someone@yahoo.com"}, "google_not_authoritative"),
        ({"as": "founder@acmenaturals.in"}, "google_not_authoritative"),
        ({"as": "founder@acmenaturals.in", "hd": "other.in"}, "google_not_authoritative"),
        ({"unverified": "1"}, "google_token_invalid"),
    ],
)
async def test_callback_refuses_identities_google_does_not_own(
    gapi: httpx.AsyncClient, gw: GoogleWorld, control: dict[str, str], reason: str
) -> None:
    browser = GoogleBrowser(gapi, gw)
    started = await browser.start()
    back = await browser.callback(await browser.consent(started.headers["location"], **control))
    assert query_of(back.headers["location"]) == {"error": "failed"}
    assert flows(gw.conn) == 0
    assert [r[-1] for r in audit_rows(gw.conn, "auth.login_failed")] == [reason]


async def test_callback_flag_off(
    gapi: httpx.AsyncClient, gw: GoogleWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "false")
    get_settings.cache_clear()
    response = await gapi.get("/api/v1/auth/google/callback", params={"code": "c", "state": "s"})
    assert response.headers["location"] == f"{DASHBOARD}/auth/google?error=unavailable"


# ---- complete: the binding, single use, expiry ----


@pytest.mark.endpoint("auth_google_complete", case="success")
async def test_complete_new_user_needs_details_with_prefill(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    browser = GoogleBrowser(gapi, gw)
    response = await browser.sign_in()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "needs_details"
    assert body["login"] is None and body["signup"] is None and body["link"] is None
    details = body["details"]
    assert details["email"] == GMAIL and details["email_verified"] is True
    assert (details["owner_name"], details["given_name"], details["family_name"]) == (
        "Asha Rao",
        "Asha",
        "Rao",
    )
    assert details["suggested_brand_name"] is None  # a Gmail address names no brand
    assert details["details_expires_in"] == 900
    assert gw.conn.execute("SELECT count(*) FROM users WHERE email = %s", [GMAIL]).fetchone() == (
        0,
    )


async def test_complete_workspace_user_gets_brand_suggestion(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    body = (
        await GoogleBrowser(gapi, gw).sign_in(
            **{"as": "meera@acme-naturals.co.in", "hd": "acme-naturals.co.in", "name": "Meera Iyer"}
        )
    ).json()
    assert body["details"]["suggested_brand_name"] == "Acme Naturals"
    assert body["details"]["owner_name"] == "Meera Iyer"


@pytest.mark.endpoint("auth_google_complete", case="validation")
async def test_complete_bad_body_422(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    for body in ({}, {"handle": "h"}, {"handle": "h", "binding": "short"}):
        assert (await gapi.post(COMPLETE, json=body)).status_code == 422


async def test_complete_wrong_binding_is_login_csrf_refused(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    """OWASP login CSRF: an attacker's handle completed in a victim's browser (another binding)
    fails, and leaves the handle for its own browser."""
    attacker = GoogleBrowser(gapi, gw)
    handle = await attacker.handle()
    victim = GoogleBrowser(gapi, gw)
    refused = await victim.complete(handle)
    assert refused.status_code == 401
    assert refused.json()["error"]["type"] == "OAUTH_STATE_INVALID"
    assert flows(gw.conn, "handle") == 1
    assert (await attacker.complete(handle)).status_code == 200


async def test_complete_handle_single_use(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    browser = GoogleBrowser(gapi, gw)
    handle = await browser.handle()
    assert (await browser.complete(handle)).status_code == 200
    replay = await browser.complete(handle)
    assert replay.status_code == 401
    assert replay.json()["error"]["type"] == "OAUTH_STATE_INVALID"


async def test_complete_expired_handle(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    browser = GoogleBrowser(gapi, gw)
    handle = await browser.handle()
    gw.conn.execute("UPDATE login_flows SET expires_at = now() - interval '1 second'")
    response = await browser.complete(handle)
    assert response.json()["error"]["type"] == "OAUTH_STATE_INVALID"


async def test_complete_flag_off_501(
    gapi: httpx.AsyncClient, gw: GoogleWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    browser = GoogleBrowser(gapi, gw)
    handle = await browser.handle()
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "false")
    get_settings.cache_clear()
    response = await browser.complete(handle)
    assert response.status_code == 501
    assert response.json()["error"]["type"] == "NOT_IMPLEMENTED"


# ---- sign-up with Google, then approval, then the Kuinn code ----


@pytest.mark.endpoint("signup_google", case="success")
async def test_signup_google_creates_a_verified_pending_applicant(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    browser = GoogleBrowser(gapi, gw)
    body = await _new_applicant(browser)
    assert body["status"] == "pending" and body["brand_name"] == "Asha Botanicals"
    row = gw.conn.execute(
        "SELECT u.hashed_password, u.auth_provider, u.email_verified_at IS NOT NULL, "
        "u.google_sub IS NOT NULL, u.display_name, r.verified_via, r.owner_name, r.phone "
        "FROM users u JOIN signup_requests r ON r.user_id = u.id WHERE u.email = %s",
        [GMAIL],
    ).fetchone()
    assert row == (None, "google", True, True, "Asha Rao", "google", "Asha Rao", "+91 98765 43210")
    applicant = user_id(gw.conn, GMAIL)
    rows = audit_rows(gw.conn, "signup.requested")
    assert [(*r[:8], r[9]) for r in rows] == [
        (None, "system", None, None, "user", applicant, None, None, "google")
    ]
    # Google sign-in again: the request's status, no second request
    again = (await browser.sign_in()).json()
    assert again["outcome"] == "pending"
    assert again["signup"]["status"] == "pending"
    assert again["signup"]["request_id"] == body["request_id"]


@pytest.mark.endpoint("signup_google", case="validation")
async def test_signup_google_bad_body_422(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    good = {"details_handle": "d", "binding": "b" * 43, **FORM}
    for change in ({"brand_name": ""}, {"owner_name": None}, {"phone": "abc"}, {"email": GMAIL}):
        response = await gapi.post(SIGNUP_GOOGLE, json={**good, **change})
        assert response.status_code == 422, change


async def test_signup_google_details_handle_single_use_and_bound(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    browser = GoogleBrowser(gapi, gw)
    done = (await browser.sign_in()).json()
    body = {"details_handle": done["details"]["details_handle"], **FORM}
    other = GoogleBrowser(gapi, gw)
    foreign = await gapi.post(SIGNUP_GOOGLE, json={**body, "binding": other.secret})
    assert foreign.json()["error"]["type"] == "OAUTH_STATE_INVALID"
    first = await gapi.post(SIGNUP_GOOGLE, json={**body, "binding": browser.secret})
    assert first.status_code == 202
    replay = await gapi.post(SIGNUP_GOOGLE, json={**body, "binding": browser.secret})
    assert replay.json()["error"]["type"] == "OAUTH_STATE_INVALID"


async def test_signup_google_email_taken_meanwhile_is_generic(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    from .auth_support import add_user

    browser = GoogleBrowser(gapi, gw)
    done = (await browser.sign_in()).json()
    add_user(gw.conn, GMAIL)  # a password sign-up of the same email landed first
    response = await gapi.post(
        SIGNUP_GOOGLE,
        json={
            "details_handle": done["details"]["details_handle"],
            "binding": browser.secret,
            **FORM,
        },
    )
    assert response.status_code == 401
    assert response.json()["error"] == response.json()["error"] | {
        "type": "UNAUTHENTICATED",
        "message": GENERIC,
    }
    assert [r[-1] for r in audit_rows(gw.conn, "auth.login_failed")] == ["google_signup_taken"]


async def test_approved_google_user_signs_in_with_mfa_off(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    browser = GoogleBrowser(gapi, gw)
    await _new_applicant(browser)
    tenant = approve(gw.conn, GMAIL, "asha-botanicals")
    body = (await browser.sign_in()).json()
    assert body["outcome"] == "sign_in"
    login = body["login"]
    assert login["status"] == "signed_in"
    claims = _payload(login["access_token"])
    assert claims["tid"] == str(tenant) and claims["roles"] == ["owner"]
    assert "amr" not in claims  # MFA off mints today's ten claims
    applicant = user_id(gw.conn, GMAIL)
    rows = audit_rows(gw.conn, "auth.login")
    assert [(*r[:8], r[9]) for r in rows] == [
        (tenant, "user", applicant, None, "user", applicant, None, None, "google")
    ]


async def test_google_sign_in_always_asks_the_kuinn_code(
    gapi: httpx.AsyncClient,
    gw: GoogleWorld,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    """Under `all`, even inside the grace period: enrol on the first Google sign-in, then the
    code every time; the token's amr holds Kuinn's factor only."""
    from datetime import UTC, datetime, timedelta

    browser = GoogleBrowser(gapi, gw)
    await _new_applicant(browser)
    approve(gw.conn, GMAIL, "asha-botanicals")
    gw.conn.execute(
        "UPDATE users SET last_login_at = now() - interval '1 day' WHERE email = %s", [GMAIL]
    )
    mfa_on(monkeypatch, grace=datetime.now(UTC) + timedelta(days=5))
    first = await _enrol_google(browser, clock)
    assert first["status"] == "signed_in"
    assert _payload(first["access_token"])["amr"] == ["otp"]
    refresh = gw.conn.execute(
        "SELECT amr FROM refresh_tokens r JOIN users u ON u.id = r.user_id WHERE u.email = %s",
        [GMAIL],
    ).fetchone()
    assert refresh == (["otp"],)
    # the next Google sign-in asks for the code
    again = (await browser.sign_in()).json()
    assert again["login"]["status"] == "mfa_required"
    assert again["login"]["access_token"] is None
    code = await gapi.post(
        LOGIN_MFA,
        json={
            "challenge_token": again["login"]["challenge_token"],
            "code": clock.next_code(first["_secret"]),
        },
    )
    assert code.status_code == 200, code.text
    assert _payload(code.json()["access_token"])["amr"] == ["otp"]
    reasons = [r[-1] for r in audit_rows(gw.conn, "auth.login")]
    assert reasons == ["google", "google"]
    # a Google-only user has no password to change or expire
    security = (await gapi.get(SECURITY, headers=bearer(code.json()["access_token"]))).json()
    assert security["has_password"] is False and security["google_linked"] is True
    assert security["password_changed_at"] is None and security["password_expires_at"] is None


async def test_google_session_step_up_never_claims_a_password(
    gapi: httpx.AsyncClient,
    gw: GoogleWorld,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    browser = GoogleBrowser(gapi, gw)
    await _new_applicant(browser)
    approve(gw.conn, GMAIL, "asha-botanicals")
    mfa_on(monkeypatch)
    signed = await _enrol_google(browser, clock)
    stepped = await gapi.post(
        STEP_UP,
        json={"code": clock.next_code(signed["_secret"])},
        headers=bearer(signed["access_token"]),
    )
    assert stepped.status_code == 200, stepped.text
    assert _payload(stepped.json()["access_token"])["amr"] == ["otp"]
    family = gw.conn.execute(
        "SELECT DISTINCT amr FROM refresh_tokens r JOIN users u ON u.id = r.user_id "
        "WHERE u.email = %s AND r.revoked_at IS NULL",
        [GMAIL],
    ).fetchall()
    assert family == [(["otp"],)]


# ---- existing accounts: link only after the password and the code (D1) ----


@pytest.mark.endpoint("auth_google_link", case="success")
async def test_link_after_password_and_code(
    gapi: httpx.AsyncClient,
    gw: GoogleWorld,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    mfa_on(monkeypatch)
    enrolled = await enrol(gapi, gw.conn, OWNER, clock)
    browser = GoogleBrowser(gapi, gw)
    done = (await browser.sign_in(**{"as": OWNER, "hd": "acme.test"})).json()
    assert done["outcome"] == "link_required"
    assert done["link"]["email"] == OWNER and done["link"]["link_expires_in"] == 900
    password = await gapi.post(
        LINK,
        json={
            "link_handle": done["link"]["link_handle"],
            "binding": browser.secret,
            "password": PASSWORD,
        },
    )
    assert password.status_code == 200, password.text
    step = password.json()
    assert step["status"] == "mfa_required"
    assert google_sub_of(gw.conn, OWNER) is None  # nothing linked before the code
    code = await gapi.post(
        LOGIN_MFA,
        json={
            "challenge_token": step["challenge_token"],
            "code": clock.next_code(enrolled.secret),
        },
    )
    assert code.status_code == 200, code.text
    assert _payload(code.json()["access_token"])["amr"] == ["pwd", "otp"]
    assert google_sub_of(gw.conn, OWNER) is not None
    owner = gw.world.user(OWNER)
    acme = gw.world.tenant("acme")
    rows = audit_rows(gw.conn, "auth.google_linked")
    assert [(*r[:8], r[9]) for r in rows] == [
        (acme, "user", owner, None, "user", owner, None, None, None)
    ]
    # from now on Google signs in directly (to the code step)
    again = (await browser.sign_in(**{"as": OWNER, "hd": "acme.test"})).json()
    assert again["outcome"] == "sign_in" and again["login"]["status"] == "mfa_required"


@pytest.mark.endpoint("auth_google_link", case="validation")
async def test_link_bad_body_422(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    good = {"link_handle": "l", "binding": "b" * 43, "password": "pw"}
    for change in ({"password": ""}, {"binding": "x"}, {"link_handle": ""}):
        assert (await gapi.post(LINK, json={**good, **change})).status_code == 422


async def test_link_wrong_password_counts_and_keeps_the_handle(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    browser = GoogleBrowser(gapi, gw)
    done = (await browser.sign_in(**{"as": OWNER, "hd": "acme.test"})).json()
    body = {"link_handle": done["link"]["link_handle"], "binding": browser.secret}
    wrong = await gapi.post(LINK, json={**body, "password": "Not-The-Password-1"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["type"] == "UNAUTHENTICATED"
    assert lock_state(gw.conn, OWNER) == (1, False)
    right = await gapi.post(LINK, json={**body, "password": PASSWORD})
    assert right.status_code == 200 and right.json()["status"] == "signed_in"  # MFA off
    assert google_sub_of(gw.conn, OWNER) is not None
    assert flows(gw.conn, "link") == 0
    other = GoogleBrowser(gapi, gw)
    foreign = await gapi.post(LINK, json={**body, "binding": other.secret, "password": PASSWORD})
    assert foreign.json()["error"]["type"] == "OAUTH_STATE_INVALID"


async def test_link_gets_no_grace(
    gapi: httpx.AsyncClient,
    gw: GoogleWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    gw.conn.execute(
        "UPDATE users SET last_login_at = now() - interval '1 day' WHERE email = %s", [OWNER]
    )
    mfa_on(monkeypatch, grace=datetime.now(UTC) + timedelta(days=5))
    browser = GoogleBrowser(gapi, gw)
    done = (await browser.sign_in(**{"as": OWNER, "hd": "acme.test"})).json()
    step = await gapi.post(
        LINK,
        json={
            "link_handle": done["link"]["link_handle"],
            "binding": browser.secret,
            "password": PASSWORD,
        },
    )
    assert step.json()["status"] == "mfa_enrollment_required"
    assert google_sub_of(gw.conn, OWNER) is None


# ---- the generic failure ----


async def _generic_failure(response: httpx.Response) -> None:
    assert response.status_code == 401, response.text
    error = response.json()["error"]
    assert (error["type"], error["message"]) == ("UNAUTHENTICATED", GENERIC)


async def test_platform_admin_never_uses_google(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    gw.conn.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [OWNER])
    await _generic_failure(
        await GoogleBrowser(gapi, gw).sign_in(**{"as": OWNER, "hd": "acme.test"})
    )
    assert [r[-1] for r in audit_rows(gw.conn, "auth.login_failed")] == ["google_admin"]


async def test_linked_admin_never_uses_google(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    from tests.support.fake_google import sub_for

    gw.conn.execute(
        "UPDATE users SET is_platform_admin = true, google_sub = %s WHERE email = %s",
        [sub_for(OWNER), OWNER],
    )
    await _generic_failure(
        await GoogleBrowser(gapi, gw).sign_in(**{"as": OWNER, "hd": "acme.test"})
    )


async def test_locked_account_refused_uncounted(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    from tests.support.fake_google import sub_for

    gw.conn.execute("UPDATE users SET google_sub = %s WHERE email = %s", [sub_for(OWNER), OWNER])
    lock(gw.conn, OWNER)
    await _generic_failure(
        await GoogleBrowser(gapi, gw).sign_in(**{"as": OWNER, "hd": "acme.test"})
    )
    assert lock_state(gw.conn, OWNER) == (0, True)
    assert [r[-1] for r in audit_rows(gw.conn, "auth.login_failed")] == ["locked"]


async def test_other_google_account_on_the_email_refused(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    gw.conn.execute("UPDATE users SET google_sub = 'another-account' WHERE email = %s", [OWNER])
    await _generic_failure(
        await GoogleBrowser(gapi, gw).sign_in(**{"as": OWNER, "hd": "acme.test"})
    )
    assert [r[-1] for r in audit_rows(gw.conn, "auth.login_failed")] == ["google_link_refused"]


async def test_inactive_user_refused(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    gw.conn.execute("UPDATE users SET is_active = false WHERE email = %s", [OWNER])
    await _generic_failure(
        await GoogleBrowser(gapi, gw).sign_in(**{"as": OWNER, "hd": "acme.test"})
    )
    assert [r[-1] for r in audit_rows(gw.conn, "auth.login_failed")] == ["inactive_user"]


async def test_password_applicant_sees_their_pending_request(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    """D4: Google proved the email, so a pending password sign-up shows its status."""
    response = await gapi.post(
        "/api/v1/signup",
        json={
            "brand_name": "Kalakar Weaves",
            "owner_name": "Meera Iyer",
            "email": "meera@gmail.com",
            "password": "Quiet-Harbour-Lamp-7305",
        },
    )
    assert response.status_code == 202
    body = (await GoogleBrowser(gapi, gw).sign_in(**{"as": "meera@gmail.com"})).json()
    assert body["outcome"] == "pending"
    assert body["signup"]["brand_name"] == "Kalakar Weaves"
    assert google_sub_of(gw.conn, "meera@gmail.com") is None


async def test_unvetted_user_without_request_refused(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    from .auth_support import add_user

    add_user(gw.conn, "loner@gmail.com")
    await _generic_failure(await GoogleBrowser(gapi, gw).sign_in(**{"as": "loner@gmail.com"}))
    assert [r[-1] for r in audit_rows(gw.conn, "auth.login_failed")] == ["google_unvetted_conflict"]


# ---- unlink ----


def _owner_token(world: World) -> str:
    return tokens_for(world, OWNER, "acme")


@pytest.mark.endpoint("auth_google_unlink", case="success")
async def test_unlink_removes_the_google_account(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    gw.conn.execute("UPDATE users SET google_sub = 'sub-owner' WHERE email = %s", [OWNER])
    response = await gapi.post(UNLINK, headers=bearer(_owner_token(gw.world)))
    assert response.status_code == 204
    assert google_sub_of(gw.conn, OWNER) is None
    assert len(audit_rows(gw.conn, "auth.google_unlinked")) == 1
    again = await gapi.post(UNLINK, headers=bearer(_owner_token(gw.world)))
    assert again.status_code == 204


async def test_unlink_refused_without_a_password(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    browser = GoogleBrowser(gapi, gw)
    await _new_applicant(browser)
    approve(gw.conn, GMAIL, "asha-botanicals")
    login = (await browser.sign_in()).json()["login"]
    response = await gapi.post(UNLINK, headers=bearer(login["access_token"]))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert google_sub_of(gw.conn, GMAIL) is not None


async def test_unlink_needs_a_step_up_under_mfa(
    gapi: httpx.AsyncClient, gw: GoogleWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw.conn.execute("UPDATE users SET google_sub = 'sub-owner' WHERE email = %s", [OWNER])
    mfa_on(monkeypatch)
    response = await gapi.post(UNLINK, headers=bearer(_owner_token(gw.world)))
    assert response.status_code == 403
    assert google_sub_of(gw.conn, OWNER) == "sub-owner"


@pytest.mark.endpoint("auth_google_unlink", case="missing_auth")
async def test_unlink_missing_auth_401(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    assert (await gapi.post(UNLINK)).status_code == 401


@pytest.mark.endpoint("auth_google_unlink", case="invalid_token", variant="expired")
async def test_unlink_expired_token_401(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    token = expired_token(gw.world, OWNER, "acme")
    assert (await gapi.post(UNLINK, headers=bearer(token))).status_code == 401


@pytest.mark.endpoint("auth_google_unlink", case="invalid_token", variant="bad_signature")
async def test_unlink_forged_token_401(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    token = forged_token(gw.world, OWNER, "acme")
    assert (await gapi.post(UNLINK, headers=bearer(token))).status_code == 401


@pytest.mark.endpoint("auth_google_unlink", case="invalid_token", variant="wrong_typ")
async def test_unlink_refresh_token_401(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    token = refresh_typ_token(gw.world, OWNER, "acme")
    assert (await gapi.post(UNLINK, headers=bearer(token))).status_code == 401


@pytest.mark.endpoint("auth_google_unlink", case="wrong_tenant")
async def test_unlink_foreign_tenant_401(gapi: httpx.AsyncClient, gw: GoogleWorld) -> None:
    gw.conn.execute("UPDATE users SET google_sub = 'sub-owner' WHERE email = %s", [OWNER])
    token = foreign_tenant_token(gw.world, OWNER)
    assert (await gapi.post(UNLINK, headers=bearer(token))).status_code == 401
    assert google_sub_of(gw.conn, OWNER) == "sub-owner"


# ---- the lockout and the logs ----


async def test_wrong_codes_after_google_lock_the_account(
    gapi: httpx.AsyncClient,
    gw: GoogleWorld,
    monkeypatch: pytest.MonkeyPatch,
    clock: TotpClock,  # noqa: F811
) -> None:
    """Ten wrong Kuinn codes after Google sign-ins lock the account like any sign-in, and a
    locked account's Google sign-in is the generic failure."""
    from tests.support.fake_google import sub_for

    mfa_on(monkeypatch)
    await enrol(gapi, gw.conn, OWNER, clock)
    gw.conn.execute("UPDATE users SET google_sub = %s WHERE email = %s", [sub_for(OWNER), OWNER])
    browser = GoogleBrowser(gapi, gw)
    for _ in range(2):  # a challenge burns after 5 attempts
        login = (await browser.sign_in(**{"as": OWNER, "hd": "acme.test"})).json()["login"]
        assert login["status"] == "mfa_required", login
        for _ in range(5):
            wrong = await gapi.post(
                LOGIN_MFA, json={"challenge_token": login["challenge_token"], "code": "000000"}
            )
            assert wrong.status_code == 401
    assert lock_state(gw.conn, OWNER) == (0, True)
    await _generic_failure(await browser.sign_in(**{"as": OWNER, "hd": "acme.test"}))


async def test_google_flow_logs_no_identity_or_secret(
    gapi: httpx.AsyncClient, gw: GoogleWorld, log_lines: Any
) -> None:
    from tests.support.fake_google import CANARY

    capture = log_lines()
    browser = GoogleBrowser(gapi, gw)
    started = await browser.start()
    redirect = await browser.consent(started.headers["location"])
    back = await browser.callback(redirect)
    handle = query_of(back.headers["location"])["handle"]
    done = (await browser.complete(handle)).json()
    secrets = (
        handle,
        query_of(redirect)["code"],
        query_of(redirect)["state"],
        query_of(started.headers["location"])["nonce"],
        browser.secret,
        browser.b,
        GMAIL,
        done["details"]["details_handle"],
        CANARY,
    )
    text = capture.text()
    assert "auth" in text or "request" in text
    for secret in secrets:
        assert secret not in text, secret
    rows = gw.conn.execute("SELECT * FROM login_flows").fetchall()
    stored = repr(rows)
    for secret in (handle, query_of(redirect)["code"], browser.secret, CANARY):
        assert secret not in stored
