"""Authentication (BACKEND-ARCHITECTURE.md §9.2; DATABASE.md:306-312; CODE-PLAN S10 Q10), with the
DPP-1 MFA, lockout and password policy (ADR-0021).

`probe_principal` turns verified access claims into the `Principal`: one membership probe inside
the token's tenant, and the role from the row (a demoted user's old token carries the old role).

DPP-1 rules every sign-in path here follows:

- **One shared `_sign_in()`** decides what the sign-in needs next (`sign_in_policy.next_step`):
  tokens, or a challenge for a code, an enrolment or a password change.
- **The per-user gate.** Every path that verifies a password or a code resolves the user, sets
  `app.user_id`, then calls `UserRepository.begin_auth_attempt` before any verify. The row lock
  serialises that user's attempts until commit, so the lock check, the verify and the counter
  write never interleave across parallel requests. A lock wait past 5 s is `AUTH_BUSY`: nothing
  is verified or counted, and the caller answers its usual refusal.
- **Commit before refusing.** A service returns a result value (None, a `Refusal`, or the
  success payload) and never raises a refusal inside its `identity_session()` block, which rolls
  back on any exception and would undo the counter and the challenge's attempt. The route raises
  the 401/422 after the block has committed.
- **Lockout.** A locked account refuses on every path whatever the input; the public password
  routes still run their one argon2 verify for timing parity and ignore its result. The counter
  clears only after a full sign-in (or a staff unlock).
"""

from __future__ import annotations

import ipaddress
import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cache
from typing import Any, Final, Literal
from uuid import UUID

from mkos_schemas.auth import AuthMethod, JWTClaims

from app.core import password_policy
from app.core.config import get_settings
from app.core.passwords import hash_password, off_loop, verify_password
from app.core.rbac import Principal
from app.core.security import InvalidToken, decode, encode, issue_claims, token_hash
from app.repositories.identity import (
    AUTH_BUSY,
    AuditRepository,
    AuthBusy,
    AuthState,
    MembershipRepository,
    MembershipRow,
    RefreshTokenRepository,
    UserRepository,
    UserRow,
)
from app.repositories.mfa import (
    ChallengeRepository,
    ChallengeRow,
    MfaFactorRepository,
    PasswordHistoryRepository,
    RecoveryCodeRepository,
)
from app.repositories.transactions import identity_session, set_gucs, unit_of_work
from app.services import auth_events, mfa
from app.services.sign_in_policy import (
    SignInFacts,
    Step,
    in_grace,
    is_locked,
    must_persist_noncompliance,
    next_step,
    refresh_allowed,
    step_up_fresh,
)


async def probe_principal(claims: JWTClaims) -> Principal | None:
    """The caller, or None when the membership is inactive, the user inactive or the tenant
    neither `trial` nor `active`."""
    async with unit_of_work(claims.tid, user_id=claims.sub) as session:
        row = await MembershipRepository(session).probe(claims.tid, claims.sub)
    if row is None:
        return None
    return Principal(
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        role=row.role,  # type: ignore[arg-type]
        email=row.email,
        jti=claims.jti,
    )


# ---- shared pieces ----

USER_AGENT_MAX: Final = 512
METHOD_ORDER: Final = ("pwd", "otp", "rc")
PURPOSE_FOR: Final[dict[str, mfa.Purpose]] = {
    "mfa_required": "login",
    "mfa_enrollment_required": "enroll",
    "password_change_required": "password_change",
}


@dataclass(frozen=True, slots=True)
class Membership:
    tenant_id: UUID
    tenant_name: str
    role: str


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    memberships: list[Membership]


@dataclass(frozen=True, slots=True)
class SignInResult:
    """What a sign-in step answers: tokens (`signed_in`) or the next challenge."""

    status: Step
    tokens: TokenPair | None = None
    challenge_token: str | None = field(default=None, repr=False)
    challenge_expires_in: int | None = None
    mfa_grace_until: datetime | None = None


RefusalKind = Literal[
    "busy",
    "locked",
    "wrong_password",
    "wrong_code",
    "password_required",
    "step_up_required",
    "no_session",
    "too_young",
    "policy",
    "reused",
    "reauth_required",
    "not_found",
]


@dataclass(frozen=True, slots=True)
class Refusal:
    """A refusal the route turns into its status (401, 403 or 422) after the session closed.
    `breaches` carries the broken password rules for a `policy` refusal."""

    kind: RefusalKind
    breaches: list[password_policy.Breach] = field(default_factory=list)


@cache
def dummy_hash() -> str:
    """A real argon2id hash of a random secret: an unknown email still costs one verify."""
    return hash_password(secrets.token_urlsafe(32))


def inet(ip: str | None) -> str | None:
    """The address for an `inet` column, or None when the limiter's value is not an IP."""
    if ip is None:
        return None
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def _agent(user_agent: str | None) -> str | None:
    return user_agent[:USER_AGENT_MAX] if user_agent else None


