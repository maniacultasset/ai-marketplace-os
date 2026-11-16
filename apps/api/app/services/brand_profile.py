"""The brand profile API (SI1 T13; PLAN D2, AC6, AC7; decision 15, 16).

`GET /brand-profile` reads the tenant's default manufacturer and website with an `ETag`; a tenant
without a `brand_profiles` row reads as the empty profile, whose tag hashes the literal `none`.
`PATCH /brand-profile` (owner, admin) sets or clears either under `If-Match`:

1. `ensure()` creates the row when missing and says whether this call inserted it; the row is
   then locked.
2. The writes run first: the party is matched on its dedupe key (name, line 1, PIN, country) or
   created, and on a match line 2, city, state and GSTIN are updated (only this route may,
   decision 16); then the profile's party id and the value-less `provenance.manufacturer`.
   Client item 1: `website_url`, the column only (no `provenance` key: the brand provenance is
   a fact layer of every product, so a key there would move every product's facts digest). The
   website is stored only: it is never model input (`repositories/brand_profile.py`).
3. Nothing written → 200 with the current tag, whatever tag was sent (CP13, invariant 10).
   Something written → the tag is compared: against `brand_etag(tenant, None)` when this call
   inserted the row, else against the locked row's `updated_at`. A stale tag is 409
   `STALE_VERSION` and the transaction rolls back, party write included.
4. One `brand_profile.updated` audit row; `after` holds only the changed keys' `provenance`
   (`manufacturer`, `brand_description`, `website_url`; null when cleared) and, when the voice
   moved, `brand_voice`: the names of the voice fields that changed. Never the party's name,
   address or contacts (DATABASE.md:328), never the website, description or voice text. The
   website's entry is in the audit only, never in the row's `provenance`.

Client feedback item 19: `brand_description` (with its value-less seller Fact, as the
manufacturer) and `brand_voice` (`services.brand_voice`; every answer empty stores NULL) are
written the same way, under the same `If-Match`.

G1 (prod bulk test, 15 Nov 2026): `default_marketer`, `default_packer` and `default_importer` are
set as the manufacturer is (a party, `provenance.<role>`), so the grouped Inbox's `brand` questions
are answered once for the brand. With `INBOX_GROUPS_ENABLED` on, setting one checks again the
listings with an open question on it (`recheck_brand_questions`), after the save commits, and
closes the product-level questions on it no refusal links; a changed party default then returns
`affected_listings`, each listing's redraft verdict (`brand_party_listings`). Neither is chunked
or embedded: the generation context sends both whole (`BrandPromptRepository.for_prompt`), so no
`brand_embed` job is queued.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from structlog import get_logger

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError
from app.core.rbac import Principal
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.brand_voice import BrandVoice, changed_voice_fields, stored_voice
from app.services.intake_values import PartyInput
from app.services.product_facts import seller_column_fact
from app.services.products import PartyView, party_fields, party_view

__all__ = [
    "BrandProfileView",
    "brand_etag",
    "get_brand_profile",
    "update_brand_profile",
]

logger = get_logger(__name__)

BRAND_ETAG_NS: Final = uuid.UUID("579f7b2f-e7d6-4ba6-a109-dc17762550d2")
MANUFACTURER: Final = "default_manufacturer"
WEBSITE: Final = "website_url"
DESCRIPTION: Final = "brand_description"
VOICE: Final = "brand_voice"
PARTY_ROLES: Final = ("manufacturer", "marketer", "packer", "importer")
"""G1: the brand defaults set once for every product (`default_<role>`)."""
PARTY_FIELDS: Final = tuple(f"default_{role}" for role in PARTY_ROLES)
FIELDS: Final = frozenset({*PARTY_FIELDS, WEBSITE, DESCRIPTION, VOICE})


def brand_etag(tenant_id: uuid.UUID, updated_at: datetime | None) -> str:
    stamp = "none" if updated_at is None else updated_at.isoformat()
    return f'"{uuid.uuid5(BRAND_ETAG_NS, f"{tenant_id}:{stamp}")}"'


@dataclass(frozen=True, slots=True)
class BrandProfileView:
    default_manufacturer: PartyView | None
    updated_at: datetime | None
    etag: str
    website_url: str | None = None
    brand_description: str | None = None
    brand_voice: dict[str, Any] | None = None
    default_marketer: PartyView | None = None
    default_packer: PartyView | None = None
    default_importer: PartyView | None = None
    affected_listings: list[dict[str, Any]] | None = None


async def _view(uow: TenantUnitOfWork, tenant_id: uuid.UUID) -> BrandProfileView:
    row = await uow.brand_profiles.get()
    if row is None:
        return BrandProfileView(None, None, brand_etag(tenant_id, None))
    parties: dict[str, PartyView | None] = {}
    for role in PARTY_ROLES:
        party_id = getattr(row, f"default_{role}_party_id")
        found = None if party_id is None else await uow.parties.get(party_id)
        parties[role] = None if found is None else party_view(found, inherited=False)
    return BrandProfileView(
        parties["manufacturer"],
        row.updated_at,
        brand_etag(tenant_id, row.updated_at),
        website_url=row.website_url,
        brand_description=row.brand_description,
        brand_voice=row.brand_voice,
        default_marketer=parties["marketer"],
        default_packer=parties["packer"],
        default_importer=parties["importer"],
    )


async def get_brand_profile(principal: Principal) -> BrandProfileView:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await _view(uow, principal.tenant_id)


async def update_brand_profile(
    principal: Principal, fields: Mapping[str, Any], if_match: str
) -> BrandProfileView:
    if set(fields) - FIELDS:
        raise ValueError(
            "update_brand_profile takes default_manufacturer, default_marketer, default_packer,"
            " default_importer, website_url, brand_description and brand_voice"
        )
    now = datetime.now(UTC)
    fact = seller_column_fact(now=now)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        inserted = await uow.brand_profiles.ensure()
        row = await uow.brand_profiles.get_for_update()
        locked_at = row.updated_at
        voice_before = row.brand_voice
        wrote = False
        audited: dict[str, Any] = {}
        if WEBSITE in fields:
            url: str | None = fields[WEBSITE]
            if await uow.brand_profiles.set_website(url):
                wrote = True
                audited[WEBSITE] = None if url is None else fact
        roles_set: list[str] = []
        party_changed = False
        for role in PARTY_ROLES:
            if f"default_{role}" not in fields:
                continue
            party: PartyInput | None = fields[f"default_{role}"]
            party_id = None
            party_wrote = False
            if party is not None:
                party_id, party_wrote = await uow.parties.match_or_create_wrote(
                    party_fields(party, role), update_on_match=True
                )
            linked = await uow.brand_profiles.set_default_party(role, party_id, fact)
            if party_wrote and not linked:
                # the same party with a new address: move the profile's version too
                await uow.brand_profiles.touch()
            if party_wrote or linked:
                wrote = True
                party_changed = True
                audited[role] = None if party is None else fact
                if party is not None:
                    roles_set.append(role)
        if DESCRIPTION in fields:
            description: str | None = fields[DESCRIPTION]
            if await uow.brand_profiles.set_description(description, fact):
                wrote = True
                audited[DESCRIPTION] = None if description is None else fact
        voice_changed: list[str] = []
        if VOICE in fields:
            given: BrandVoice | None = fields[VOICE]
            voice = stored_voice(given)
            if await uow.brand_profiles.set_voice(voice):
                wrote = True
                voice_changed = changed_voice_fields(voice_before, voice)
        if not wrote:
            return await _view(uow, principal.tenant_id)
        expected = brand_etag(principal.tenant_id, None if inserted else locked_at)
        if expected != if_match:
            raise ConflictError(ErrorCode.STALE_VERSION, "brand_profile_etag")
        after: dict[str, Any] = {"provenance": audited}
        if voice_changed:
            after[VOICE] = voice_changed
        await uow.audit.write(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_user_id=principal.user_id,
            action="brand_profile.updated",
            entity_type="brand_profile",
            entity_id=principal.tenant_id,
            after=json.dumps(after, sort_keys=True),
        )
        await uow.session.refresh(row)
        view = await _view(uow, principal.tenant_id)
    if party_changed and get_settings().inbox_groups_enabled:
        affected = await brand_party_listings(principal, roles_set)
        view = dataclasses.replace(view, affected_listings=affected)
    return view


async def brand_party_listings(principal: Principal, roles: list[str]) -> list[dict[str, Any]]:
    """G1: the listings with an open question on one of the party `roles` just set, each with
    its redraft verdict (`inbox_groups.listing_verdicts`), read after `recheck_brand_questions`
    so a question the brand value closed no longer holds a listing back. The web redrafts only
    on this verdict. Best effort after the save committed: a failed read is logged and returns
    an empty list, never a 500."""
    from app.services.inbox_groups import listing_verdicts

    if not roles:
        return []
    targets = [f"facts.{role}" for role in roles]
    try:
        async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
            listing_ids = await uow.inbox_groups.listings.with_open_targets(
                targets, RECHECK_MAX, written_only=False
            )
    except Exception:
        logger.warning("brand.party_listings_failed", exc_info=True)
        return []
    await recheck_brand_questions(principal, roles)
    try:
        async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
            refs = await uow.inbox_groups.listings.by_ids(listing_ids)
            return await listing_verdicts(uow, refs, targets)
    except Exception:
        logger.warning("brand.party_listings_failed", exc_info=True)
        return []


RECHECK_MAX: Final = 100
"""The most listings one brand save checks again (the rest on their next check)."""


async def recheck_brand_questions(principal: Principal, roles: list[str]) -> int:
    """G1: after a brand default is set, check again (`listing_validate.validate_listing`, the
    dashboard's "Check again") every listing of a product with an open question on that party,
    so a question the brand value now answers closes without a click; and close (`superseded`)
    the product-level questions on that party that no listing refusal links (no check would
    ever close them) on products without their own party. Each step in its own transaction,
    after the save committed: a listing that cannot be checked (no version yet, being written,
    deleted) or any other failure is logged and left as it is, never undoing the save nor
    turning it into a 500. Returns how many listings were checked."""
    from app.services.listing_validate import validate_listing

    targets = [f"facts.{role}" for role in roles]
    try:
        async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
            closed = await uow.human_tasks.cancel_brand_party_tasks(roles)
            listing_ids = await uow.inbox_groups.listings.with_open_targets(targets, RECHECK_MAX)
    except Exception:
        logger.warning("brand.recheck_failed", exc_info=True)
        return 0
    if closed:
        logger.info("brand.party_questions_closed", count=closed)
    checked = 0
    for listing_id in listing_ids:
        try:
            async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
                await validate_listing(uow, principal, listing_id, None)
        except DomainError:
            continue
        except Exception:
            logger.warning(
                "brand.recheck_listing_failed", listing_id=str(listing_id), exc_info=True
            )
            continue
        checked += 1
    return checked
