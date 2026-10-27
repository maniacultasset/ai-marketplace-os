"""TOTP factors, recovery codes and sign-in challenges (DPP-1, ADR-0021).

The only importer of `app.adapters.mfa_secrets` (import contract
`mfa-secret-unseal-only-in-mfa-service`): a TOTP secret is sealed with `seal_bytes` under the
`mfa-secret-key` wrapper and opened here, for one `match_step`, and nowhere else.

Every function runs inside the caller's session, after the caller has set `app.user_id` to the
user it names (the `*_self` RLS policies) and taken the user row gate. Nothing here raises for a
wrong code: a refusal is a None result the caller counts and answers after its session closes.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from app.adapters.mfa_secrets import unseal_bytes
from app.core import recovery_codes, totp
from app.core.crypto import (
    MFA_BLOB_MAGIC,
    SealedBytes,
    get_mfa_key_wrapper,
    mfa_aad,
    seal_bytes,
)
from app.repositories.mfa import ChallengeRepository, MfaFactorRepository, RecoveryCodeRepository
from app.services.sign_in_policy import CHALLENGE_TTL_SECONDS, ENROLL_CHALLENGE_TTL_SECONDS

Method = Literal["otp", "rc"]
Purpose = Literal["login", "enroll", "password_change"]
CHALLENGE_TOKEN_BYTES = 32


async def seal_secret(secret: bytes, *, user_id: UUID) -> SealedBytes:
    """A new TOTP secret sealed for `user_id` under the MFA key."""
    return await seal_bytes(
        secret,
        aad=mfa_aad(user_id),
        subject=user_id,
        wrapper=get_mfa_key_wrapper(),
        magic=MFA_BLOB_MAGIC,
    )


async def open_secret(blob: bytes, key_version: str, *, user_id: UUID) -> bytes:
    """The TOTP secret sealed for `user_id`; any failure is `MfaUnsealError`."""
    return await unseal_bytes(blob, key_version, user_id=user_id)


@dataclass(frozen=True, slots=True)
class Enrollment:
    otpauth_uri: str = field(repr=False)
    secret_b32: str = field(repr=False)


async def start_enrollment(session: Any, *, user_id: UUID, email: str) -> Enrollment:
    """A fresh pending factor (replacing an abandoned one); the active factor, if any, keeps
    working until the new one is confirmed."""
    secret = totp.new_secret()
    sealed = await seal_secret(secret, user_id=user_id)
    await MfaFactorRepository(session).replace_pending(user_id, sealed.blob, sealed.key_version)
    return Enrollment(totp.otpauth_uri(secret, account=email), totp.b32(secret))


async def _match(factor_blob: bytes, key_version: str, user_id: UUID, code: str) -> int | None:
    secret = await open_secret(factor_blob, key_version, user_id=user_id)
    return totp.match_step(secret, code, time.time())


async def confirm_enrollment(session: Any, *, user_id: UUID, code: str) -> list[str] | None:
    """The pending factor proven by `code` becomes the active one, with 10 new recovery codes
    (returned once, in display form); None when there is no pending factor or the code is
    wrong or already used."""
    factors = MfaFactorRepository(session)
    pending = await factors.pending(user_id)
    if pending is None:
        return None
    step = await _match(pending.secret_blob, pending.key_version, user_id, code)
    if step is None or not await factors.consume_step(pending.id, step):
        return None
    await factors.activate(pending.id, user_id)
    return await _new_codes(session, user_id)


async def _new_codes(session: Any, user_id: UUID) -> list[str]:
    codes = recovery_codes.new_codes()
    hashes = []
    for code in codes:
        normalised = recovery_codes.normalise(code)
        if normalised is None:  # pragma: no cover - new_codes only makes valid codes
            raise RuntimeError("generated a malformed recovery code")
        hashes.append(recovery_codes.code_hash(user_id, normalised))
    await RecoveryCodeRepository(session).replace(user_id, hashes)
    return codes


async def regenerate_codes(session: Any, *, user_id: UUID) -> list[str]:
    """A new set of 10; every old code stops working."""
    return await _new_codes(session, user_id)


async def verify_second_factor(
    session: Any, *, user_id: UUID, code: str | None = None, recovery_code: str | None = None
) -> Method | None:
    """`otp` for a TOTP code of the active factor (each step once), `rc` for an unused recovery
    code (used up here) while an active factor exists, or None. A malformed recovery code
    never reaches the database."""
    if code is not None:
        factors = MfaFactorRepository(session)
        active = await factors.active(user_id)
        if active is None:
            return None
        step = await _match(active.secret_blob, active.key_version, user_id, code)
        if step is None or not await factors.consume_step(active.id, step):
            return None
        return "otp"
    if recovery_code is not None:
        normalised = recovery_codes.normalise(recovery_code)
        if normalised is None:
            return None
        if not await MfaFactorRepository(session).has_active(user_id):
            return None  # codes back up a factor; none count without one (review SEC-3)
        digest = recovery_codes.code_hash(user_id, normalised)
        if await RecoveryCodeRepository(session).consume(user_id, digest):
            return "rc"
    return None


def challenge_digest(raw_token: str) -> bytes:
    return hashlib.sha256(raw_token.encode("utf-8")).digest()


def challenge_ttl(purpose: Purpose) -> int:
    return ENROLL_CHALLENGE_TTL_SECONDS if purpose == "enroll" else CHALLENGE_TTL_SECONDS


async def new_challenge(
    session: Any,
    *,
    user_id: UUID,
    purpose: Purpose,
    amr: Sequence[str],
    ip: str | None,
    user_agent: str | None,
    mfa_at: datetime | None = None,
) -> tuple[str, int]:
    """An opaque one-time token for the next sign-in step (only its sha256 is stored) and its
    lifetime in seconds: 15 minutes to enrol (installing an app takes time), else 5."""
    raw = secrets.token_urlsafe(CHALLENGE_TOKEN_BYTES)
    ttl = challenge_ttl(purpose)
    await ChallengeRepository(session).create(
        user_id=user_id,
        purpose=purpose,
        amr=sorted(set(amr), key=["pwd", "otp", "rc"].index),
        challenge_hash=challenge_digest(raw),
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        ip=ip,
        user_agent=user_agent,
        mfa_at=mfa_at if {"otp", "rc"} & set(amr) else None,
    )
    return raw, ttl