def _now() -> datetime:
    return datetime.now(UTC)


def _enforcing() -> bool:
    return get_settings().mfa_enforcement == "all"


def _ordered(methods: set[str] | frozenset[str]) -> list[AuthMethod]:
    return [m for m in METHOD_ORDER if m in methods]


def _unix(moment: datetime | None) -> int | None:
    return None if moment is None else int(moment.timestamp())


def _from_unix(moment: int | None) -> datetime | None:
    return None if moment is None else datetime.fromtimestamp(moment, UTC)


async def _issue(
    session: Any,
    *,
    user_id: UUID,
    tenant_id: UUID,
    role: str,
    email: str,
    family_id: UUID,
    ip: str | None,
    user_agent: str | None,
    amr: list[AuthMethod] | None = None,
    mfa_at: datetime | None = None,
) -> tuple[str, str, UUID]:
    """A signed access + refresh pair for one tenant, the refresh row stored by hash with the
    session proof (`amr`, `mfa_at`). The tokens carry `amr`, `mfa_at` and `sid` (the family)
    only under `MFA_ENFORCEMENT=all`: with MFA off they keep today's ten claims. The caller has
    set `app.tenant_id` = `tenant_id` and `app.user_id` = `user_id` (Q10)."""
    claim_extra: dict[str, Any] = {}
    if _enforcing():
        claim_extra = {"amr": amr or ["pwd"], "mfa_at": _unix(mfa_at), "sid": family_id}
    access = issue_claims(sub=user_id, tid=tenant_id, role=role, email=email, typ="access", **claim_extra)  # type: ignore[arg-type]
    refresh_claims = issue_claims(sub=user_id, tid=tenant_id, role=role, email=email, typ="refresh", **claim_extra)  # type: ignore[arg-type]
    raw_refresh = encode(refresh_claims)
    row_id = await RefreshTokenRepository(session).insert(
        tenant_id=tenant_id,
        user_id=user_id,
        family_id=family_id,
        token_hash=token_hash(raw_refresh),
        expires_at=datetime.fromtimestamp(refresh_claims.exp, UTC),
        ip=inet(ip),
        user_agent=_agent(user_agent),
        amr=list(amr) if amr else None,
        mfa_at=mfa_at,
    )
    return encode(access), raw_refresh, row_id


def _access_ttl() -> int:
    return get_settings().access_token_ttl_minutes * 60


def _membership(row: MembershipRow) -> Membership:
    return Membership(row.tenant_id, row.tenant_name, row.role)


async def _gate(session: Any, user_id: UUID, *, ip: str | None) -> AuthState | AuthBusy | None:
    """`app.user_id` = the user, then the row-locked lockout state (the per-user gate)."""
    await set_gucs(session, user_id=user_id)
    state = await UserRepository(session).begin_auth_attempt(user_id)
    if state is AUTH_BUSY:
        auth_events.log_event(
            "auth.attempt_busy", user_id=user_id, tenant_id=None, ip=inet(ip), warning=True
        )
    return state


def _locked(state: AuthState | AuthBusy | None) -> bool:
    return isinstance(state, AuthState) and is_locked(state.locked_until, _now())


async def _count_failure(
    session: Any, user_id: UUID, *, ip: str | None, action: str, tenant_id: UUID | None = None
) -> None:
    """One failed password or code: the counter, the event, and `auth.locked` when it locks."""
    locked_now = await UserRepository(session).note_auth_failure(user_id)
    await auth_events.record(
        session, action=action, user_id=user_id, tenant_id=tenant_id, ip=inet(ip)
    )
    if locked_now:
        await auth_events.record(
            session, action="auth.locked", user_id=user_id, tenant_id=tenant_id, ip=inet(ip)
        )


def _facts(
    user: UserRow,
    *,
    has_factor: bool,
    memberships: list[MembershipRow],
    compliant: bool | None,
) -> SignInFacts:
    settings = get_settings()
    return SignInFacts(
        enforcement=settings.mfa_enforcement,
        grace_until=settings.mfa_grace_until,
        now=_now(),
        is_platform_admin=user.is_platform_admin,
        is_active=user.is_active,
        has_live_membership=bool(memberships),
        has_active_factor=has_factor,
        first_login=user.last_login_at is None,
        password_changed_at=user.password_changed_at or _now(),
        password_must_change=user.password_must_change,
        password_compliant=compliant,
    )


