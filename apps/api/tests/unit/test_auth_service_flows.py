"""DPP-1 T10/T11 without a database: the sign-in, lockout and step-up flows of
`app.services.auth` against in-memory repositories, and the routes' commit order (a refusal is
raised only after the service's session has exited, so a counted failure is never rolled back).

The database-backed proofs (row locks, RLS, the SQL counter) are the DB suites of the Docker batch;
these pin the service logic: the gate before any verify, one argon2 verify on the locked path, the
counter on every wrong password or code, the challenge purpose binding, and the 501s under `off`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.repositories.identity import AUTH_BUSY, AuthState, MembershipRow, UserRow
from app.repositories.mfa import ChallengeRow
from app.services import auth, auth_events
from app.services import mfa as mfa_service
from mkos_schemas.auth import JWTClaims

NOW = datetime.now(UTC)
PASSWORD = "Brass-Lantern-Path-0123"
WEAK = "weakpassword"


@dataclass
class Settings:
    mfa_enforcement: str = "all"
    mfa_grace_until: datetime | None = None
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14


@dataclass
class World:
    settings: Settings = field(default_factory=Settings)
    users: dict[str, UserRow] = field(default_factory=dict)
    state: AuthState | Any = field(default_factory=lambda: AuthState(0, None))
    memberships: list[MembershipRow] = field(default_factory=list)
    has_factor: bool = True
    challenges: dict[bytes, ChallengeRow] = field(default_factory=dict)
    second_factor: str | None = None
    events: list[str] = field(default_factory=list)
    failures: int = 0
    verifies: int = 0
    challenge_fails: int = 0
    consumed: list[uuid.UUID] = field(default_factory=list)
    issued: list[dict[str, Any]] = field(default_factory=list)
    cleared: int = 0
    must_change_set: int = 0
    new_challenges: list[dict[str, Any]] = field(default_factory=list)
    stepped: int = 1
    tenant: uuid.UUID | None = None
    revoked_open: list[uuid.UUID] = field(default_factory=list)
    passwords_set: list[str] = field(default_factory=list)
    regenerated: int = 0


def _user(world: World, **changes: Any) -> UserRow:
    user = UserRow(
        id=uuid.uuid4(),
        email="asha@acme.test",
        hashed_password=f"hash:{PASSWORD}",
        is_active=True,
        display_name="Asha",
        password_changed_at=NOW - timedelta(days=10),
        last_login_at=NOW - timedelta(days=1),
    )
    user = replace(user, **changes)
    world.users[user.email] = user
    world.memberships = [
        MembershipRow(uuid.uuid4(), "Acme", user.id, user.email, "Asha", "owner", None)
    ]
    return user


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()

    @asynccontextmanager
    async def session() -> AsyncIterator[Any]:
        class _Session:
            @asynccontextmanager
            async def begin_nested(self) -> AsyncIterator[None]:
                yield

        w.tenant = None  # a new session starts with no GUCs
        try:
            yield _Session()
        finally:
            w.events.append("session_exit")

    async def set_gucs(_: Any, **kw: Any) -> None:
        if kw.get("tenant_id") is not None:
            w.tenant = kw["tenant_id"]

    class Users:
        def __init__(self, _: Any) -> None:
            pass

        async def by_email(self, email: str) -> UserRow | None:
            return w.users.get(email)

        async def self_row(self, user_id: uuid.UUID) -> UserRow | None:
            return next((u for u in w.users.values() if u.id == user_id), None)

        async def begin_auth_attempt(self, _: uuid.UUID) -> Any:
            w.events.append("gate")
            return w.state

        async def note_auth_failure(self, _: uuid.UUID) -> bool:
            w.failures += 1
            return w.failures >= 10

        async def clear_auth_failures(self, _: uuid.UUID) -> None:
            w.cleared += 1

        async def set_must_change(self, _: uuid.UUID) -> None:
            w.must_change_set += 1

        async def touch_login(self, _: uuid.UUID) -> None:
            return None

        async def is_platform_admin(self, _: uuid.UUID) -> bool:
            return False

        async def password_hashes(self, user_id: uuid.UUID) -> list[str]:
            user = await self.self_row(user_id)
            return [user.hashed_password] if user and user.hashed_password else []

        async def set_password(self, _: uuid.UUID, password_hash: str, **__: Any) -> None:
            w.passwords_set.append(password_hash)

        async def touch_mfa_changed(self, _: uuid.UUID) -> None:
            return None

    class History:
        def __init__(self, _: Any) -> None:
            pass

        async def push(self, *_: Any) -> None:
            return None

        async def prune(self, *_: Any) -> None:
            return None

    class Memberships:
        def __init__(self, _: Any) -> None:
            pass

        async def list_active_for_user(self, _: uuid.UUID) -> list[MembershipRow]:
            return list(w.memberships)

        async def touch_last_used(self, *_: Any) -> None:
            return None

    class Tokens:
        def __init__(self, _: Any) -> None:
            pass

        async def insert(self, **kw: Any) -> uuid.UUID:
            w.issued.append(kw)
            return uuid.uuid4()

        async def step_up_family(self, *_: Any, **__: Any) -> int:
            return w.stepped

        async def revoke_user_everywhere(self, *_: Any, **__: Any) -> int:
            return 0

    class Factors:
        def __init__(self, _: Any) -> None:
            pass

        async def has_active(self, _: uuid.UUID) -> bool:
            return w.has_factor

    class Challenges:
        def __init__(self, _: Any) -> None:
            pass

        async def peek_user(self, digest: bytes) -> uuid.UUID | None:
            found = w.challenges.get(digest)
            return found.user_id if found else None

        async def lock(self, digest: bytes) -> ChallengeRow | None:
            w.events.append("challenge_lock")
            return w.challenges.get(digest)

        async def fail(self, _: uuid.UUID) -> int:
            w.challenge_fails += 1
            return w.challenge_fails

        async def consume(self, challenge_id: uuid.UUID) -> None:
            w.consumed.append(challenge_id)

        async def delete_open(self, user_id: uuid.UUID) -> int:
            w.events.append("delete_open")
            w.revoked_open.append(user_id)
            return 1

    async def record(_: Any, *, action: str, tenant_id: uuid.UUID | None = None, **__: Any) -> None:
        # the audit_insert policy: a tenant row only under that tenant's GUC (else 42501)
        if tenant_id is not None and tenant_id != w.tenant:
            raise RuntimeError(f"42501: audit row for {tenant_id} under app.tenant_id={w.tenant}")
        w.events.append(action)

    async def regenerate_codes(_: Any, **__: Any) -> list[str]:
        w.regenerated += 1
        return ["rc"] * 10

    async def confirm_enrollment(_: Any, **__: Any) -> list[str] | None:
        return ["rc"] * 10 if w.second_factor else None

    def fake_hash(plain: str) -> str:
        return f"hash:{plain}"

    async def verify_second_factor(_: Any, **__: Any) -> str | None:
        w.events.append("verify_code")
        return w.second_factor

    async def new_challenge(_: Any, **kw: Any) -> tuple[str, int]:
        w.new_challenges.append(kw)
        return "raw-challenge", 300

    def fake_verify(plain: str, hashed: str) -> bool:
        return hashed == f"hash:{plain}"

    async def off_loop(fn: Any, *args: Any) -> Any:
        if getattr(fn, "__name__", "") == "verify_password" or fn is fake_verify:
            w.verifies += 1
            w.events.append("verify_password")
        return fn(*args)

    monkeypatch.setattr(auth, "get_settings", lambda: w.settings)
    monkeypatch.setattr(auth, "identity_session", session)
    monkeypatch.setattr(auth, "set_gucs", set_gucs)
    monkeypatch.setattr(auth, "UserRepository", Users)
    monkeypatch.setattr(auth, "MembershipRepository", Memberships)
    monkeypatch.setattr(auth, "RefreshTokenRepository", Tokens)
    monkeypatch.setattr(auth, "MfaFactorRepository", Factors)
    monkeypatch.setattr(auth, "ChallengeRepository", Challenges)
    monkeypatch.setattr(auth, "PasswordHistoryRepository", History)
    monkeypatch.setattr(mfa_service, "regenerate_codes", regenerate_codes)
    monkeypatch.setattr(mfa_service, "confirm_enrollment", confirm_enrollment)
    monkeypatch.setattr(auth, "hash_password", fake_hash)
    monkeypatch.setattr(auth_events, "record", record)
    monkeypatch.setattr(mfa_service, "verify_second_factor", verify_second_factor)
    monkeypatch.setattr(mfa_service, "new_challenge", new_challenge)
    monkeypatch.setattr(auth, "off_loop", off_loop)
    monkeypatch.setattr("app.services.auth.verify_password", fake_verify)
    monkeypatch.setattr(auth, "dummy_hash", lambda: "hash:not-a-real-password")
    monkeypatch.setattr(auth, "encode", lambda claims: f"jwt-{claims.typ}")
    return w


async def _login(world: World, password: str = PASSWORD) -> auth.SignInResult | None:
    return await auth.login("asha@acme.test", password, ip="203.0.113.7", user_agent="ua")


# ---- the password step ----


async def test_locked_right_password_refused_after_one_verify(world: World) -> None:
    _user(world)
    world.state = AuthState(0, NOW + timedelta(minutes=5))
    assert await _login(world) is None
    assert world.verifies == 1  # timing parity: the locked path still verifies once
    assert world.failures == 0  # and counts nothing
    assert "auth.login_failed" in world.events
    assert world.events.index("gate") < world.events.index("verify_password")


async def test_unknown_email_takes_no_gate_one_verify(world: World) -> None:
    assert await _login(world) is None
    assert world.verifies == 1 and "gate" not in world.events


async def test_wrong_password_counts(world: World) -> None:
    _user(world)
    assert await _login(world, "Wrong-Password-0000") is None
    assert world.failures == 1 and world.cleared == 0


async def test_tenth_wrong_password_writes_auth_locked(world: World) -> None:
    _user(world)
    world.failures = 9
    assert await _login(world, "Wrong-Password-0000") is None
    assert "auth.locked" in world.events


async def test_busy_gate_counts_nothing(world: World) -> None:
    _user(world)
    world.state = AUTH_BUSY
    assert await _login(world) is None
    assert world.failures == 0 and world.verifies == 1


async def test_inactive_user_right_password_none_uncounted(world: World) -> None:
    _user(world, is_active=False)
    assert await _login(world) is None
    assert world.failures == 0 and not world.new_challenges


async def test_membershipless_applicant_none_uncounted(world: World) -> None:
    _user(world)
    world.memberships = []
    assert await _login(world) is None
    assert world.failures == 0 and not world.new_challenges


async def test_enrolled_user_gets_mfa_required_no_tokens(world: World) -> None:
    _user(world)
    result = await _login(world)
    assert result is not None and result.status == "mfa_required"
    assert result.tokens is None and result.challenge_token == "raw-challenge"
    assert world.new_challenges[0]["purpose"] == "login"
    assert world.new_challenges[0]["amr"] == ["pwd"]
    assert world.cleared == 0  # the counter clears only after the full sign-in
    assert not world.issued


async def test_off_signs_in_without_code_even_with_factor(world: World) -> None:
    world.settings.mfa_enforcement = "off"
    _user(world)
    result = await _login(world)
    assert result is not None and result.status == "signed_in" and result.tokens is not None
    assert world.cleared == 1
    assert world.issued[0]["amr"] == ["pwd"]


async def test_noncompliant_password_persisted_before_mfa_required(world: World) -> None:
    _user(world, hashed_password=f"hash:{WEAK}")
    result = await _login(world, WEAK)
    assert result is not None and result.status == "mfa_required"
    assert world.must_change_set == 1


async def test_noncompliant_password_not_persisted_under_off(world: World) -> None:
    world.settings.mfa_enforcement = "off"
    _user(world, hashed_password=f"hash:{WEAK}")
    result = await _login(world, WEAK)
    assert result is not None and result.status == "signed_in"
    assert world.must_change_set == 0


async def test_first_login_without_factor_must_enrol(world: World) -> None:
    world.has_factor = False
    world.settings.mfa_grace_until = NOW + timedelta(days=5)
    _user(world, last_login_at=None)
    result = await _login(world)
    assert result is not None and result.status == "mfa_enrollment_required"
    assert world.new_challenges[0]["purpose"] == "enroll"


async def test_existing_user_in_grace_signed_in_with_grace_date(world: World) -> None:
    world.has_factor = False
    grace = NOW + timedelta(days=5)
    world.settings.mfa_grace_until = grace
    _user(world)
    result = await _login(world)
    assert result is not None and result.status == "signed_in"
    assert result.mfa_grace_until == grace


# ---- the code step ----


def _challenge(world: World, user: UserRow, purpose: str = "login", **changes: Any) -> str:
    raw = f"raw-{purpose}-{uuid.uuid4().hex}"
    row = ChallengeRow(
        id=uuid.uuid4(),
        user_id=user.id,
        purpose=purpose,
        amr=["pwd"],
        mfa_at=None,
        expires_at=NOW + timedelta(minutes=5),
        attempts=0,
    )
    world.challenges[mfa_service.challenge_digest(raw)] = replace(row, **changes)
    return raw


async def _code_step(world: World, raw: str, code: str = "123456") -> auth.SignInResult | None:
    return await auth.login_mfa(
        raw, code=code, recovery_code=None, ip="203.0.113.7", user_agent="ua"
    )


async def test_right_code_signs_in_with_amr_pwd_otp(world: World) -> None:
    user = _user(world)
    world.second_factor = "otp"
    result = await _code_step(world, _challenge(world, user))
    assert result is not None and result.status == "signed_in"
    assert world.issued[0]["amr"] == ["pwd", "otp"] and world.issued[0]["mfa_at"] is not None
    assert world.cleared == 1 and len(world.consumed) == 1


async def test_wrong_code_fails_challenge_and_counts(world: World) -> None:
    user = _user(world)
    world.second_factor = None
    assert await _code_step(world, _challenge(world, user)) is None
    assert world.challenge_fails == 1 and world.failures == 1
    assert "auth.mfa_failed" in world.events and world.cleared == 0


async def test_locked_code_step_never_checks_the_code(world: World) -> None:
    user = _user(world)
    world.second_factor = "otp"
    world.state = AuthState(0, NOW + timedelta(minutes=5))
    assert await _code_step(world, _challenge(world, user)) is None
    assert "verify_code" not in world.events and world.failures == 0


async def test_gate_before_challenge_lock(world: World) -> None:
    user = _user(world)
    world.second_factor = "otp"
    await _code_step(world, _challenge(world, user))
    assert world.events.index("gate") < world.events.index("challenge_lock")


@pytest.mark.parametrize("purpose", ["enroll", "password_change"])
async def test_challenge_of_other_purpose_refused(world: World, purpose: str) -> None:
    user = _user(world)
    world.second_factor = "otp"
    assert await _code_step(world, _challenge(world, user, purpose)) is None
    assert "verify_code" not in world.events


async def test_expired_challenge_refused(world: World) -> None:
    user = _user(world)
    world.second_factor = "otp"
    raw = _challenge(world, user, expires_at=NOW - timedelta(seconds=1))
    assert await _code_step(world, raw) is None
    assert "verify_code" not in world.events


async def test_code_step_rechecks_active(world: World) -> None:
    user = _user(world, is_active=False)
    world.second_factor = "otp"
    assert await _code_step(world, _challenge(world, user)) is None
    assert world.failures == 0 and len(world.consumed) == 1


async def test_login_challenge_refused_by_enroll(world: World) -> None:
    user = _user(world)
    world.has_factor = False
    assert await auth.login_enroll(_challenge(world, user, "login"), ip=None) is None


async def test_enroll_challenge_refused_when_factor_exists(world: World) -> None:
    user = _user(world)
    world.has_factor = True
    assert await auth.login_enroll(_challenge(world, user, "enroll"), ip=None) is None


# ---- step-up ----


def _claims(user: UserRow, *, sid: uuid.UUID | None) -> JWTClaims:
    now = int(time.time())
    return JWTClaims(
        sub=user.id,
        tid=uuid.uuid4(),
        roles=["owner"],
        email=user.email,
        typ="access",
        jti="j",
        iss="http://localhost:8000",
        aud="ai-marketplace-os",
        iat=now,
        exp=now + 600,
        sid=sid,
    )


def _principal(user: UserRow) -> Any:
    from app.core.rbac import Principal

    return Principal(user.id, uuid.uuid4(), "owner", user.email, "j")


async def test_step_up_without_sid_refused_before_gate(world: World) -> None:
    user = _user(world)
    found = await auth.step_up(
        _principal(user), _claims(user, sid=None), code="123456", recovery_code=None, ip=None
    )
    assert isinstance(found, auth.Refusal) and found.kind == "no_session"
    assert "gate" not in world.events


async def test_step_up_refused_while_locked_no_verify(world: World) -> None:
    user = _user(world)
    world.state = AuthState(0, NOW + timedelta(minutes=5))
    world.second_factor = "otp"
    found = await auth.step_up(
        _principal(user),
        _claims(user, sid=uuid.uuid4()),
        code="123456",
        recovery_code=None,
        ip=None,
    )
    assert isinstance(found, auth.Refusal) and found.kind == "locked"
    assert "verify_code" not in world.events


async def test_step_up_wrong_code_counts(world: World) -> None:
    user = _user(world)
    world.second_factor = None
    found = await auth.step_up(
        _principal(user),
        _claims(user, sid=uuid.uuid4()),
        code="123456",
        recovery_code=None,
        ip=None,
    )
    assert isinstance(found, auth.Refusal) and found.kind == "wrong_code"
    assert world.failures == 1


async def test_step_up_revoked_family_401_not_counted(world: World) -> None:
    user = _user(world)
    world.second_factor = "otp"
    world.stepped = 0
    found = await auth.step_up(
        _principal(user),
        _claims(user, sid=uuid.uuid4()),
        code="123456",
        recovery_code=None,
        ip=None,
    )
    assert isinstance(found, auth.Refusal) and found.kind == "no_session"
    assert world.failures == 0


async def test_step_up_issues_fresh_token(world: World) -> None:
    user = _user(world)
    world.second_factor = "rc"
    found = await auth.step_up(
        _principal(user), _claims(user, sid=uuid.uuid4()), code=None, recovery_code="x", ip=None
    )
    assert isinstance(found, auth.StepUp) and found.access_token == "jwt-access"
    assert "auth.step_up" in world.events


# ---- the bearer routes audit under the caller's tenant (review CR1) ----
# The fake `record` refuses a tenant audit row whose tenant is not `app.tenant_id`, as the
# `audit_insert` RLS policy does (42501, the 500 that also rolled back the counter).

NEW_PASSWORD = "Fresh-Lantern-Path-4821"


async def test_change_password_wrong_current_counts_under_tenant(world: World) -> None:
    user = _user(world)
    found = await auth.change_password(
        _principal(user),
        _claims(user, sid=uuid.uuid4()),
        "Wrong-Password-0000",
        NEW_PASSWORD,
        ip=None,
    )
    assert isinstance(found, auth.Refusal) and found.kind == "wrong_password"
    assert world.failures == 1 and "auth.password_failed" in world.events


async def test_change_password_success_audits_under_tenant(world: World) -> None:
    user = _user(world)
    found = await auth.change_password(
        _principal(user), _claims(user, sid=uuid.uuid4()), PASSWORD, NEW_PASSWORD, ip=None
    )
    assert found is None and "auth.password_changed" in world.events
    assert world.passwords_set == [f"hash:{NEW_PASSWORD}"]


async def test_totp_enroll_wrong_password_counts_under_tenant(world: World) -> None:
    user = _user(world)
    world.has_factor = False
    found = await auth.totp_enroll(
        _principal(user), _claims(user, sid=uuid.uuid4()), "Wrong-Password-0000", ip=None
    )
    assert isinstance(found, auth.Refusal) and found.kind == "wrong_password"
    assert world.failures == 1


@pytest.mark.parametrize("sid", [uuid.uuid4(), None])
async def test_totp_confirm_audits_under_tenant(world: World, sid: uuid.UUID | None) -> None:
    user = _user(world)
    world.has_factor = False
    world.second_factor = None
    wrong = await auth.totp_confirm(_principal(user), _claims(user, sid=sid), "000000", ip=None)
    assert isinstance(wrong, auth.Refusal) and wrong.kind == "wrong_code"
    assert world.failures == 1
    world.second_factor = "otp"
    right = await auth.totp_confirm(_principal(user), _claims(user, sid=sid), "123456", ip=None)
    assert right == ["rc"] * 10 and "auth.mfa_enrolled" in world.events


async def test_regenerate_codes_audits_under_tenant(world: World) -> None:
    user = _user(world)
    found = await auth.regenerate_codes(_principal(user), ip=None)
    assert found == ["rc"] * 10 and "auth.mfa_codes_regenerated" in world.events


# ---- the routes raise only after the service's session exited ----


@pytest.fixture
def client(world: World, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.api.v1.routers import auth as routes
    from app.api.v1.surface import OPS
    from fastapi.testclient import TestClient
    from tests.conftest import CI_DATABASE_URL, CI_JWT_SECRET

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", CI_DATABASE_URL)
    monkeypatch.setenv("JWT_SECRET", CI_JWT_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app
    from tests.support.fake_auth import install_fake_auth

    async def no_login_limit(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(routes, "enforce_login_limit", no_login_limit)
    monkeypatch.setattr(routes, "get_settings", lambda: world.settings)
    from app.api.errors import ApiError

    original = ApiError.__init__

    def tracking_init(self: Any, *args: Any, **kwargs: Any) -> None:
        world.events.append("raise")
        original(self, *args, **kwargs)

    monkeypatch.setattr(ApiError, "__init__", tracking_init)
    assert OPS["auth_login_mfa"].public
    app = install_fake_auth(create_app())
    yield TestClient(app, raise_server_exceptions=False)
    get_settings.cache_clear()


def test_refusal_raised_after_session_exit(world: World, client: Any) -> None:
    user = _user(world)
    world.second_factor = None
    login = client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Wrong-Password-0000"}
    )
    assert login.status_code == 401
    assert world.events.index("session_exit") < world.events.index("raise")
    world.events.clear()
    raw = _challenge(world, user)
    response = client.post(
        "/api/v1/auth/login/mfa", json={"challenge_token": raw, "code": "123456"}
    )
    assert response.status_code == 401
    assert world.failures == 2
    assert world.events.index("auth.mfa_failed") < world.events.index("session_exit")
    assert world.events.index("session_exit") < world.events.index("raise")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/auth/login/mfa", {"challenge_token": "ch", "code": "123456"}),
        ("/api/v1/auth/login/mfa/enroll", {"challenge_token": "ch"}),
        ("/api/v1/auth/login/mfa/confirm", {"challenge_token": "ch", "code": "123456"}),
        (
            "/api/v1/auth/password/expired",
            {"challenge_token": "ch", "new_password": PASSWORD},
        ),
        ("/api/v1/auth/mfa/totp/enroll", {}),
        ("/api/v1/auth/mfa/totp/confirm", {"code": "123456"}),
        ("/api/v1/auth/mfa/recovery-codes", None),
        ("/api/v1/auth/mfa/step-up", {"code": "123456"}),
    ],
)
def test_off_mfa_routes_501(world: World, client: Any, path: str, body: Any) -> None:
    world.settings.mfa_enforcement = "off"
    response = client.post(path, json=body, headers={"Authorization": "Bearer x"})
    assert response.status_code == 501, response.text
    error = response.json()["error"]
    assert error["type"] == "NOT_IMPLEMENTED" and error["message"] == "MFA is not enabled"
    assert "session_exit" not in world.events  # refused before any database work


def _shape(response: Any) -> dict[str, Any]:
    error = dict(response.json()["error"])
    error.pop("correlation_id", None)
    report = dict(error.get("error_report") or {})
    report.pop("request_id", None)
    error["error_report"] = report
    return error


@pytest.mark.parametrize(
    ("path", "body", "service", "masked"),
    [
        ("/api/v1/auth/mfa/step-up", {"code": "123456"}, "step_up", "wrong_code"),
        ("/api/v1/auth/mfa/totp/confirm", {"code": "123456"}, "totp_confirm", "wrong_code"),
        (
            "/api/v1/auth/mfa/totp/enroll",
            {"current_password": PASSWORD},
            "totp_enroll",
            "wrong_password",
        ),
        (
            "/api/v1/auth/password",
            {"current_password": PASSWORD, "new_password": "Fresh-Lantern-Path-4821"},
            "change_password",
            "wrong_password",
        ),
    ],
)
def test_locked_or_busy_body_matches_wrong_secret(
    world: World,
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body: Any,
    service: str,
    masked: str,
) -> None:
    """Review CR2 (CODE-PLAN :642, :778): a locked or busy account answers the same 422 body as
    a wrong code (or a wrong current password), so a token holder cannot see the lock."""
    shapes: dict[str, dict[str, Any]] = {}
    for kind in (masked, "locked", "busy"):

        async def refused(*_: Any, _kind: str = kind, **__: Any) -> auth.Refusal:
            return auth.Refusal(_kind)  # type: ignore[arg-type]

        monkeypatch.setattr(auth, service, refused)
        response = client.post(path, json=body, headers={"Authorization": "Bearer x"})
        assert response.status_code == 422, response.text
        shapes[kind] = _shape(response)
    assert shapes["locked"] == shapes[masked]
    assert shapes["busy"] == shapes[masked]


# ---- a password change ends every open sign-in challenge (review E2E-1) ----


async def test_change_password_deletes_open_challenges(world: World) -> None:
    user = _user(world)
    found = await auth.change_password(
        _principal(user), _claims(user, sid=uuid.uuid4()), PASSWORD, NEW_PASSWORD, ip=None
    )
    assert found is None and world.revoked_open == [user.id]


async def test_refused_change_password_keeps_challenges(world: World) -> None:
    user = _user(world)
    await auth.change_password(
        _principal(user), _claims(user, sid=uuid.uuid4()), PASSWORD, WEAK, ip=None
    )
    assert world.revoked_open == []


async def test_expired_password_deletes_other_open_challenges(world: World) -> None:
    user = _user(world)
    raw = _challenge(world, user, "password_change")
    result = await auth.expired_password(raw, NEW_PASSWORD, ip=None, user_agent=None)
    assert isinstance(result, auth.SignInResult)
    assert world.revoked_open == [user.id]
    assert world.events.index("delete_open") < world.events.index("auth.password_changed")


# ---- recovery codes need the gate and an active factor (review SEC-3) ----


async def test_regenerate_codes_refused_while_locked(world: World) -> None:
    user = _user(world)
    world.state = AuthState(0, NOW + timedelta(minutes=5))
    found = await auth.regenerate_codes(_principal(user), ip=None)
    assert isinstance(found, auth.Refusal) and found.kind == "locked"
    assert world.regenerated == 0 and "gate" in world.events


async def test_regenerate_codes_refused_without_active_factor(world: World) -> None:
    user = _user(world)
    world.has_factor = False  # e.g. staff reset-mfa after the step-up
    found = await auth.regenerate_codes(_principal(user), ip=None)
    assert isinstance(found, auth.Refusal) and found.kind == "enrollment_required"
    assert world.regenerated == 0


async def test_recovery_code_refused_without_active_factor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import recovery_codes

    consumed: list[bytes] = []

    class Factors:
        def __init__(self, _: Any) -> None:
            pass

        async def has_active(self, _: uuid.UUID) -> bool:
            return False

    class Codes:
        def __init__(self, _: Any) -> None:
            pass

        async def consume(self, _: uuid.UUID, digest: bytes) -> bool:
            consumed.append(digest)
            return True

    monkeypatch.setattr(mfa_service, "MfaFactorRepository", Factors)
    monkeypatch.setattr(mfa_service, "RecoveryCodeRepository", Codes)
    code = recovery_codes.new_codes()[0]
    found = await mfa_service.verify_second_factor(
        object(), user_id=uuid.uuid4(), recovery_code=code
    )
    assert found is None and consumed == []


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("locked", 403, "MFA_STEP_UP_REQUIRED"),
        ("busy", 403, "MFA_STEP_UP_REQUIRED"),
        ("enrollment_required", 403, "MFA_ENROLLMENT_REQUIRED"),
    ],
)
def test_recovery_codes_refusals_use_declared_statuses(
    world: World,
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    status: int,
    code: str,
) -> None:
    calls: list[int] = []

    async def refused(*_: Any, **__: Any) -> auth.Refusal:
        calls.append(1)
        return auth.Refusal(kind)  # type: ignore[arg-type]

    monkeypatch.setattr(auth, "regenerate_codes", refused)
    response = client.post("/api/v1/auth/mfa/recovery-codes", headers={"Authorization": "Bearer x"})
    assert calls == [1], response.text  # the route ran (not the step-up guard)
    assert response.status_code == status and response.json()["error"]["type"] == code


async def test_refused_new_password_uses_a_challenge_attempt(world: World) -> None:
    """Review SEC-5: each refused new password costs up to 11 argon2 operations, so it uses one
    of the challenge's 5 attempts (the account counter is untouched)."""
    user = _user(world)
    raw = _challenge(world, user, "password_change")
    found = await auth.expired_password(raw, WEAK, ip=None, user_agent=None)
    assert isinstance(found, auth.Refusal) and found.kind == "policy"
    assert world.challenge_fails == 1 and world.failures == 0
