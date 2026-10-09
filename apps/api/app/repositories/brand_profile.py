"""`BrandPromptRepository.for_prompt()`: the `GenerationContext.brand_profile` dict (DATABASE.md:231;
S18 D6).

Exactly: `tone`, `guidelines`, `glossary` (the tenant row) and `brand_names`, `brand_owner_type`,
`content_locale`, `brand_description` (the brand profile row; the description cut to 2000
characters). Never `website_url`, `logo_storage_key` or a party id. A tenant with no
`brand_profiles` row gets the tenant fields plus `content_locale` `en-IN` and an empty
`brand_names`. Another tenant reads as an empty profile.
"""

from __future__ import annotations

from typing import Any, Final

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
)


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
        }