async def _sign_in(
    session: Any,
    user: UserRow,
    *,
    memberships: list[MembershipRow],
    proven: frozenset[str],
    mfa_at: datetime | None,
    compliant: bool | None,
    ip: str | None,
    user_agent: str | None,
) -> SignInResult | None:
    """The one place a sign-in ends (Google sign-in reuses it later): None for an inactive user
    or no live membership; a challenge for the next step; or tokens for the most recently used
    live tenant, which clears the lockout counter. `app.user_id` is set."""
    has_factor = await MfaFactorRepository(session).has_active(user.id)
    facts = _facts(user, has_factor=has_factor, memberships=memberships, compliant=compliant)
    step = next_step(facts, proven=proven)
    if step is None:
        return None
    grace = facts.grace_until if in_grace(facts) else None
    if step != "signed_in":
        raw, ttl = await mfa.new_challenge(
            session,
            user_id=user.id,
            purpose=PURPOSE_FOR[step],
            amr=_ordered(proven),
            ip=inet(ip),
            user_agent=_agent(user_agent),
            mfa_at=mfa_at,
        )
        return SignInResult(
            step, challenge_token=raw, challenge_expires_in=ttl, mfa_grace_until=grace
        )
    users = UserRepository(session)
    await users.clear_auth_failures(user.id)
    chosen = memberships[0]
    await set_gucs(session, tenant_id=chosen.tenant_id)
    await MembershipRepository(session).touch_last_used(chosen.tenant_id, user.id)
    await users.touch_login(user.id)
    access, refresh_token, _ = await _issue(
        session,
        user_id=user.id,
        tenant_id=chosen.tenant_id,
        role=chosen.role,
        email=user.email,
        family_id=uuid.uuid4(),
        ip=ip,
        user_agent=user_agent,
        amr=_ordered(proven),
        mfa_at=mfa_at,
    )
    await auth_events.record(
        session, action="auth.login", user_id=user.id, tenant_id=chosen.tenant_id, ip=inet(ip)
    )
    pair = TokenPair(access, refresh_token, _access_ttl(), [_membership(m) for m in memberships])
    return SignInResult("signed_in", tokens=pair, mfa_grace_until=grace)


async def _login_failed(session: Any, user_id: UUID | None, reason: str, ip: str | None) -> None:
    await auth_events.record(
        session,
        action="auth.login_failed",
        user_id=user_id,
        tenant_id=None,
        ip=inet(ip),
        reason=reason,
        actor_type="system",
    )


# ---- login (CODE-PLAN S10 C8; DATABASE.md:308 GUC order; DPP-1 password step) ----


async def login(
    email: str, password: str, *, ip: str | None, user_agent: str | None
) -> SignInResult | None:
    """Email + password in; tokens or the next challenge out, or None for the one 401 (an
    unknown email, a wrong password, a locked account, an inactive user and no live membership
    look the same: one argon2 verify each). A failure writes `auth.login_failed` and commits
    before the caller answers 401 (Q10 (4))."""
    async with identity_session() as session:
        await set_gucs(session, login_email=email)
        users = UserRepository(session)
        user = await users.by_email(email)
        state: AuthState | AuthBusy | None = None
        if user is not None:
            state = await _gate(session, user.id, ip=ip)
        stored = user.hashed_password if user is not None and user.hashed_password else None
        verified = await off_loop(verify_password, password, stored or dummy_hash())
        if user is None or state is None:
            await _login_failed(session, None if user is None else user.id, "unknown_email", ip)
            return None
        if isinstance(state, AuthBusy):
            await _login_failed(session, user.id, "busy", ip)
            return None
        if is_locked(state.locked_until, _now()):
            await _login_failed(session, user.id, "locked", ip)
            return None
        if not verified or stored is None:
            await _login_failed(session, user.id, "wrong_password", ip)
            if await users.note_auth_failure(user.id):
                await auth_events.record(
                    session, action="auth.locked", user_id=user.id, tenant_id=None, ip=inet(ip)
                )
            return None
        if not user.is_active:
            await _login_failed(session, user.id, "inactive_user", ip)
            return None
        memberships = await MembershipRepository(session).list_active_for_user(user.id)
        if not memberships:
            await _login_failed(session, user.id, "no_active_membership", ip)
            return None
        compliant = not password_policy.check(
            password, email=user.email, display_name=user.display_name
        )
        has_factor = await MfaFactorRepository(session).has_active(user.id)
        facts = _facts(user, has_factor=has_factor, memberships=memberships, compliant=compliant)
        if must_persist_noncompliance(facts):
            await users.set_must_change(user.id)
            user = replace(user, password_must_change=True)
        result = await _sign_in(
            session,
            user,
            memberships=memberships,
            proven=frozenset({"pwd"}),
            mfa_at=None,
            compliant=compliant,
            ip=ip,
            user_agent=user_agent,
        )
        if result is None:  # pragma: no cover - active and member were checked above
            await _login_failed(session, user.id, "no_active_membership", ip)
    return result


# ---- the challenge steps (DPP-1: /auth/login/mfa, /auth/login/mfa/{enroll,confirm},
# /auth/password/expired) ----


