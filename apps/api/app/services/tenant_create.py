"""An owner adds another brand without a new sign-up review (ADR-0032).

`POST /tenants` (settings:manage, step-up, Idempotency-Key, per-user limit). The caller must own
the brand its token names: the probe has already proved that brand live (`trial` or `active`)
and the membership active, and the platform admin approved the caller when that brand was made.
They must own no suspended brand (a suspended trial cannot be escaped by adding a fresh one), and
own fewer than `TENANT_CREATE_MAX_OWNED` live brands; an owner with no `active` brand (still on
trial) owns at most `TRIAL_MAX_OWNED`, which keeps one trial's AI spend under the platform budget.

The new tenant is made exactly as an approved sign-up makes it (`signup.approve`): the same slug
rule, `trial`, the staff CLI's own statements through
`SignupRepository.create_tenant`, with the caller as its one owner. The trial clock is not reset:
an owner still on trial gives the new brand their latest trial end; an owner of an `active` brand
gets `DEFAULT_TRIAL_DAYS`. The audit rows (`tenant.created`,
`membership.created`, `tenant.added_by_owner`) are `user` rows naming the caller, in the new
tenant. A website given goes on the new brand profile (`website_url`).

Everything runs in the `idempotent` transaction of the caller's current tenant, under a per-user
advisory lock (the cap holds for parallel calls with different keys). The new tenant's writes run
under its own `app.tenant_id` (the t_tenant policies); the caller's is put back before the key is
stored. A replay answers the stored brand and makes nothing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from app.core.config import get_settings
from app.core.rbac import Principal
from app.repositories.identity import (
    AuditRepository,
    MembershipRepository,
    SignupRepository,
    SlugTakenError,
    TenantAdminRepository,
)
from app.repositories.parties import BrandProfileRepository
from app.repositories.transactions import set_gucs
from app.services.idempotency import idempotent
from app.services.signup import DEFAULT_TRIAL_DAYS, slug_candidates

METHOD: Final = "POST"
# owned brands while on trial: 3 x LLM_TENANT_DAILY_BUDGET_USD (2.5) stays under the platform's
# LLM_DAILY_BUDGET_USD (12), so one unpaid owner cannot spend the day's AI budget for everyone
TRIAL_MAX_OWNED: Final = 3
Refusal = Literal["not_owner", "suspended", "trial_cap_reached", "cap_reached"]
OwnedBrand = tuple[str, datetime | None]  # (status, trial_ends_at) of a live brand the caller owns

__all__ = [
    "METHOD",
    "TRIAL_MAX_OWNED",
    "OwnedBrand",
    "Refusal",
    "TenantCreateRefusedError",
    "create_tenant",
    "on_trial",
    "refusal",
    "trial_end",
]


class TenantCreateRefusedError(Exception):
    """The caller may not add a brand now; `reason` says why (the route answers 403)."""

    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason)
        self.reason: Refusal = reason


def on_trial(owned: Sequence[OwnedBrand]) -> bool:
    """No brand the caller owns is `active` (paid or activated by a platform admin)."""
    return not any(status == "active" for status, _ in owned)


def refusal(
    *, role: str, owned: Sequence[OwnedBrand], max_owned: int, suspended_owned: bool
) -> Refusal | None:
    """Why the caller may not add a brand, or None. `role` is the caller's role in the token's
    (live) brand; `owned` lists the live brands they own, that one included."""
    if role != "owner":
        return "not_owner"
    if suspended_owned:
        return "suspended"
    limit = min(max_owned, TRIAL_MAX_OWNED) if on_trial(owned) else max_owned
    if len(owned) >= limit:
        return "trial_cap_reached" if limit < max_owned else "cap_reached"
    return None


def trial_end(owned: Sequence[OwnedBrand]) -> datetime | None:
    """The new brand's trial end: the latest trial end among the owned brands while the caller is
    on trial, so adding a brand never restarts the clock. None (a fresh `DEFAULT_TRIAL_DAYS`) for
    an owner of an `active` brand, or when no owned trial has an end."""
    if not on_trial(owned):
        return None
    ends = [end for status, end in owned if status == "trial" and end is not None]
    return max(ends) if ends else None


@dataclass(frozen=True, slots=True)
class _Request:
    name: str
    website: str | None

    def payload(self) -> bytes:
        return json.dumps(
            {"name": self.name, "website": self.website}, sort_keys=True, separators=(",", ":")
        ).encode()


async def create_tenant(
    principal: Principal, name: str, website: str | None, idem_key: str, path: str
) -> tuple[int, dict[str, Any]]:
    """201 and the new brand (`TenantCreated` fields). Raises `TenantCreateRefusedError`."""
    request = _Request(name.strip(), website)

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        return 201, await _create(session, principal, request)

    return await idempotent(principal, idem_key, METHOD, path, request.payload(), handler)


async def _create(session: Any, principal: Principal, request: _Request) -> dict[str, Any]:
    await set_gucs(session, user_id=principal.user_id)
    memberships = MembershipRepository(session)
    await memberships.lock_brand_creation(principal.user_id)
    owned = await memberships.owned_live(principal.user_id)
    reason = refusal(
        role=principal.role,
        owned=owned,
        max_owned=get_settings().tenant_create_max_owned,
        suspended_owned=await memberships.has_suspended(principal.user_id, owner_only=True),
    )
    if reason is not None:
        raise TenantCreateRefusedError(reason)
    tenant_id = uuid.uuid4()
    await set_gucs(session, tenant_id=tenant_id)
    try:
        requests = SignupRepository(session)
        for slug in slug_candidates(request.name):
            try:
                await requests.create_tenant(
                    tenant_id=tenant_id,
                    name=request.name,
                    slug=slug,
                    trial_days=DEFAULT_TRIAL_DAYS,
                    owner_id=principal.user_id,
                    staff_email=None,
                    actor_user_id=principal.user_id,
                    trial_ends_at=trial_end(owned),
                )
                break
            except SlugTakenError:
                continue
        else:
            raise RuntimeError("no free tenant slug for the brand name")
        if request.website is not None:
            await BrandProfileRepository(session, tenant_id).create_with_website(request.website)
        await AuditRepository(session).write(
            tenant_id=tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="tenant.added_by_owner",
            entity_type="tenant",
            entity_id=tenant_id,
            after=json.dumps({"name": request.name}),
        )
        row = await TenantAdminRepository(session).get(tenant_id)
    finally:
        await set_gucs(session, tenant_id=principal.tenant_id)
    if row is None:
        raise RuntimeError("a new tenant is not readable")
    return {
        "id": str(row.id),
        "name": row.name,
        "slug": row.slug,
        "status": row.status,
        "trial_ends_at": None if row.trial_ends_at is None else row.trial_ends_at.isoformat(),
        "role": "owner",
    }
