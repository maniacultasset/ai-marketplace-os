"""Category availability (SI14): the stand-in for a product without a category, and (T8) which
categories the classifier may offer.

A product may have no category yet (0027, D3). Its rules are then the category-wide ones only, and
a seam request that needs a category slug carries `UNCATEGORIZED`. That is a wire value only, never
stored: no template, rule or dictionary row uses it, so the rule query `category = $1 OR category IS
NULL` returns the category-wide rows alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol

from app.core.config import Settings
from app.repositories.templates import CurrentCategory

UNCATEGORIZED: Final = "uncategorized"
LAMPS: Final = "home_decor_lamps"
# the seam caps (mkos_schemas.classify: 40 options, 30 key attributes of the dictionary-key shape)
MAX_OFFERED: Final = 40
MAX_KEY_ATTRIBUTES: Final = 30
_KEY: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class _Templates(Protocol):
    async def current_categories(self) -> list[CurrentCategory]: ...


class _HasTemplates(Protocol):
    @property
    def templates(self) -> _Templates: ...


@dataclass(frozen=True, slots=True)
class CategoryOffer:
    """One category the classifier may suggest: a current template exists and it is enabled."""

    category: str
    template_version: int
    key_attributes: tuple[str, ...]


def category_enabled(slug: str, settings: Settings) -> bool:
    """False only for lamps while `CATEGORY_LAMPS_ENABLED` is off (PLAN E): no new lamps listing
    and no lamps suggestion."""
    return slug != LAMPS or settings.category_lamps_enabled


async def category_offers(uow: _HasTemplates, settings: Settings) -> list[CategoryOffer]:
    """The enabled categories with a current template, sorted, at most `MAX_OFFERED`."""
    offers = [
        CategoryOffer(
            category=row.category,
            template_version=row.version,
            key_attributes=tuple(k for k in row.required_attributes if _KEY.fullmatch(k))[
                :MAX_KEY_ATTRIBUTES
            ],
        )
        for row in await uow.templates.current_categories()
        if category_enabled(row.category, settings) and row.category != UNCATEGORIZED
    ]
    return offers[:MAX_OFFERED]


async def allowed_categories(uow: _HasTemplates, settings: Settings) -> list[str]:
    """The slugs of `category_offers`."""
    return [offer.category for offer in await category_offers(uow, settings)]


def rules_category(category: str | None) -> str:
    """The category to load rules for: the product's own, or `UNCATEGORIZED` (category-wide only)."""
    return category if category is not None else UNCATEGORIZED