@dataclass(frozen=True, slots=True)
class _Opened:
    user: UserRow
    challenge: ChallengeRow
    memberships: list[MembershipRow]


async def _open_challenge(
    session: Any, raw_token: str, purpose: mfa.Purpose, *, ip: str | None
) -> _Opened | None:
    """The challenge named by `raw_token`, for `purpose`, with the user gate taken first (the
    lock order is user, then challenge). None for an unknown, consumed, expired or mismatched
    challenge, a busy gate or a locked account (nothing verified, nothing counted). An inactive
    user or a lost membership consumes the challenge (uncounted)."""
    digest = mfa.challenge_digest(raw_token)
    await set_gucs(session, challenge_hash=digest)
    challenges = ChallengeRepository(session)
    user_id = await challenges.peek_user(digest)
    if user_id is None:
        return None
    state = await _gate(session, user_id, ip=ip)
    if not isinstance(state, AuthState):
        return None
    challenge = await challenges.lock(digest)
    if (
        challenge is None
        or challenge.user_id != user_id
        or challenge.purpose != purpose
        or challenge.expires_at <= _now()
    ):
        return None
    if is_locked(state.locked_until, _now()):
        return None
    user = await UserRepository(session).self_row(user_id)
    memberships = await MembershipRepository(session).list_active_for_user(user_id)
    if user is None or not user.is_active or not memberships:
        await challenges.consume(challenge.id)
        return None
    return _Opened(user, challenge, memberships)


async def _wrong_code(session: Any, opened: _Opened, *, ip: str | None) -> None:
    await ChallengeRepository(session).fail(opened.challenge.id)
    await _count_failure(session, opened.user.id, ip=ip, action="auth.mfa_failed")


async def login_mfa(
    challenge_token: str,
    *,
    code: str | None,
    recovery_code: str | None,
    ip: str | None,
    user_agent: str | None,
) -> SignInResult | None:
    """The code step after the password: a TOTP code or a recovery code against a `login`
    challenge; tokens (or the next challenge), or None for the one 401."""
    async with identity_session() as session:
        opened = await _open_challenge(session, challenge_token, "login", ip=ip)
        if opened is None:
            return None
        method = await mfa.verify_second_factor(
            session, user_id=opened.user.id, code=code, recovery_code=recovery_code
        )
        if method is None:
            await _wrong_code(session, opened, ip=ip)
            return None
        await ChallengeRepository(session).consume(opened.challenge.id)
        if method == "rc":
            await auth_events.record(
                session,
                action="auth.mfa_recovery_used",
                user_id=opened.user.id,
                tenant_id=None,
                ip=inet(ip),
            )
        return await _sign_in(
            session,
            opened.user,
            memberships=opened.memberships,
            proven=frozenset(opened.challenge.amr) | {method},
            mfa_at=_now(),
            compliant=None,
            ip=ip,
            user_agent=user_agent,
        )


async def login_enroll(challenge_token: str, *, ip: str | None) -> mfa.Enrollment | None:
    """The forced enrolment's first half: a new pending secret against an `enroll` challenge,
    only while the user has no active factor (a password alone never binds a second one)."""
    async with identity_session() as session:
        opened = await _open_challenge(session, challenge_token, "enroll", ip=ip)
        if opened is None or await MfaFactorRepository(session).has_active(opened.user.id):
            return None
        return await mfa.start_enrollment(session, user_id=opened.user.id, email=opened.user.email)


@dataclass(frozen=True, slots=True)
class Confirmed:
    recovery_codes: list[str] = field(repr=False)
    result: SignInResult | None = None


async def login_confirm(
    challenge_token: str, code: str, *, ip: str | None, user_agent: str | None
) -> Confirmed | None:
    """The forced enrolment's second half: the code proves the pending secret, which becomes the
    active factor with 10 recovery codes; the challenge is consumed, every refresh family of the
    user is revoked, and the sign-in goes on with `amr` holding `otp`."""
    async with identity_session() as session:
        opened = await _open_challenge(session, challenge_token, "enroll", ip=ip)
        if opened is None or await MfaFactorRepository(session).has_active(opened.user.id):
            return None
        codes = await mfa.confirm_enrollment(session, user_id=opened.user.id, code=code)
        if codes is None:
            await _wrong_code(session, opened, ip=ip)
            return None
        await ChallengeRepository(session).consume(opened.challenge.id)
        await UserRepository(session).touch_mfa_changed(opened.user.id)
        await RefreshTokenRepository(session).revoke_user_everywhere(
            opened.user.id, keep_family=None, reason="mfa_enrolled"
        )
        await auth_events.record(
            session, action="auth.mfa_enrolled", user_id=opened.user.id, tenant_id=None, ip=inet(ip)
        )
        result = await _sign_in(
            session,
            opened.user,
            memberships=opened.memberships,
            proven=frozenset(opened.challenge.amr) | {"otp"},
            mfa_at=_now(),
            compliant=None,
            ip=ip,
            user_agent=user_agent,
        )
        return Confirmed(codes, result)


