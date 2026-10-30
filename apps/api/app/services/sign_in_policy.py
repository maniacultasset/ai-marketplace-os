"""The sign-in and lockout decisions of DPP-1, as pure functions (no database, no web code).

The caller checks the password (or the code) and the lock first; `next_step` then says what the
sign-in needs next. Google sign-in (ADR-0020) is a first factor only (`fed`): it never gets the
grace period and never skips the code, and a user with no password never meets the password-change
step. A trusted browser (ADR-0023) is the internal proof `dev`: it stands in for the code only
when the code is the last thing the sign-in misses, and never for enrolment, a password change or a
Google link; it is never stored and never reaches a token's `amr`. The counter itself is atomic SQL (`app_note_auth_failure` in migration
`0011_mfa_lockout`), whose threshold and lock length are the literals below; a unit test keeps the
two in step.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from app.core import password_policy

LOCK_THRESHOLD: Final = 10
LOCK_SECONDS: Final = 900
CHALLENGE_TTL_SECONDS: Final = 300
ENROLL_CHALLENGE_TTL_SECONDS: Final = 900
CHALLENGE_MAX_ATTEMPTS: Final = 5
# ADR-0023 (amends ADR-0021 §6): a code entered in the last 30 minutes
STEP_UP_MAX_AGE_SECONDS: Final = 1800
REAUTH_MAX_AGE_DAYS: Final = 7
# ADR-0023: a trusted browser's fixed life (the 0013 CHECK and guard cap the same values) and the
# most a user keeps (the oldest is evicted)
TRUST_DAYS_BRAND: Final = 30
TRUST_DAYS_ADMIN: Final = 7
TRUST_MAX_DEVICES: Final = 10
DEVICE_PROOF: Final = "dev"

Enforcement = Literal["off", "all"]
FirstFactor = Literal["pwd", "fed"]
Step = Literal["signed_in", "mfa_required", "mfa_enrollment_required", "password_change_required"]
SECOND_FACTORS: Final = frozenset({"otp", "rc"})


@dataclass(frozen=True)
class SignInFacts:
    enforcement: Enforcement
    grace_until: datetime | None
    now: datetime
    is_platform_admin: bool
    is_active: bool
    has_live_membership: bool
    has_active_factor: bool
    first_login: bool
    password_changed_at: datetime
    password_must_change: bool
    # None when the plaintext is unknown: every step after the password step.
    password_compliant: bool | None
    # ADR-0020: False for a Google-only user (no password to expire or change)
    has_password: bool = True
    # ADR-0020: `fed` when the sign-in began with Google (never a grace period)
    first_factor: FirstFactor = "pwd"
    # ADR-0020: a Google account waits to be linked once this sign-in finishes (no grace)
    linking: bool = False


def _grace_running(grace_until: datetime | None, now: datetime) -> bool:
    return grace_until is not None and now < grace_until


def in_grace(f: SignInFacts) -> bool:
    """True when this user may still sign in without a factor (it sets `mfa_grace_until`).
    Never for a Google sign-in, nor while it links a Google account (ADR-0020)."""
    return (
        f.enforcement == "all"
        and not f.has_active_factor
        and not f.is_platform_admin
        and not f.first_login
        and f.first_factor == "pwd"
        and not f.linking
        and _grace_running(f.grace_until, f.now)
    )


def _password_change_due(f: SignInFacts) -> bool:
    if not f.has_password:
        return False
    return (
        f.password_must_change
        or password_policy.expired(f.password_changed_at, f.now)
        or f.password_compliant is False
    )


def next_step(f: SignInFacts, *, proven: frozenset[str]) -> Step | None:
    """What the sign-in needs next, or None for today's uncounted 401 (inactive user or no live
    membership). Order: active and member, enforcement, factor, enrolment, password age, tokens."""
    if not f.is_active or not f.has_live_membership:
        return None
    if f.enforcement == "off":
        return "signed_in"
    if f.has_active_factor and not (proven & (SECOND_FACTORS | {DEVICE_PROOF})):
        return "mfa_required"
    if not f.has_active_factor and not in_grace(f):
        return "mfa_enrollment_required"
    if _password_change_due(f):
        return "password_change_required"
    return "signed_in"


def trust_usable(f: SignInFacts, *, proven: frozenset[str]) -> bool:
    """True when a trusted browser may stand in for the code (ADR-0023): MFA is enforced, the
    user has an active factor, no Google account waits to be linked, and the code is the last
    thing the sign-in misses (no enrolment, no password change)."""
    return (
        f.enforcement == "all"
        and f.has_active_factor
        and not f.linking
        and next_step(f, proven=proven | {DEVICE_PROOF}) == "signed_in"
    )


def trust_days(*, is_platform_admin: bool) -> int:
    """How long a new trusted browser lasts: 7 days for a platform admin, else 30."""
    return TRUST_DAYS_ADMIN if is_platform_admin else TRUST_DAYS_BRAND


def first_factor_of(proven: frozenset[str]) -> FirstFactor:
    """`fed` when Google, not a password, began the sign-in."""
    return "fed" if "fed" in proven and "pwd" not in proven else "pwd"


def must_persist_noncompliance(f: SignInFacts) -> bool:
    """True when the password step must write `password_must_change = true` before it answers a
    code step, because the later steps cannot see the plaintext."""
    return f.enforcement == "all" and f.password_compliant is False and not f.password_must_change


def is_locked(locked_until: datetime | None, now: datetime) -> bool:
    return locked_until is not None and locked_until > now


def refresh_allowed(
    *,
    enforcement: Enforcement,
    grace_until: datetime | None,
    is_platform_admin: bool,
    mfa_at: datetime | None,
    now: datetime,
    device_auth_at: datetime | None = None,
    device_live: bool = False,
) -> bool:
    """The 7-day re-authentication rule: under `all`, past grace (admins get none), a session
    renews only while its code is under 7 days old. A trusted-browser session (ADR-0023:
    `device_auth_at` set and no code since, `mfa_at < device_auth_at`) counts its 7 days from that
    sign-in instead, and only while its browser is still trusted (`device_live`)."""
    if enforcement == "off":
        return True
    if not is_platform_admin and _grace_running(grace_until, now):
        return True
    max_age = timedelta(days=REAUTH_MAX_AGE_DAYS)
    if device_auth_at is not None and (mfa_at is None or mfa_at < device_auth_at):
        return device_live and now - device_auth_at < max_age
    return mfa_at is not None and now - mfa_at < max_age


def step_up_fresh(
    mfa_at: int | None, amr: Sequence[str] | None, now: datetime, *, otp_only: bool
) -> bool:
    """True when a code was entered in the last 1800 seconds (a TOTP code only, when `otp_only`)."""
    if mfa_at is None or not amr:
        return False
    accepted = {"otp"} if otp_only else SECOND_FACTORS
    if not accepted.intersection(amr):
        return False
    # `mfa_at` is signed into the token, so a value ahead of `now` is only clock skew.
    return now.timestamp() - mfa_at <= STEP_UP_MAX_AGE_SECONDS
