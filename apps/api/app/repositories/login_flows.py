"""`login_flows` (revision 0012; ADR-0020): one Google sign-in between the browser's redirects.

A platform table with no `tenant_id`, so the tenant guard does not apply (it checks `TenantScoped`
tables only). FORCE RLS lets app_rw read and delete only the row whose `flow_hash` is
`app.token_hash` (set here first) and insert rows that expire within 15 minutes; there is no
UPDATE. Each consume is one `DELETE ... RETURNING` that also matches the phase, the browser
binding and the expiry, so a row is used at most once even under concurrent requests, and a
wrong binding consumes nothing. Row values are never logged.

- `authorize` (10 minutes): the PKCE verifier and the nonce's sha256, written by `start`.
- `handle` (60 seconds): the verified identity, written by the callback.
- `details` (15 minutes): a new user's sign-up form, written by `complete`.
- `link` (15 minutes): an existing user's password step, written by `complete`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Literal

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.tenancy import LoginFlow
from app.db.session import set_gucs

Phase = Literal["authorize", "handle", "details", "link"]
TTL_MAX_SECONDS: Final = 900  # lf_insert's 15 minutes


@dataclass(frozen=True, slots=True)
class FlowIdentity:
    """The verified Google identity a flow carries (never logged)."""

    sub: str = field(repr=False)
    email: str = field(repr=False)
    hd: str | None = field(default=None, repr=False)
    display_name: str | None = field(default=None, repr=False)
    given_name: str | None = field(default=None, repr=False)
    family_name: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class FlowRow:
    phase: str
    binding_hash: bytes = field(repr=False)
    code_verifier: str | None = field(default=None, repr=False)
    nonce_hash: bytes | None = field(default=None, repr=False)
    identity: FlowIdentity | None = None


def _hash(name: str, value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{name} must be 32 bytes (sha256)")
    return value


def _ttl(ttl_s: int) -> int:
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or not 1 <= ttl_s <= TTL_MAX_SECONDS:
        raise ValueError(f"ttl_s must be an int in 1..{TTL_MAX_SECONDS}")
    return ttl_s


_COLUMNS: Final = (
    LoginFlow.phase,
    LoginFlow.binding_hash,
    LoginFlow.code_verifier,
    LoginFlow.nonce_hash,
    LoginFlow.sub,
    LoginFlow.email,
    LoginFlow.hd,
    LoginFlow.display_name,
    LoginFlow.given_name,
    LoginFlow.family_name,
)


def _row(found: Any) -> FlowRow:
    identity = None
    if found[4] is not None:
        identity = FlowIdentity(found[4], found[5], found[6], found[7], found[8], found[9])
    return FlowRow(
        phase=found[0],
        binding_hash=bytes(found[1]),
        code_verifier=found[2],
        nonce_hash=None if found[3] is None else bytes(found[3]),
        identity=identity,
    )


class LoginFlowRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def insert(
        self,
        *,
        phase: Phase,
        flow_hash: bytes,
        binding_hash: bytes,
        ttl_s: int,
        code_verifier: str | None = None,
        nonce_hash: bytes | None = None,
        identity: FlowIdentity | None = None,
    ) -> None:
        """A new row; the expiry is taken from the database clock (`now()`), which `lf_insert`
        bounds to 15 minutes. The id is chosen here, so no RETURNING has to pass the SELECT
        policy (it needs the row's own hash GUC)."""
        await self.db.execute(
            insert(LoginFlow).values(
                id=uuid.uuid4(),
                phase=phase,
                flow_hash=_hash("flow_hash", flow_hash),
                binding_hash=_hash("binding_hash", binding_hash),
                code_verifier=code_verifier,
                nonce_hash=None if nonce_hash is None else _hash("nonce_hash", nonce_hash),
                sub=identity.sub if identity else None,
                email=identity.email if identity else None,
                hd=identity.hd if identity else None,
                display_name=identity.display_name if identity else None,
                given_name=identity.given_name if identity else None,
                family_name=identity.family_name if identity else None,
                expires_at=func.now() + timedelta(seconds=_ttl(ttl_s)),
            )
        )

    async def peek(self, *, flow_hash: bytes, phase: Phase, binding_hash: bytes) -> FlowRow | None:
        """The live row of this phase and binding, left in place (the rate limit reads its email
        before the consume)."""
        await set_gucs(self.db, token_hash=_hash("flow_hash", flow_hash))
        stmt = select(*_COLUMNS).where(
            LoginFlow.flow_hash == flow_hash,
            LoginFlow.phase == phase,
            LoginFlow.binding_hash == _hash("binding_hash", binding_hash),
            LoginFlow.expires_at > func.now(),
        )
        found = (await self.db.execute(stmt)).one_or_none()
        return None if found is None else _row(found)

    async def consume(
        self, *, flow_hash: bytes, phase: Phase, binding_hash: bytes | None
    ) -> FlowRow | None:
        """The live row of this phase (and binding, when given), deleted; None if there is none.
        A wrong binding deletes nothing."""
        await set_gucs(self.db, token_hash=_hash("flow_hash", flow_hash))
        where = [
            LoginFlow.flow_hash == flow_hash,
            LoginFlow.phase == phase,
            LoginFlow.expires_at > func.now(),
        ]
        if binding_hash is not None:
            where.append(LoginFlow.binding_hash == _hash("binding_hash", binding_hash))
        stmt = (
            delete(LoginFlow)
            .where(*where)
            .returning(*_COLUMNS)
            .execution_options(synchronize_session=False)
        )
        found = (await self.db.execute(stmt)).one_or_none()
        return None if found is None else _row(found)

    async def prune(self) -> int:
        """Delete every expired row (`app_prune_login_flows`, the daily maintenance run)."""
        found = await self.db.execute(text("SELECT public.app_prune_login_flows()"))
        return int(found.scalar_one())


__all__ = ["FlowIdentity", "FlowRow", "LoginFlowRepository", "Phase"]