async def expired_password(
    challenge_token: str, new_password: str, *, ip: str | None, user_agent: str | None
) -> SignInResult | Refusal | None:
    """The forced password change: a `password_change` challenge and a new password that meets
    the rules and none of the last 10. Every refresh family is revoked, the challenge consumed,
    and the sign-in ends with the methods (and code time) proven before the change. None is the
    401; a `Refusal` (policy or reuse) is a 422 and keeps the challenge alive."""
    async with identity_session() as session:
        opened = await _open_challenge(session, challenge_token, "password_change", ip=ip)
        if opened is None:
            return None
        refusal = await _new_password_refusal(session, opened.user, new_password)
        if refusal is not None:
            return refusal
        await _store_password(session, opened.user, new_password)
        await RefreshTokenRepository(session).revoke_user_everywhere(
            opened.user.id, keep_family=None, reason="password_changed"
        )
        await ChallengeRepository(session).consume(opened.challenge.id)
        await auth_events.record(
            session,
            action="auth.password_changed",
            user_id=opened.user.id,
            tenant_id=None,
            ip=inet(ip),
        )
        fresh = await UserRepository(session).self_row(opened.user.id)
        if fresh is None:  # pragma: no cover - the row is locked by the gate
            return None
        return await _sign_in(
            session,
            fresh,
            memberships=opened.memberships,
            proven=frozenset(opened.challenge.amr),
            mfa_at=opened.challenge.mfa_at,
            compliant=True,
            ip=ip,
            user_agent=user_agent,
        )


async def _new_password_refusal(session: Any, user: UserRow, new_password: str) -> Refusal | None:
    """The set-time rules: the DPP composition and name rules, then none of the last 10."""
    breaches = password_policy.check(new_password, email=user.email, display_name=user.display_name)
    if breaches:
        return Refusal("policy", breaches)
    for previous in await UserRepository(session).password_hashes(user.id):
        if await off_loop(verify_password, new_password, previous):
            return Refusal("reused")
    return None


async def _store_password(session: Any, user: UserRow, new_password: str) -> None:
    history = PasswordHistoryRepository(session)
    if user.hashed_password:
        await history.push(user.id, user.hashed_password)
        await history.prune(user.id)
    await UserRepository(session).set_password(
        user.id, await off_loop(hash_password, new_password), must_change=False
    )


# ---- the bearer MFA routes (DPP-1: /auth/mfa/*, /auth/password, /auth/security) ----


async def _bearer_gate(session: Any, principal: Principal, *, ip: str | None) -> Refusal | None:
    """`app.tenant_id` = the caller's tenant first: every audit row these routes write (a counted
    failure included) carries it, and the `audit_insert` policy refuses it under any other
    tenant GUC (the 42501 would roll the counter back). Then the per-user gate."""
    await set_gucs(session, tenant_id=principal.tenant_id)
    state = await _gate(session, principal.user_id, ip=ip)
    if isinstance(state, AuthBusy):
        return Refusal("busy")
    if state is None:
        return Refusal("not_found")
    if _locked(state):
        return Refusal("locked")
    return None


async def totp_enroll(
    principal: Principal, claims: JWTClaims, current_password: str | None, *, ip: str | None
) -> mfa.Enrollment | Refusal:
    """A new pending secret from account settings. With no factor yet the current password is
    required; replacing a factor needs a step-up in the last 10 minutes (a TOTP or a recovery
    code, so a lost phone can be replaced without staff)."""
    async with identity_session() as session:
        refused = await _bearer_gate(session, principal, ip=ip)
        if refused is not None:
            return refused
        if await MfaFactorRepository(session).has_active(principal.user_id):
            if not step_up_fresh(claims.mfa_at, claims.amr, _now(), otp_only=False):
                return Refusal("step_up_required")
        else:
            if current_password is None:
                return Refusal("password_required")
            user = await UserRepository(session).self_row(principal.user_id)
            stored = user.hashed_password if user is not None else None
            if not stored or not await off_loop(verify_password, current_password, stored):
                await _count_failure(
                    session,
                    principal.user_id,
                    ip=ip,
                    action="auth.password_failed",
                    tenant_id=principal.tenant_id,
                )
                return Refusal("wrong_password")
        return await mfa.start_enrollment(session, user_id=principal.user_id, email=principal.email)


