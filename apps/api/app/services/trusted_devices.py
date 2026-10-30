"""Trusted browsers (ADR-0023): the pieces the sign-in flows share.

A browser is trusted only right after a correct TOTP code, for a fixed 30 days (7 for a platform
admin). The browser keeps a random 256-bit token (only its sha256 is stored, bound to the user)
and sends it in the JSON body of the next sign-in. A live token stands in for the code only when
the code is the last thing the sign-in misses (`sign_in_policy.trust_usable`); each use swaps the
token for a new one and the expiry never moves. A rotated token presented again is a theft
signal: that browser is forgotten and its sessions revoked.

Every function runs inside the caller's session, after the caller has set `app.user_id` (the
`td_self` policy) and taken the per-user gate. Audit rows: `auth.device_trusted` and
`auth.device_revoked` (reason `removed`, `sign_out_all`, `password_changed`, `mfa_changed`,
`evicted`, `expired` or `reuse_detected`). Tokens and hashes are never logged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID

from app.core import device_tokens, user_agent
from app.repositories.identity import RefreshTokenRepository
from app.repositories.trusted_devices import TrustedDeviceRepository, TrustedDeviceRow
from app.services import auth_events
from app.services.sign_in_policy import REAUTH_MAX_AGE_DAYS, TRUST_MAX_DEVICES, trust_days

__all__ = [
    "Lookup",
    "NewTrust",
    "Rotated",
    "forget_all",
    "lookup",
    "rotate",
    "trust",
]

TRUSTED: Final = "auth.device_trusted"
REVOKED: Final = "auth.device_revoked"


@dataclass(frozen=True, slots=True)
class Lookup:
    """The live browser the tokens name for this user, or None; `invalid` when tokens were sent
    and none of them names a live browser of this user."""

    device: TrustedDeviceRow | None = None
    invalid: bool = False


@dataclass(frozen=True, slots=True)
class Rotated:
    device_id: UUID
    trusted_at: datetime
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class NewTrust:
    device_id: UUID
    token: str = field(repr=False)
    expires_at: datetime


async def _revoked(
    session: Any,
    user_id: UUID,
    reason: str,
    *,
    ip: str | None,
    tenant_id: UUID | None = None,
    count: int = 1,
) -> None:
    for _ in range(count):
        await auth_events.record(
            session, action=REVOKED, user_id=user_id, tenant_id=tenant_id, ip=ip, reason=reason
        )


async def lookup(
    session: Any, user_id: UUID, tokens: Sequence[str], *, ip: str | None, now: datetime
) -> Lookup:
    """Match the sent tokens against the user's own browsers (locked). A reused token forgets
    that browser and revokes its sessions; a matched expired row is deleted. A malformed token
    never reaches the database. Nothing is rotated here."""
    if not tokens:
        return Lookup()
    wanted = [t for t in dict.fromkeys(tokens) if device_tokens.well_formed(t)]
    devices = TrustedDeviceRepository(session)
    match = await devices.match_for_update(
        user_id, [device_tokens.digest(t) for t in wanted], now=now
    )
    if match.reused:
        await devices.delete(user_id, match.reused)
        tokens_repo = RefreshTokenRepository(session)
        for device_id in match.reused:
            await tokens_repo.revoke_user_everywhere(
                user_id, keep_family=None, reason="device_reuse", device_id=device_id
            )
        auth_events.log_event(
            "auth.device_reuse", user_id=user_id, tenant_id=None, ip=ip, warning=True
        )
        await _revoked(session, user_id, "reuse_detected", ip=ip, count=len(match.reused))
        # fail safe: a replayed token anywhere in the list means no trusted sign-in this time
        return Lookup(invalid=True)
    if match.expired:
        await devices.delete(user_id, match.expired)
        await _revoked(session, user_id, "expired", ip=ip, count=len(match.expired))
    return Lookup(match.live, invalid=match.live is None)


async def rotate(session: Any, device: TrustedDeviceRow) -> Rotated:
    """A new token for a browser that just stood in for the code; the expiry stays."""
    token = device_tokens.new_token()
    await TrustedDeviceRepository(session).rotate(
        device.id, device.user_id, old_hash=device.token_hash, new_hash=device_tokens.digest(token)
    )
    return Rotated(device.id, device.trusted_at, token, device.expires_at)


async def trust(
    session: Any,
    user_id: UUID,
    *,
    is_platform_admin: bool,
    code_at: datetime,
    agent: str | None,
    ip: str | None,
    tenant_id: UUID | None,
) -> NewTrust:
    """Trust this browser after the TOTP code entered at `code_at`: a new row (30 days, 7 for
    an admin, from that code), the oldest beyond 10 evicted, `auth.device_trusted` audited."""
    token = device_tokens.new_token()
    expires_at = code_at + timedelta(days=trust_days(is_platform_admin=is_platform_admin))
    devices = TrustedDeviceRepository(session)
    device_id = await devices.insert(
        user_id=user_id,
        token_hash=device_tokens.digest(token),
        label=user_agent.summary(agent),
        trusted_at=code_at,
        expires_at=expires_at,
    )
    evicted = await devices.evict_beyond(user_id, TRUST_MAX_DEVICES)
    tokens_repo = RefreshTokenRepository(session)
    for old in evicted:
        await tokens_repo.revoke_user_everywhere(
            user_id, keep_family=None, reason="device_evicted", device_id=old
        )
    await auth_events.record(session, action=TRUSTED, user_id=user_id, tenant_id=tenant_id, ip=ip)
    await _revoked(session, user_id, "evicted", ip=ip, tenant_id=tenant_id, count=len(evicted))
    return NewTrust(device_id, token, expires_at)


async def forget_all(
    session: Any,
    user_id: UUID,
    reason: str,
    *,
    ip: str | None,
    tenant_id: UUID | None = None,
    keep_family: tuple[UUID, UUID] | None = None,
) -> int:
    """Every trusted browser of the user deleted (a credential changed, or sign out all), with
    one `auth.device_revoked` row when any was. The sessions they began are revoked by the
    caller's own revocation; `keep_family` (tenant, family) is the caller's session, kept but
    detached from its browser first (while its expiry can still be read), so it ends no later
    than 7 days after its trusted sign-in and never after the browser's own trust would have."""
    if keep_family is not None:
        await RefreshTokenRepository(session).detach_device(
            keep_family[0], keep_family[1], user_id, reauth_days=REAUTH_MAX_AGE_DAYS
        )
    count = await TrustedDeviceRepository(session).delete_all(user_id)
    if count:
        await auth_events.record(
            session, action=REVOKED, user_id=user_id, tenant_id=tenant_id, ip=ip, reason=reason
        )
    return count
