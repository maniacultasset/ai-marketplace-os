"""The brand profile API (SI1 T13; PLAN D2, AC6, AC7; decision 15, 16).

`GET /brand-profile` reads the tenant's default manufacturer with an `ETag`; a tenant without a
`brand_profiles` row reads as the empty profile, whose tag hashes the literal `none`.
`PATCH /brand-profile` (owner, admin) sets or clears it under `If-Match`:

1. `ensure()` creates the row when missing and says whether this call inserted it; the row is
   then locked.
2. The writes run first: the party is matched on its dedupe key (name, line 1, PIN, country) or
   created, and on a match line 2, city, state and GSTIN are updated (only this route may,
   decision 16); then the profile's party id and the value-less `provenance.manufacturer`.
3. Nothing written → 200 with the current tag, whatever tag was sent (CP13, invariant 10).
   Something written → the tag is compared: against `brand_etag(tenant, None)` when this call
   inserted the row, else against the locked row's `updated_at`. A stale tag is 409
   `STALE_VERSION` and the transaction rolls back, party write included.
4. One `brand_profile.updated` audit row; `after` holds only `provenance.manufacturer`, never
   the party's name, address or contacts (DATABASE.md:328).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.core.errors import ConflictError
from app.core.rbac import Principal
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.intake_values import PartyInput
from app.services.product_facts import seller_column_fact
from app.services.products import PartyView, party_fields, party_view

__all__ = [
    "BrandProfileView",
    "brand_etag",
    "get_brand_profile",
    "update_brand_profile",
]

BRAND_ETAG_NS: Final = uuid.UUID("579f7b2f-e7d6-4ba6-a109-dc17762550d2")
MANUFACTURER: Final = "default_manufacturer"


def brand_etag(tenant_id: uuid.UUID, updated_at: datetime | None) -> str:
    stamp = "none" if updated_at is None else updated_at.isoformat()
    return f'"{uuid.uuid5(BRAND_ETAG_NS, f"{tenant_id}:{stamp}")}"'


@dataclass(frozen=True, slots=True)
class BrandProfileView:
    default_manufacturer: PartyView | None
    updated_at: datetime | None
    etag: str


async def _view(uow: TenantUnitOfWork, tenant_id: uuid.UUID) -> BrandProfileView:
    row = await uow.brand_profiles.get()
    if row is None:
        return BrandProfileView(None, None, brand_etag(tenant_id, None))
    party = None
    if row.default_manufacturer_party_id is not None:
        found = await uow.parties.get(row.default_manufacturer_party_id)
        party = None if found is None else party_view(found, inherited=False)
    return BrandProfileView(party, row.updated_at, brand_etag(tenant_id, row.updated_at))


async def get_brand_profile(principal: Principal) -> BrandProfileView:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await _view(uow, principal.tenant_id)


async def update_brand_profile(
    principal: Principal, fields: Mapping[str, Any], if_match: str
) -> BrandProfileView:
    if set(fields) - {MANUFACTURER}:
        raise ValueError("update_brand_profile takes default_manufacturer only")
    now = datetime.now(UTC)
    fact = seller_column_fact(now=now)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        inserted = await uow.brand_profiles.ensure()
        row = await uow.brand_profiles.get_for_update()
        locked_at = row.updated_at
        wrote = False
        if MANUFACTURER in fields:
            party: PartyInput | None = fields[MANUFACTURER]
            party_id = None
            if party is not None:
                party_id, wrote = await uow.parties.match_or_create_wrote(
                    party_fields(party), update_on_match=True
                )
            linked = await uow.brand_profiles.set_default_manufacturer(party_id, fact)
            if wrote and not linked:
                # the same party with a new address: move the profile's version too
                await uow.brand_profiles.touch()
            wrote = wrote or linked
        if not wrote:
            return await _view(uow, principal.tenant_id)
        expected = brand_etag(principal.tenant_id, None if inserted else locked_at)
        if expected != if_match:
            raise ConflictError(ErrorCode.STALE_VERSION, "brand_profile_etag")
        stored = fields[MANUFACTURER]
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="brand_profile.updated",
            entity_type="brand_profile",
            entity_id=principal.tenant_id,
            after=json.dumps({"provenance": {"manufacturer": None if stored is None else fact}}),
        )
        await uow.session.refresh(row)
        return await _view(uow, principal.tenant_id)