async def totp_confirm(
    principal: Principal, claims: JWTClaims, code: str, *, ip: str | None
) -> list[str] | Refusal:
    """The code proves the pending secret, which becomes the active factor with 10 recovery
    codes. The caller's family (`sid`) records the fresh code; the user's other families are
    revoked."""
    async with identity_session() as session:
        refused = await _bearer_gate(session, principal, ip=ip)
        if refused is not None:
            return refused
        if await MfaFactorRepository(session).has_active(principal.user_id) and not step_up_fresh(
            claims.mfa_at, claims.amr, _now(), otp_only=False
        ):
            return Refusal("step_up_required")
        codes = await mfa.confirm_enrollment(session, user_id=principal.user_id, code=code)
        if codes is None:
            await _count_failure(
                session,
                principal.user_id,
                ip=ip,
                action="auth.mfa_failed",
                tenant_id=principal.tenant_id,
            )
            return Refusal("wrong_code")
        await UserRepository(session).touch_mfa_changed(principal.user_id)
        tokens = RefreshTokenRepository(session)
        if claims.sid is not None:
            await tokens.step_up_family(
                principal.tenant_id,
                claims.sid,
                principal.user_id,
                amr=["pwd", "otp"],
                mfa_at=_now(),
            )
        await tokens.revoke_user_everywhere(
            principal.user_id, keep_family=claims.sid, reason="mfa_enrolled"
        )
        await auth_events.record(
            session,
            action="auth.mfa_enrolled",
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            ip=inet(ip),
        )
        return codes


async def regenerate_codes(principal: Principal, *, ip: str | None) -> list[str]:
    """A new set of 10 recovery codes (the route requires a step-up in the last 10 minutes)."""
    async with identity_session() as session:
        await set_gucs(session, tenant_id=principal.tenant_id, user_id=principal.user_id)
        codes = await mfa.regenerate_codes(session, user_id=principal.user_id)
        await auth_events.record(
            session,
            action="auth.mfa_codes_regenerated",
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            ip=inet(ip),
        )
    return codes


@dataclass(frozen=True, slots=True)
class StepUp:
    access_token: str = field(repr=False)
    expires_in: int


class _NoMatch(Exception):  # noqa: N818 - a control-flow signal inside one savepoint
    pass


class _FamilyGone(Exception):  # noqa: N818 - a control-flow signal inside one savepoint
    pass


async def step_up(
    principal: Principal,
    claims: JWTClaims,
    *,
    code: str | None,
    recovery_code: str | None,
    ip: str | None,
) -> StepUp | Refusal:
    """A fresh code for a sensitive action: a new access token with `mfa_at = now` and the same
    `sid`, whose family records the code too (so a refresh keeps it). A token with no `sid`
    (minted with MFA off) or a revoked family is `no_session` (401; the code is not used up)."""
    if claims.sid is None:
        return Refusal("no_session")
    async with identity_session() as session:
        refused = await _bearer_gate(session, principal, ip=ip)
        if refused is not None:
            return refused
        now = _now()
        method: mfa.Method | None = None
        try:
            async with session.begin_nested():
                method = await mfa.verify_second_factor(
                    session, user_id=principal.user_id, code=code, recovery_code=recovery_code
                )
                if method is None:
                    raise _NoMatch
                touched = await RefreshTokenRepository(session).step_up_family(
                    principal.tenant_id,
                    claims.sid,
                    principal.user_id,
                    amr=["pwd", method],
                    mfa_at=now,
                )
                if touched == 0:
                    raise _FamilyGone
        except _NoMatch:
            await _count_failure(
                session,
                principal.user_id,
                ip=ip,
                action="auth.mfa_failed",
                tenant_id=principal.tenant_id,
            )
            return Refusal("wrong_code")
        except _FamilyGone:
            return Refusal("no_session")
        assert method is not None  # noqa: S101 - set before the savepoint committed
        access = issue_claims(
            sub=principal.user_id,
            tid=principal.tenant_id,
            role=principal.role,
            email=principal.email,
            typ="access",
            amr=["pwd", method],
            mfa_at=_unix(now),
            sid=claims.sid,
        )
        await auth_events.record(
            session,
            action="auth.step_up",
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            ip=inet(ip),
            reason=method,
        )
    return StepUp(encode(access), _access_ttl())


async def change_password(
    principal: Principal,
    claims: JWTClaims,
    current_password: str,
    new_password: str,
    *,
    ip: str | None,
) -> Refusal | None:
    """A self-service change: the current password, at most once a day, the DPP rules and none
    of the last 10. The caller's other refresh families are revoked."""
    async with identity_session() as session:
        refused = await _bearer_gate(session, principal, ip=ip)
        if refused is not None:
            return refused
        user = await UserRepository(session).self_row(principal.user_id)
        stored = user.hashed_password if user is not None else None
        if user is None or not stored:
            return Refusal("wrong_password")
        if not await off_loop(verify_password, current_password, stored):
            await _count_failure(
                session,
                principal.user_id,
                ip=ip,
                action="auth.password_failed",
                tenant_id=principal.tenant_id,
            )
            return Refusal("wrong_password")
        if user.password_changed_at is not None and password_policy.too_young(
            user.password_changed_at, _now()
        ):
            return Refusal("too_young")
        refusal = await _new_password_refusal(session, user, new_password)
        if refusal is not None:
            return refusal
        await _store_password(session, user, new_password)
        await RefreshTokenRepository(session).revoke_user_everywhere(
            principal.user_id, keep_family=claims.sid, reason="password_changed"
        )
        await auth_events.record(
            session,
            action="auth.password_changed",
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            ip=inet(ip),
        )
    return None


