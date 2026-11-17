"""Category availability (SI14): the stand-in for a product without a category, and (T8) which
categories the classifier may offer (by plain name, with the keys that tell them apart, and a last
`not_listed` option so the model can say none fits).

A product may have no category yet (0027, D3). Its rules are then the category-wide ones only, and
a seam request that needs a category slug carries `UNCATEGORIZED`. That is a wire value only, never
stored: no template, rule or dictionary row uses it, so the rule query `category = $1 OR category IS
NULL` returns the category-wide rows alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Protocol

from mkos_schemas.classify import CategoryOption

from app.core.config import Settings
from app.repositories.templates import CurrentCategory

UNCATEGORIZED: Final = "uncategorized"
LAMPS: Final = "home_decor_lamps"
# the seam caps (mkos_schemas.classify: 40 options, 30 key attributes of the dictionary-key shape)
MAX_OFFERED: Final = 40
MAX_KEY_ATTRIBUTES: Final = 30
_KEY: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Client feedback 17 Nov: the last option of every classify request, so the model can say that
# none of the offered categories fits. An offer only: never stored, never pickable (no template).
NOT_LISTED: Final = "not_listed"
NOT_LISTED_NAME: Final = "None of these: the product is not in this list"
# keys every apparel category holds: they tell the categories apart no better than nothing
SHARED_KEYS: Final = frozenset(
    {
        "color",
        "fabric_type",
        "country_of_origin",
        "manufacturer",
        "fiber_composition",
        "number_of_pieces",
    }
)


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
    name: str | None = None


def category_enabled(slug: str, settings: Settings) -> bool:
    """False only for lamps while `CATEGORY_LAMPS_ENABLED` is off (PLAN E): no new lamps listing
    and no lamps suggestion."""
    return slug != LAMPS or settings.category_lamps_enabled


def _key_attributes(row: CurrentCategory) -> tuple[str, ...]:
    """The keys that tell this category apart: every template key minus the shared ones, in
    template order, dictionary-key shape only, at most `MAX_KEY_ATTRIBUTES`."""
    keys = row.attribute_keys or row.required_attributes
    return tuple(k for k in keys if _KEY.fullmatch(k) and k not in SHARED_KEYS)[:MAX_KEY_ATTRIBUTES]


async def category_offers(uow: _HasTemplates, settings: Settings) -> list[CategoryOffer]:
    """The enabled categories with a current template, sorted, with their plain names, at most
    `MAX_OFFERED - 1` (the last request slot is `NOT_LISTED`)."""
    from app.services.catalog_categories import category_label  # it imports this module

    offers = [
        CategoryOffer(
            category=row.category,
            template_version=row.version,
            key_attributes=_key_attributes(row),
            name=category_label(row.category),
        )
        for row in await uow.templates.current_categories()
        if category_enabled(row.category, settings) and row.category != UNCATEGORIZED
    ]
    return offers[: MAX_OFFERED - 1]


def classify_options(offers: list[CategoryOffer]) -> list[CategoryOption]:
    """The request's options: the offers by name, then `NOT_LISTED` with no key attributes."""
    options = [
        CategoryOption(
            category=offer.category,
            name=offer.name,
            key_attributes=list(offer.key_attributes),
            template_version=offer.template_version,
        )
        for offer in offers
    ]
    options.append(CategoryOption(category=NOT_LISTED, name=NOT_LISTED_NAME))
    return options


def offer_digest_slugs(offers: list[CategoryOffer]) -> list[str]:
    """The offered slugs as the input digests cover them (with the `NOT_LISTED` option)."""
    return [*(offer.category for offer in offers), NOT_LISTED]


async def pickable(uow: _HasTemplates, category: str, settings: Settings) -> bool:
    """`category` has a current template and is switched on (not capped at `MAX_OFFERED`)."""
    if category == UNCATEGORIZED or not category_enabled(category, settings):
        return False
    return any(row.category == category for row in await uow.templates.current_categories())


async def allowed_categories(uow: _HasTemplates, settings: Settings) -> list[str]:
    """The slugs of `category_offers`."""
    return [offer.category for offer in await category_offers(uow, settings)]


def rules_category(category: str | None) -> str:
    """The category to load rules for: the product's own, or `UNCATEGORIZED` (category-wide only)."""
    return category if category is not None else UNCATEGORIZED
