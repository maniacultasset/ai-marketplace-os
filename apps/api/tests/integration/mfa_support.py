"""DPP-1 test helpers (ADR-0021): MFA switched on for one test, a TOTP clock the test drives, and
the API steps a test needs to reach a state (enrolled, stepped up, locked).

- `mfa_on(monkeypatch, grace=None)`: `MFA_ENFORCEMENT=all` (and a grace date) for the app under
  test; the settings cache is cleared so the next request reads it.
- `TotpClock`: replaces `time` in `app.services.mfa`, so each code is generated for, and checked
  at, a step the test chose; `next_code()` moves one step on (a code works once per step).
- `enrol(api, conn, email)`: the forced enrolment through the public challenge pair; returns the
  secret, the recovery codes and the signed-in result.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import pytest

from .auth_support import PASSWORD, PREFIX, Conn, bearer

LOGIN = f"{PREFIX}/auth/login"
LOGIN_MFA = f"{PREFIX}/auth/login/mfa"
LOGIN_ENROLL = f"{PREFIX}/auth/login/mfa/enroll"
LOGIN_CONFIRM = f"{PREFIX}/auth/login/mfa/confirm"
PASSWORD_EXPIRED = f"{PREFIX}/auth/password/expired"
TOTP_ENROLL = f"{PREFIX}/auth/mfa/totp/enroll"
TOTP_CONFIRM = f"{PREFIX}/auth/mfa/totp/confirm"
SECURITY = f"{PREFIX}/auth/security"
RECOVERY_CODES = f"{PREFIX}/auth/mfa/recovery-codes"
STEP_UP = f"{PREFIX}/auth/mfa/step-up"
PASSWORD_CHANGE = f"{PREFIX}/auth/password"
REFRESH = f"{PREFIX}/auth/refresh"
SWITCH = f"{PREFIX}/auth/switch-tenant"
NEW_PASSWORD = "Fresh-Lantern-Path-4821"


def mfa_on(monkeypatch: pytest.MonkeyPatch, *, grace: datetime | None = None) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("MFA_ENFORCEMENT", "all")
    if grace is not None:
        monkeypatch.setenv("MFA_GRACE_UNTIL", grace.isoformat())
    else:
        monkeypatch.delenv("MFA_GRACE_UNTIL", raising=False)
    get_settings.cache_clear()


def mfa_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("MFA_ENFORCEMENT", "off")
    get_settings.cache_clear()


@dataclass
class TotpClock:
    now: float = 1_900_000_005.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float = 30.0) -> None:
        self.now += seconds

    def code(self, secret: bytes, *, delta: int = 0) -> str:
        from app.core import totp

        return totp.hotp(secret, totp.step_at(self.now) + delta)

    def next_code(self, secret: bytes) -> str:
        self.advance()
        return self.code(secret)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> TotpClock:
    import app.services.mfa as mfa_service

    fake = TotpClock()
    monkeypatch.setattr(mfa_service, "time", fake)
    return fake


def secret_of(enrolment: dict[str, Any]) -> bytes:
    raw = enrolment["secret_b32"]
    return base64.b32decode(raw + "=" * (-len(raw) % 8))


async def login(api: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    return await api.post(LOGIN, json={"email": email, "password": password})


@dataclass
class Enrolled:
    email: str
    secret: bytes
    recovery_codes: list[str]
    result: dict[str, Any]
    used_codes: list[str] = field(default_factory=list)

    @property
    def access(self) -> str:
        return str(self.result["access_token"])

    @property
    def refresh(self) -> str:
        return str(self.result["refresh_token"])

    def headers(self) -> dict[str, str]:
        return bearer(self.access)


async def enrol(
    api: httpx.AsyncClient, conn: Conn, email: str, clock: TotpClock, password: str = PASSWORD
) -> Enrolled:
    """Forced enrolment at sign-in (the user has no factor and no grace): login, enroll,
    confirm. The rate-limit counters are reset first and after."""
    from tests.support.rate_limits import reset_rate_limits

    reset_rate_limits(conn)
    first = await login(api, email, password)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "mfa_enrollment_required", body
    challenge = body["challenge_token"]
    enrolment = await api.post(LOGIN_ENROLL, json={"challenge_token": challenge})
    assert enrolment.status_code == 200, enrolment.text
    secret = secret_of(enrolment.json())
    confirm = await api.post(
        LOGIN_CONFIRM, json={"challenge_token": challenge, "code": clock.next_code(secret)}
    )
    assert confirm.status_code == 200, confirm.text
    out = confirm.json()
    reset_rate_limits(conn)
    return Enrolled(email, secret, out["recovery_codes"], out["result"])


async def sign_in_with_code(
    api: httpx.AsyncClient, conn: Conn, user: Enrolled, clock: TotpClock
) -> dict[str, Any]:
    from tests.support.rate_limits import reset_rate_limits

    reset_rate_limits(conn)
    first = (await login(api, user.email)).json()
    assert first["status"] == "mfa_required", first
    second = await api.post(
        LOGIN_MFA,
        json={"challenge_token": first["challenge_token"], "code": clock.next_code(user.secret)},
    )
    assert second.status_code == 200, second.text
    body: dict[str, Any] = second.json()
    return body


def lock_state(conn: Conn, email: str) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT failed_auth_count, coalesce(locked_until > now(), false) FROM users "
        "WHERE email = %s",
        [email],
    ).fetchone()
    assert row is not None
    return int(row[0]), bool(row[1])


def lock(conn: Conn, email: str, *, minutes: int = 15) -> None:
    """Lock the account as the definer would (the admin login is a superuser)."""
    conn.execute(
        "UPDATE users SET failed_auth_count = 0, "
        "locked_until = now() + make_interval(mins => %s) WHERE email = %s",
        [minutes, email],
    )


def set_failures(conn: Conn, email: str, count: int) -> None:
    conn.execute("UPDATE users SET failed_auth_count = %s WHERE email = %s", [count, email])


def user_id(conn: Conn, email: str) -> uuid.UUID:
    row = conn.execute("SELECT id FROM users WHERE email = %s", [email]).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def mark_signed_in_before(conn: Conn, email: str) -> None:
    """Not a first login (a first login gets no grace)."""
    conn.execute(
        "UPDATE users SET last_login_at = now() - interval '1 day' WHERE email = %s", [email]
    )


def count(conn: Conn, sql: str, params: list[Any] | None = None) -> int:
    row = conn.execute(sql, params or []).fetchone()
    assert row is not None
    return int(row[0])


def expired_token(world: Any, email: str, slug: str) -> str:
    import time

    import jwt
    from tests.conftest import CI_JWT_SECRET

    from .auth_support import tokens_for

    claims = jwt.decode(tokens_for(world, email, slug), options={"verify_signature": False})
    now = int(time.time())
    claims.update(iat=now - 3600, exp=now - 1)
    return jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")


def forged_token(world: Any, email: str, slug: str) -> str:
    import jwt

    from .auth_support import tokens_for

    claims = jwt.decode(tokens_for(world, email, slug), options={"verify_signature": False})
    return jwt.encode(claims, "another_secret_that_is_at_least_32_chars", algorithm="HS256")


def refresh_typ_token(world: Any, email: str, slug: str) -> str:
    from .auth_support import tokens_for

    return tokens_for(world, email, slug, typ="refresh")


def foreign_tenant_token(world: Any, email: str) -> str:
    """A well-signed token naming a tenant the user is not a member of."""
    from .auth_support import tokens_for

    other = "bharat" if email.endswith("@acme.test") else "acme"
    return tokens_for(world, email, other, role="owner")
