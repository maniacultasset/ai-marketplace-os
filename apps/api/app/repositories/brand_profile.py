"""`BrandPromptRepository.for_prompt()`: the `GenerationContext.brand_profile` dict (DATABASE.md:231;
S18 D6).

Exactly: `tone`, `guidelines`, `glossary` (the tenant row) and `brand_names`, `brand_owner_type`,
`content_locale`, `brand_description` (the brand profile row; the description cut to 2000
characters) and `brand_voice` (client feedback item 19: the stored answers as a dict, or None;
`build_context` re-reads them through `services.brand_voice.voice_for_prompt`). Never `website_url`, `logo_storage_key` or a
party id. A tenant with no `brand_profiles` row gets the tenant fields plus `content_locale`
`en-IN`, an empty `brand_names` and no description or voice. Another tenant reads as an empty
profile. The voice shapes tone and emphasis only; the product facts stay in charge.

SI8 (CP-D1, CP-D9): `brand_choices()` are the tenant's own brand names (its non-blank
`brand_profiles.brand_names`, then `tenants.name`), read through this repository's tenant scope;
`default_brand_name()` is the first. No tenant id is ever passed in.
"""

from __future__ import annotations

from typing import Any, Final

from app.core.resolved_facts import brand_choices, default_brand
from app.db.models.facts import BrandProfile
from app.db.models.tenancy import Tenant
from app.repositories.base import TenantScopedRepository

DESCRIPTION_MAX: Final = 2000
DEFAULT_LOCALE: Final = "en-IN"
PROMPT_KEYS: Final = (
    "tone",
    "guidelines",
    "glossary",
    "brand_names",
    "brand_owner_type",
    "content_locale",
    "brand_description",
    "brand_voice",
)


def _voice(raw: object) -> dict[str, Any] | None:
    return dict(raw) if isinstance(raw, dict) else None


class _Brand(TenantScopedRepository[BrandProfile]):
    model = BrandProfile


class BrandPromptRepository(TenantScopedRepository[Tenant]):
    model = Tenant

    async def for_prompt(self) -> dict[str, Any]:
        tenant = (
            await self.db.execute(
                self._select().with_only_columns(Tenant.tone, Tenant.guidelines, Tenant.glossary)
            )
        ).one_or_none()
        brand = (
            await self.db.execute(
                _Brand(self.db, self.tenant_id)
                ._select()
                .with_only_columns(
                    BrandProfile.brand_names,
                    BrandProfile.brand_owner_type,
                    BrandProfile.content_locale,
                    BrandProfile.brand_description,
                    BrandProfile.brand_voice,
                )
            )
        ).one_or_none()
        description = None if brand is None else brand.brand_description
        return {
            "tone": None if tenant is None else tenant.tone,
            "guidelines": list(tenant.guidelines or []) if tenant is not None else [],
            "glossary": dict(tenant.glossary or {}) if tenant is not None else {},
            "brand_names": list(brand.brand_names or []) if brand is not None else [],
            "brand_owner_type": None if brand is None else brand.brand_owner_type,
            "content_locale": DEFAULT_LOCALE if brand is None else brand.content_locale,
            "brand_description": None if description is None else description[:DESCRIPTION_MAX],
            "brand_voice": _voice(None if brand is None else brand.brand_voice),
        }

    async def brand_choices(self) -> tuple[str, ...]:
        """The brand names this tenant may list under (`core.resolved_facts.brand_choices`): two
        tenant-scoped reads, its brand profile `brand_names` and its `tenants.name`."""
        names = (
            await self.db.execute(
                _Brand(self.db, self.tenant_id)
                ._select()
                .with_only_columns(BrandProfile.brand_names)
            )
        ).scalar_one_or_none()
        tenant = (
            await self.db.execute(self._select().with_only_columns(Tenant.name))
        ).scalar_one_or_none()
        return brand_choices(names, tenant)

    async def default_brand_name(self) -> str | None:
        """The brand a product without its own (or its style's) brand lists under."""
        return default_brand(await self.brand_choices())
