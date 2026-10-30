"""MFA repositories (DPP-1, ADR-0021; revision 0011): TOTP factors, recovery codes, sign-in
challenges and password history.

The four tables are per-user platform tables with no `tenant_id`, so the tenant guard does not
apply to them (it checks `TenantScoped` tables only) and `identity_scope` is not needed (repo rule
R7 keeps it in `identity.py`). FORCE RLS narrows every statement instead: `*_self` lets `app_rw`
reach only `app.user_id`'s rows, and `mc_hash` the one challenge named by `app.challenge_hash`.
The service sets those GUCs first (`set_gucs`). Writes that must touch rows raise
`RowCountError` when they do not.
"""

from __future__ import annotations

import builtins
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.errors import RowCountError
from app.db.models.tenancy import (
    MfaChallenge,
    UserMfaFactor,
    UserPasswordHistory,
    UserRecoveryCode,
)

__all__ = [
    "ChallengeRepository",
    "ChallengeRow",
    "FactorRow",
    "MfaFactorRepository",
    "PasswordHistoryRepository",
    "RecoveryCodeRepository",
]

CHALLENGE_MAX_ATTEMPTS: Final = 5  # sign_in_policy.CHALLENGE_MAX_ATTEMPTS; the 0011 CHECK caps it


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", -1))


@dataclass(frozen=True, slots=True)
class FactorRow:
    id: uuid.UUID
    user_id: uuid.UUID
    status: str
    secret_blob: bytes = field(repr=False)
    key_version: str
    last_used_step: int | None


class MfaFactorRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _one(self, user_id: uuid.UUID, status: str) -> FactorRow | None:
        stmt = (
            select(UserMfaFactor)
            .options(undefer(UserMfaFactor.secret_blob))
            .where(UserMfaFactor.user_id == user_id, UserMfaFactor.status == status)
        )
        row = (await self.db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return FactorRow(
            row.id, row.user_id, row.status, row.secret_blob, row.key_version, row.last_used_step
        )

    async def active(self, user_id: uuid.UUID) -> FactorRow | None:
        return await self._one(user_id, "active")

    async def pending(self, user_id: uuid.UUID) -> FactorRow | None:
        return await self._one(user_id, "pending")

    async def has_active(self, user_id: uuid.UUID) -> bool:
        stmt = select(func.count()).where(
            UserMfaFactor.user_id == user_id, UserMfaFactor.status == "active"
        )
        return bool((await self.db.execute(stmt)).scalar_one())

    async def replace_pending(self, user_id: uuid.UUID, blob: bytes, key_version: str) -> uuid.UUID:
        """Drop the user's pending factor (an abandoned enrolment), then store a new one."""
        await self.db.execute(
            delete(UserMfaFactor)
            .where(UserMfaFactor.user_id == user_id, UserMfaFactor.status == "pending")
            .execution_options(synchronize_session=False)
        )
        factor_id = uuid.uuid4()
        await self.db.execute(
            insert(UserMfaFactor).values(
                id=factor_id, user_id=user_id, secret_blob=blob, key_version=key_version
            )
        )
        return factor_id

    async def activate(self, factor_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """The pending factor becomes the active one; the old active factor is deleted."""
        await self.db.execute(
            delete(UserMfaFactor)
            .where(UserMfaFactor.user_id == user_id, UserMfaFactor.status == "active")
            .execution_options(synchronize_session=False)
        )
        result = await self.db.execute(
            update(UserMfaFactor)
            .where(
                UserMfaFactor.id == factor_id,
                UserMfaFactor.user_id == user_id,
                UserMfaFactor.status == "pending",
            )
            .values(status="active", confirmed_at=func.now())
            .execution_options(synchronize_session=False)
        )
        if _rowcount(result) != 1:
            raise RowCountError(1, _rowcount(result))

    async def consume_step(self, factor_id: uuid.UUID, step: int) -> bool:
        """Record `step` as used; False when it (or a later step) already was, so each code
        works once even under concurrent submits."""
        result = await self.db.execute(
            update(UserMfaFactor)
            .where(
                UserMfaFactor.id == factor_id,
                (UserMfaFactor.last_used_step.is_(None)) | (UserMfaFactor.last_used_step < step),
            )
            .values(last_used_step=step, last_used_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1

    async def delete_all(self, user_id: uuid.UUID) -> int:
        result = await self.db.execute(
            delete(UserMfaFactor)
            .where(UserMfaFactor.user_id == user_id)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)


class RecoveryCodeRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def replace(self, user_id: uuid.UUID, hashes: builtins.list[bytes]) -> None:
        """A new set: every old code (used or not) stops working."""
        await self.db.execute(
            delete(UserRecoveryCode)
            .where(UserRecoveryCode.user_id == user_id)
            .execution_options(synchronize_session=False)
        )
        if hashes:
            await self.db.execute(
                insert(UserRecoveryCode),
                [{"id": uuid.uuid4(), "user_id": user_id, "code_hash": h} for h in hashes],
            )

    async def consume(self, user_id: uuid.UUID, code_hash: bytes) -> bool:
        """Use one code once: `UPDATE ... used_at IS NULL RETURNING`, so a race has one winner."""
        stmt = (
            update(UserRecoveryCode)
            .where(
                UserRecoveryCode.user_id == user_id,
                UserRecoveryCode.code_hash == code_hash,
                UserRecoveryCode.used_at.is_(None),
            )
            .values(used_at=func.now())
            .returning(UserRecoveryCode.id)
            .execution_options(synchronize_session=False)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def left(self, user_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            UserRecoveryCode.user_id == user_id, UserRecoveryCode.used_at.is_(None)
        )
        return int((await self.db.execute(stmt)).scalar_one())

    async def delete_all(self, user_id: uuid.UUID) -> int:
        result = await self.db.execute(
            delete(UserRecoveryCode)
            .where(UserRecoveryCode.user_id == user_id)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)


@dataclass(frozen=True, slots=True)
class ChallengeRow:
    id: uuid.UUID
    user_id: uuid.UUID
    purpose: str
    amr: builtins.list[str]
    mfa_at: datetime | None
    expires_at: datetime
    attempts: int
    # 0012 (ADR-0020): a Google account to link when this sign-in finishes
    link_google_sub: str | None = field(default=None, repr=False)
    # 0013 (ADR-0023): "trust this browser" chosen at the code step
    trust_device: bool = False


class ChallengeRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        purpose: str,
        amr: builtins.list[str],
        challenge_hash: bytes,
        expires_at: datetime,
        ip: str | None,
        user_agent: str | None,
        mfa_at: datetime | None = None,
        link_google_sub: str | None = None,
        trust_device: bool = False,
    ) -> uuid.UUID:
        """A new challenge; the user's expired or consumed ones are deleted first (needs
        `app.user_id` = `user_id`). `mfa_at` is set only when `amr` holds `otp` or `rc`."""
        await self.db.execute(
            delete(MfaChallenge)
            .where(
                MfaChallenge.user_id == user_id,
                (MfaChallenge.consumed_at.is_not(None)) | (MfaChallenge.expires_at <= func.now()),
            )
            .execution_options(synchronize_session=False)
        )
        challenge_id = uuid.uuid4()
        await self.db.execute(
            insert(MfaChallenge).values(
                id=challenge_id,
                user_id=user_id,
                purpose=purpose,
                amr=amr,
                challenge_hash=challenge_hash,
                expires_at=expires_at,
                ip=ip,
                user_agent=user_agent,
                mfa_at=mfa_at,
                link_google_sub=link_google_sub,
                trust_device=True if trust_device else None,
            )
        )
        return challenge_id

    async def peek_user(self, challenge_hash: bytes) -> uuid.UUID | None:
        """The owner of an unconsumed challenge, read without a lock (needs
        `app.challenge_hash`), so the caller can take the user row first."""
        stmt = select(MfaChallenge.user_id).where(
            MfaChallenge.challenge_hash == challenge_hash, MfaChallenge.consumed_at.is_(None)
        )
        found = (await self.db.execute(stmt)).scalar_one_or_none()
        return found if isinstance(found, uuid.UUID) else None

    async def lock(self, challenge_hash: bytes) -> ChallengeRow | None:
        """The unconsumed challenge, FOR UPDATE (parallel submits queue on it). Always after
        the user row lock: the lock order is user, then challenge."""
        stmt = (
            select(
                MfaChallenge.id,
                MfaChallenge.user_id,
                MfaChallenge.purpose,
                MfaChallenge.amr,
                MfaChallenge.mfa_at,
                MfaChallenge.expires_at,
                MfaChallenge.attempts,
                MfaChallenge.link_google_sub,
                MfaChallenge.trust_device,
            )
            .where(
                MfaChallenge.challenge_hash == challenge_hash, MfaChallenge.consumed_at.is_(None)
            )
            .with_for_update()
        )
        row = (await self.db.execute(stmt)).one_or_none()
        if row is None:
            return None
        return ChallengeRow(
            row[0],
            row[1],
            row[2],
            list(row[3]),
            row[4],
            row[5],
            int(row[6]),
            row[7],
            row[8] is True,
        )

    async def fail(self, challenge_id: uuid.UUID) -> int:
        """One wrong code: attempts + 1, and consumed (burned) at the cap. The new count."""
        stmt = (
            update(MfaChallenge)
            .where(MfaChallenge.id == challenge_id, MfaChallenge.consumed_at.is_(None))
            .values(
                attempts=MfaChallenge.attempts + 1,
                consumed_at=case(
                    (MfaChallenge.attempts + 1 >= CHALLENGE_MAX_ATTEMPTS, func.now()),
                    else_=MfaChallenge.consumed_at,
                ),
            )
            .returning(MfaChallenge.attempts)
            .execution_options(synchronize_session=False)
        )
        found = (await self.db.execute(stmt)).scalar_one_or_none()
        return CHALLENGE_MAX_ATTEMPTS if found is None else int(found)

    async def delete_open(self, user_id: uuid.UUID) -> int:
        """Every unconsumed challenge of the user deleted (a password change ends the sign-ins
        begun under the old password; needs `app.user_id` = `user_id`). The count."""
        result = await self.db.execute(
            delete(MfaChallenge)
            .where(MfaChallenge.user_id == user_id, MfaChallenge.consumed_at.is_(None))
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)

    async def consume(self, challenge_id: uuid.UUID) -> None:
        result = await self.db.execute(
            update(MfaChallenge)
            .where(MfaChallenge.id == challenge_id, MfaChallenge.consumed_at.is_(None))
            .values(consumed_at=func.now())
            .execution_options(synchronize_session=False)
        )
        if _rowcount(result) != 1:
            raise RowCountError(1, _rowcount(result))


class PasswordHistoryRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def push(self, user_id: uuid.UUID, old_hash: str) -> None:
        await self.db.execute(
            insert(UserPasswordHistory).values(
                id=uuid.uuid4(),
                user_id=user_id,
                hashed_password=old_hash,
                # the wall clock, not the column's `now()` (the transaction start): two pushes in
                # one transaction still order newest first, so `prune` keeps the right nine
                created_at=func.clock_timestamp(),
            )
        )

    async def prune(self, user_id: uuid.UUID, keep: int = 9) -> int:
        """Keep the newest `keep` rows (with the current hash: the last 10)."""
        newest = (
            select(UserPasswordHistory.id)
            .where(UserPasswordHistory.user_id == user_id)
            .order_by(UserPasswordHistory.created_at.desc(), UserPasswordHistory.id.desc())
            .limit(keep)
        )
        result = await self.db.execute(
            delete(UserPasswordHistory)
            .where(UserPasswordHistory.user_id == user_id, UserPasswordHistory.id.not_in(newest))
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)
