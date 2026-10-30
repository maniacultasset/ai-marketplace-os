"""DPP-1 AC11, AC22 (ADR-0021): MFA secrets never leak, and every security event logs one line.

One flow in process under `MFA_ENFORCEMENT=all`: the forced enrolment (both public challenge
steps), a code sign-in, a recovery-code sign-in, a step-up, the bearer enrolment pair, a
recovery-code regeneration, a trusted browser (ADR-0023: trusted, used, then replayed), a password
change and a forced password change. Then no log line, Sentry event, audit row or error body
holds the TOTP secret, the `otpauth://` URI, a recovery code, a TOTP code, a challenge token, a
trusted-browser token or a password; and each new audit action wrote a log line whose message is
the action, with an IP hash and no email.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
from tests.integration.auth_support import PASSWORD, Conn, bearer, build_world
from tests.integration.mfa_support import (
    LOGIN_CONFIRM,
    LOGIN_ENROLL,
    LOGIN_MFA,
    NEW_PASSWORD,
    PASSWORD_CHANGE,
    PASSWORD_EXPIRED,
    RECOVERY_CODES,
    STEP_UP,
    TOTP_CONFIRM,
    TOTP_ENROLL,
    TotpClock,
    mfa_on,
)
from tests.support.rate_limits import reset_rate_limits

EMAIL = "owner@acme.test"
DSN = "https://public@o0.ingest.example.invalid/0"
NEW_ACTIONS = (
    "auth.mfa_enrolled",
    "auth.mfa_failed",
    "auth.mfa_recovery_used",
    "auth.mfa_codes_regenerated",
    "auth.step_up",
    "auth.password_changed",
    "auth.device_trusted",  # ADR-0023
    "auth.device_revoked",
)


def _secret(enrolment: dict[str, Any]) -> bytes:
    raw = enrolment["secret_b32"]
    return base64.b32decode(raw + "=" * (-len(raw) % 8))


@pytest.fixture
async def flow(
    auth_db: Conn,
    monkeypatch: pytest.MonkeyPatch,
    log_lines: Any,
    sentry_events: Any,
) -> dict[str, Any]:
    import app.services.mfa as mfa_service
    from app.core.config import get_settings
    from app.main import create_app

    build_world(auth_db)
    reset_rate_limits(auth_db)
    monkeypatch.setenv("SENTRY_DSN", DSN)
    mfa_on(monkeypatch)
    clock = TotpClock()
    monkeypatch.setattr(mfa_service, "time", clock)
    get_settings.cache_clear()
    app = create_app()
    capture = log_lines()
    secrets_seen: list[str] = [PASSWORD, NEW_PASSWORD]
    bodies: list[str] = []
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as api:

        async def post(path: str, body: Any = None, headers: Any = None) -> httpx.Response:
            reset_rate_limits(auth_db)
            response = await api.post(path, json=body, headers=headers or {})
            if response.status_code >= 400:
                bodies.append(response.text)
            return response

        first = (await post("/api/v1/auth/login", {"email": EMAIL, "password": PASSWORD})).json()
        challenge = first["challenge_token"]
        secrets_seen.append(challenge)
        enrolment = (await post(LOGIN_ENROLL, {"challenge_token": challenge})).json()
        secrets_seen += [enrolment["secret_b32"], enrolment["otpauth_uri"]]
        secret = _secret(enrolment)
        await post(LOGIN_CONFIRM, {"challenge_token": challenge, "code": "000000"})  # mfa_failed
        code = clock.next_code(secret)
        secrets_seen.append(code)
        confirmed = (await post(LOGIN_CONFIRM, {"challenge_token": challenge, "code": code})).json()
        codes = confirmed["recovery_codes"]
        secrets_seen += codes
        session = bearer(confirmed["result"]["access_token"])

        second = (await post("/api/v1/auth/login", {"email": EMAIL, "password": PASSWORD})).json()
        secrets_seen.append(second["challenge_token"])
        await post(
            LOGIN_MFA, {"challenge_token": second["challenge_token"], "recovery_code": codes[0]}
        )
        step_code = clock.next_code(secret)
        secrets_seen.append(step_code)
        stepped = (await post(STEP_UP, {"code": step_code}, session)).json()
        fresh = bearer(stepped["access_token"])
        replacement = (await post(TOTP_ENROLL, {}, fresh)).json()
        secrets_seen += [replacement["secret_b32"], replacement["otpauth_uri"]]
        new_code = clock.next_code(_secret(replacement))
        secrets_seen.append(new_code)
        replaced = (await post(TOTP_CONFIRM, {"code": new_code}, fresh)).json()
        secrets_seen += replaced["recovery_codes"]
        regenerated = (await post(RECOVERY_CODES, None, fresh)).json()
        secrets_seen += regenerated["recovery_codes"]
        # ADR-0023: trust this browser, use it once (rotation), then replay the old token
        login = {"email": EMAIL, "password": PASSWORD}
        to_trust = (await post("/api/v1/auth/login", login)).json()
        secrets_seen.append(to_trust["challenge_token"])
        trust_code = clock.next_code(_secret(replacement))
        secrets_seen.append(trust_code)
        trusted = (
            await post(
                LOGIN_MFA,
                {
                    "challenge_token": to_trust["challenge_token"],
                    "code": trust_code,
                    "trust_device": True,
                },
            )
        ).json()
        first_token = trusted["device_token"]
        secrets_seen.append(first_token)
        used = (await post("/api/v1/auth/login", {**login, "device_token": first_token})).json()
        assert used["status"] == "signed_in", used
        secrets_seen.append(used["device_token"])
        replayed = (await post("/api/v1/auth/login", {**login, "device_token": first_token})).json()
        assert replayed["device_token_invalid"] is True, replayed
        secrets_seen.append(replayed["challenge_token"])
        auth_db.execute(
            "UPDATE users SET password_changed_at = now() - interval '2 days' WHERE email = %s",
            [EMAIL],
        )
        await post(
            PASSWORD_CHANGE, {"current_password": PASSWORD, "new_password": NEW_PASSWORD}, fresh
        )
        auth_db.execute("UPDATE users SET password_must_change = true WHERE email = %s", [EMAIL])
        third = (
            await post("/api/v1/auth/login", {"email": EMAIL, "password": NEW_PASSWORD})
        ).json()
        secrets_seen.append(third["challenge_token"])
        last_code = clock.next_code(_secret(replacement))
        secrets_seen.append(last_code)
        fourth = (
            await post(LOGIN_MFA, {"challenge_token": third["challenge_token"], "code": last_code})
        ).json()
        secrets_seen.append(fourth["challenge_token"])
        final_password = "Quiet-Harbour-Lamp-7305"
        secrets_seen.append(final_password)
        done = await post(
            PASSWORD_EXPIRED,
            {"challenge_token": fourth["challenge_token"], "new_password": final_password},
        )
        assert done.status_code == 200, done.text
    import sentry_sdk

    sentry_sdk.get_client().flush(timeout=2)
    audit = "\n".join(
        str(r[0])
        for r in auth_db.execute(
            "SELECT concat_ws('|', action, before::text, after::text, reason) FROM audit_log"
        ).fetchall()
    )
    return {
        "secrets": [s for s in secrets_seen if s],
        "logs": capture.text(),
        "lines": capture.lines(),
        "sentry": sentry_events.serialized(),
        "audit": audit,
        "bodies": "\n".join(bodies),
    }


@pytest.mark.parametrize("sink", ["logs", "sentry", "audit", "bodies"])
def test_mfa_secrets_never_logged(flow: dict[str, Any], sink: str) -> None:
    text = flow[sink]
    for value in flow["secrets"]:
        assert value not in text, (sink, value[:6])


def test_every_new_action_logs_one_line_with_ip_hash_no_email(flow: dict[str, Any]) -> None:
    lines = [line for line in flow["lines"] if str(line.get("message", "")).startswith("auth.")]
    seen = {line["message"] for line in lines}
    assert set(NEW_ACTIONS) <= seen, sorted(seen)
    for line in lines:
        if line["message"] in NEW_ACTIONS:
            assert line.get("ip_hash"), line
            assert "@" not in repr(line), line
            assert "203.0.113.7" not in repr(line), line


def test_audit_rows_exist_for_new_actions(flow: dict[str, Any]) -> None:
    for action in NEW_ACTIONS:
        assert action in flow["audit"], action