@dataclass(frozen=True, slots=True)
class Security:
    mfa_enrolled: bool
    recovery_codes_left: int
    enforcement: str
    mfa_grace_until: datetime | None
    password_changed_at: datetime | None
    password_expires_at: datetime | None


async def security(principal: Principal) -> Security:
    """The account-security card: factor, codes left, enforcement, grace and password expiry."""
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        user = await UserRepository(session).self_row(principal.user_id)
        enrolled = await MfaFactorRepository(session).has_active(principal.user_id)
        left = await RecoveryCodeRepository(session).left(principal.user_id)
    settings = get_settings()
    changed = user.password_changed_at if user is not None else None
    grace = None
    if settings.mfa_enforcement == "all" and not enrolled and user is not None:
        facts = _facts(user, has_factor=False, memberships=[], compliant=None)
        grace = settings.mfa_grace_until if in_grace(facts) else None
    return Security(
        mfa_enrolled=enrolled,
        recovery_codes_left=left,
        enforcement=settings.mfa_enforcement,
        mfa_grace_until=grace,
        password_changed_at=changed,
        password_expires_at=(changed + password_policy.MAX_AGE) if changed else None,
    )


async def has_active_factor(user_id: UUID) -> bool:
    """Whether the user has a confirmed TOTP factor (the step-up guard's refusal path)."""
    async with identity_session() as session:
        await set_gucs(session, user_id=user_id)
        return await MfaFactorRepository(session).has_active(user_id)


# ---- refresh (CODE-PLAN S10 C9; PLAN Decision 8; DPP-1 7-day rule) ----


async def refresh(raw_token: str, *, ip: str | None, user_agent: str | None) -> TokenPair | None:
    """Rotate a refresh token: a new pair in the same family, or None for a 401. Reusing a
    rotated or revoked token revokes the whole family (`reuse_detected`), audits it and commits
    before the caller answers 401; a concurrent second refresh of one token is such a reuse.
    Under `MFA_ENFORCEMENT=all` past grace (none for admins) a family whose code is missing or
    7 days old is refused (`auth.refresh_reauth_required`)."""
    try:
        claims = decode(raw_token, typ="refresh")
    except InvalidToken:
        return None
    digest = token_hash(raw_token)
    async with identity_session() as session:
        # the tenant and user GUCs come from the verified token, before any locking read (Q10)
        await set_gucs(session, tenant_id=claims.tid, user_id=claims.sub, token_hash=digest)
        tokens = RefreshTokenRepository(session)
        row = await tokens.by_hash_for_update(digest)
        if row is None or row.tenant_id != claims.tid or row.user_id != claims.sub:
            return None
        if row.revoked_at is not None:
            await tokens.revoke_family(
                row.tenant_id, row.family_id, "reuse_detected", expect_live=False
            )
            await AuditRepository(session).write(
                tenant_id=row.tenant_id,
                actor_type="system",
                actor_user_id=None,
                action="auth.refresh_reuse_detected",
                entity_type="user",
                entity_id=row.user_id,
                ip=inet(ip),
            )
            return None
        if row.expires_at <= _now():
            return None
        memberships = MembershipRepository(session)
        member = await memberships.probe(row.tenant_id, row.user_id)
        if member is None:
            return None
        if not await _renewal_allowed(session, row.user_id, row.mfa_at):
            await auth_events.record(
                session,
                action="auth.refresh_reauth_required",
                user_id=row.user_id,
                tenant_id=row.tenant_id,
                ip=inet(ip),
                actor_type="system",
            )
            return None
        access, new_refresh, new_id = await _issue(
            session,
            user_id=row.user_id,
            tenant_id=row.tenant_id,
            role=member.role,
            email=member.email,
            family_id=row.family_id,
            ip=ip,
            user_agent=user_agent,
            amr=row.amr,  # type: ignore[arg-type]
            mfa_at=row.mfa_at,
        )
        await tokens.rotate(row.tenant_id, row.id, new_id)
        listed = await memberships.list_active_for_user(row.user_id)
    return TokenPair(access, new_refresh, _access_ttl(), [_membership(m) for m in listed])


