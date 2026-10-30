"""Trusted browsers (revision 0013; ADR-0023): one row per browser and user, found by the sha256
of the browser's random token.

A per-user platform table with no `tenant_id`, like the MFA tables: FORCE RLS (`td_self`) lets
`app_rw` reach only `app.user_id`'s rows, so the service sets that GUC first (the per-user gate)
and every statement here also names the user. A token is matched only under its owner: a token of
another user never matches. Hashes are compared again in constant time after the lookup, and the
rows are locked (FOR UPDATE) so two sign-ins with one token are served one after the other.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core import device_tokens
from app.core.errors import RowCountError
from app.db.models.tenancy import TrustedDevice

__all__ = ["DeviceMatch", "TrustedDeviceRepository", "TrustedDeviceRow"]


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", -1))


@dataclass(frozen=True, slots=True)
class TrustedDeviceRow:
    id: uuid.UUID
    user_id: uuid.UUID
    label: str
    trusted_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    created_at: datetime
    token_hash: bytes = field(default=b"", repr=False)


@dataclass(frozen=True, slots=True)
class DeviceMatch:
    """What a sign-in's tokens found for one user: the live browser (or None), the rows whose
    previous token was presented again (`reused`) and the matched rows past their expiry."""

    live: TrustedDeviceRow | None = None
    reused: tuple[uuid.UUID, ...] = ()
    expired: tuple[uuid.UUID, ...] = ()


def _row(found: TrustedDevice) -> TrustedDeviceRow:
    return TrustedDeviceRow(
        id=found.id,
        user_id=found.user_id,
        label=found.label,
        trusted_at=found.trusted_at,
        expires_at=found.expires_at,
        last_used_at=found.last_used_at,
        created_at=found.created_at,
        token_hash=found.token_hash,
    )


class TrustedDeviceRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def insert(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: bytes,
        label: str,
        trusted_at: datetime,
        expires_at: datetime,
    ) -> uuid.UUID:
        """A new trusted browser (the guard checks the times and the admin cap)."""
        device_id = uuid.uuid4()
        await self.db.execute(
            insert(TrustedDevice).values(
                id=device_id,
                user_id=user_id,
                token_hash=token_hash,
                method="otp",
                label=label,
                trusted_at=trusted_at,
                expires_at=expires_at,
            )
        )
        return device_id

    async def match_for_update(
        self, user_id: uuid.UUID, digests: Sequence[bytes], *, now: datetime
    ) -> DeviceMatch:
        """The user's rows whose current or previous token is one of `digests`, locked. A
        current token on a live row is the match; a previous token is a reuse."""
        if not digests:
            return DeviceMatch()
        wanted = list(digests)
        stmt = (
            select(TrustedDevice)
            .options(undefer(TrustedDevice.token_hash), undefer(TrustedDevice.prev_token_hash))
            .where(
                TrustedDevice.user_id == user_id,
                or_(
                    TrustedDevice.token_hash.in_(wanted),
                    TrustedDevice.prev_token_hash.in_(wanted),
                ),
            )
            .order_by(TrustedDevice.created_at.desc(), TrustedDevice.id)
            .with_for_update()
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        live: TrustedDeviceRow | None = None
        reused: list[uuid.UUID] = []
        expired: list[uuid.UUID] = []
        for found in rows:
            current = any(device_tokens.matches(found.token_hash, d) for d in wanted)
            previous = found.prev_token_hash is not None and any(
                device_tokens.matches(found.prev_token_hash, d) for d in wanted
            )
            if previous and not current:
                reused.append(found.id)
            elif current and found.expires_at <= now:
                expired.append(found.id)
            elif current and live is None:
                live = _row(found)
        return DeviceMatch(live, tuple(reused), tuple(expired))

    async def rotate(
        self, device_id: uuid.UUID, user_id: uuid.UUID, *, old_hash: bytes, new_hash: bytes
    ) -> None:
        """A new token for the browser; the old one becomes `prev_token_hash` (the guard insists),
        and `last_used_at = now()`. The expiry never moves."""
        result = await self.db.execute(
            update(TrustedDevice)
            .where(
                TrustedDevice.id == device_id,
                TrustedDevice.user_id == user_id,
                TrustedDevice.token_hash == old_hash,
            )
            .values(token_hash=new_hash, prev_token_hash=old_hash, last_used_at=func.now())
            .execution_options(synchronize_session=False)
        )
        if _rowcount(result) != 1:
            raise RowCountError(1, _rowcount(result))

    async def list_for_user(
        self, user_id: uuid.UUID, *, now: datetime, limit: int = 10
    ) -> builtins.list[TrustedDeviceRow]:
        """The user's live trusted browsers, newest first."""
        stmt = (
            select(TrustedDevice)
            .where(TrustedDevice.user_id == user_id, TrustedDevice.expires_at > now)
            .order_by(TrustedDevice.created_at.desc(), TrustedDevice.id)
            .limit(limit)
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [
            TrustedDeviceRow(
                id=r.id,
                user_id=r.user_id,
                label=r.label,
                trusted_at=r.trusted_at,
                expires_at=r.expires_at,
                last_used_at=r.last_used_at,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def is_live(self, user_id: uuid.UUID, device_id: uuid.UUID, *, now: datetime) -> bool:
        stmt = select(func.count()).where(
            TrustedDevice.id == device_id,
            TrustedDevice.user_id == user_id,
            TrustedDevice.expires_at > now,
        )
        return bool((await self.db.execute(stmt)).scalar_one())

    async def delete(self, user_id: uuid.UUID, device_ids: Sequence[uuid.UUID]) -> int:
        """Delete the named browsers of the user; the count (another user's id deletes nothing)."""
        if not device_ids:
            return 0
        result = await self.db.execute(
            delete(TrustedDevice)
            .where(TrustedDevice.user_id == user_id, TrustedDevice.id.in_(list(device_ids)))
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)

    async def delete_all(self, user_id: uuid.UUID) -> int:
        """Every trusted browser of the user; the count."""
        result = await self.db.execute(
            delete(TrustedDevice)
            .where(TrustedDevice.user_id == user_id)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)

    async def evict_beyond(self, user_id: uuid.UUID, keep: int) -> builtins.list[uuid.UUID]:
        """Keep the user's `keep` newest browsers; delete the rest and return their ids."""
        newest = (
            select(TrustedDevice.id)
            .where(TrustedDevice.user_id == user_id)
            .order_by(TrustedDevice.created_at.desc(), TrustedDevice.id.desc())
            .limit(keep)
        )
        stmt = (
            delete(TrustedDevice)
            .where(TrustedDevice.user_id == user_id, TrustedDevice.id.not_in(newest))
            .returning(TrustedDevice.id)
            .execution_options(synchronize_session=False)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def prune(self) -> int:
        """Delete every expired row (`app_prune_trusted_devices`, the daily retention run)."""
        found = await self.db.execute(text("SELECT public.app_prune_trusted_devices()"))
        return int(found.scalar_one() or 0)