async def _renewal_allowed(session: Any, user_id: UUID, mfa_at: datetime | None) -> bool:
    """`refresh_allowed` with the user's own admin flag (read only under `all`)."""
    settings = get_settings()
    if settings.mfa_enforcement != "all":
        return True
    admin = await UserRepository(session).is_platform_admin(user_id)
    return refresh_allowed(
        enforcement=settings.mfa_enforcement,
        grace_until=settings.mfa_grace_until,
        is_platform_admin=admin,
        mfa_at=mfa_at,
        now=_now(),
    )


# ---- logout and me (CODE-PLAN S10 C10) ----


async def logout(raw_token: str, *, ip: str | None) -> None:
    """Revoke the token's whole family. Idempotent: an invalid, expired, unknown or already
    revoked token changes nothing (no database write for a token that does not verify)."""
    try:
        claims = decode(raw_token, typ="refresh")
    except InvalidToken:
        return
    digest = token_hash(raw_token)
    async with identity_session() as session:
        await set_gucs(session, tenant_id=claims.tid, user_id=claims.sub, token_hash=digest)
        tokens = RefreshTokenRepository(session)
        row = await tokens.by_hash_for_update(digest)
        if row is None or row.tenant_id != claims.tid or row.user_id != claims.sub:
            return
        if row.revoked_at is not None:
            return
        await tokens.revoke_family(row.tenant_id, row.family_id, "logout", expect_live=True)
        await AuditRepository(session).write(
            tenant_id=row.tenant_id,
            actor_type="user",
            actor_user_id=row.user_id,
            action="auth.logout",
            entity_type="user",
            entity_id=row.user_id,
            ip=inet(ip),
        )


@dataclass(frozen=True, slots=True)
class Me:
    user_id: UUID
    email: str
    display_name: str | None
    tenant_id: UUID
    role: str
    memberships: list[Membership]
    is_platform_admin: bool = False


async def me(principal: Principal) -> Me:
    """The caller with every live membership (the switcher lists them) and their own platform
    admin flag (ADR-0019: the FE shows the sign-up queue only to admins)."""
    async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
        rows = await MembershipRepository(session).list_active_for_user(principal.user_id)
        admin = await UserRepository(session).is_platform_admin(principal.user_id)
    current = next((r for r in rows if r.tenant_id == principal.tenant_id), None)
    return Me(
        user_id=principal.user_id,
        email=principal.email,
        display_name=current.display_name if current is not None else None,
        tenant_id=principal.tenant_id,
        role=principal.role,
        memberships=[_membership(r) for r in rows],
        is_platform_admin=admin,
    )


# ---- tenant switch (CODE-PLAN S10 C11; PLAN Decision 4; DATABASE.md:310) ----


async def switch_tenant(
    principal: Principal,
    target: UUID,
    claims: JWTClaims,
    *,
    ip: str | None,
    user_agent: str | None,
) -> TokenPair | Refusal | None:
    """A new pair (a new family) for another of the caller's live tenants, or None (404): the
    target is checked against the caller's own active memberships only (memberships_own and
    tenants_switcher under `app.user_id`), then every write runs under the target's GUC. Under
    `all`, the access token's own code time passes the 7-day rule first, else `reauth_required`
    (401), so a pre-MFA or stale token cannot renew itself by switching. The new family keeps
    the token's `amr` and `mfa_at`."""
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        mfa_at = _from_unix(claims.mfa_at)
        if not await _renewal_allowed(session, principal.user_id, mfa_at):
            return Refusal("reauth_required")
        memberships = MembershipRepository(session)
        member = await memberships.probe(target, principal.user_id)
        if member is None:
            return None
        await set_gucs(session, tenant_id=target)
        await memberships.touch_last_used(target, principal.user_id)
        access, refresh_token, _ = await _issue(
            session,
            user_id=principal.user_id,
            tenant_id=target,
            role=member.role,
            email=member.email,
            family_id=uuid.uuid4(),
            ip=ip,
            user_agent=user_agent,
            amr=claims.amr or ["pwd"],
            mfa_at=mfa_at,
        )
        await AuditRepository(session).write(
            tenant_id=target,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="auth.tenant_switched",
            entity_type="user",
            entity_id=principal.user_id,
            ip=inet(ip),
        )
        listed = await memberships.list_active_for_user(principal.user_id)
    return TokenPair(access, refresh_token, _access_ttl(), [_membership(m) for m in listed])


__all__ = [
    "Confirmed",
    "Me",
    "Membership",
    "Refusal",
    "Security",
    "SignInResult",
    "StepUp",
    "TokenPair",
    "change_password",
    "dummy_hash",
    "expired_password",
    "has_active_factor",
    "inet",
    "login",
    "login_confirm",
    "login_enroll",
    "login_mfa",
    "logout",
    "me",
    "probe_principal",
    "refresh",
    "regenerate_codes",
    "security",
    "step_up",
    "switch_tenant",
    "totp_confirm",
    "totp_enroll",
]
