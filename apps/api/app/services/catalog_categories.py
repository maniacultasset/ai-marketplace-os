"""Client feedback 11 Nov (item 5): the categories a seller can pick, built from the current
templates, with what each marketplace will be sent for them.

`build_categories(mappings, settings)` (pure) groups the current templates by category: its plain
label, whether it is switched on (lamps only while `CATEGORY_LAMPS_ENABLED` is on), and per
marketplace the product type the listing uses with its plain name and the external category id
(null until the browse nodes are mapped). The plain names of the product types live in the
adapters (`PRODUCT_TYPE_NAMES`, CP-D1: marketplace knowledge stays there). Only Amazon and
Flipkart rows are shown; a category with no row on either is left out. Platform rows only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from mkos_schemas.enums import Marketplace

from app.adapters.amazon import constants as amazon
from app.adapters.flipkart import constants as flipkart
from app.core.config import Settings, get_settings
from app.core.rbac import Principal
from app.repositories.templates import CurrentMapping
from app.repositories.uow import tenant_uow
from app.services.category_gate import UNCATEGORIZED, category_enabled

__all__ = [
    "CATEGORY_LABELS",
    "CategoryCatalog",
    "CategoryChoice",
    "MarketplaceMapping",
    "build_categories",
    "category_label",
    "list_categories",
    "type_name",
]

# The plain names the seller reads; a slug missing here reads as words.
CATEGORY_LABELS: Final[Mapping[str, str]] = {
    "ethnic_wear_kurta_sets": "Kurta sets",
    "tshirts": "T-shirts",
    "home_decor_lamps": "Lamps",
}
TYPE_NAMES: Final[Mapping[Marketplace, Mapping[str, str]]] = {
    Marketplace.AMAZON: amazon.PRODUCT_TYPE_NAMES,
    Marketplace.FLIPKART: flipkart.PRODUCT_TYPE_NAMES,
}
_SHOWN: Final = frozenset(m.value for m in Marketplace)


@dataclass(frozen=True, slots=True)
class MarketplaceMapping:
    marketplace: str
    product_type: str
    product_type_name: str
    external_category_id: str | None
    template_version: int


@dataclass(frozen=True, slots=True)
class CategoryChoice:
    category: str
    label: str
    enabled: bool
    marketplaces: tuple[MarketplaceMapping, ...]


@dataclass(frozen=True, slots=True)
class CategoryCatalog:
    items: tuple[CategoryChoice, ...]
    suggest_enabled: bool


def _words(code: str) -> str:
    text = " ".join(code.replace("-", " ").replace("_", " ").split())
    return text[:1].upper() + text[1:].lower() if text else code


def category_label(slug: str) -> str:
    """`ethnic_wear_kurta_sets` -> "Kurta sets"; an unknown slug reads as words."""
    return CATEGORY_LABELS.get(slug) or _words(slug)


def type_name(marketplace: str, product_type: str) -> str:
    """The plain name of a marketplace product type: the adapter's, else the code as words."""
    names = TYPE_NAMES.get(Marketplace(marketplace), {})
    return names.get(product_type) or _words(product_type)


def build_categories(
    mappings: Sequence[CurrentMapping], settings: Settings
) -> tuple[CategoryChoice, ...]:
    """The current templates grouped by category, sorted by label (pure)."""
    grouped: dict[str, list[MarketplaceMapping]] = {}
    for row in mappings:
        if row.marketplace not in _SHOWN or row.category == UNCATEGORIZED or not row.product_type:
            continue
        grouped.setdefault(row.category, []).append(
            MarketplaceMapping(
                marketplace=row.marketplace,
                product_type=row.product_type,
                product_type_name=type_name(row.marketplace, row.product_type),
                external_category_id=row.external_category_id,
                template_version=row.version,
            )
        )
    out = [
        CategoryChoice(
            category=slug,
            label=category_label(slug),
            enabled=category_enabled(slug, settings),
            marketplaces=tuple(sorted(rows, key=lambda m: m.marketplace)),
        )
        for slug, rows in grouped.items()
    ]
    return tuple(sorted(out, key=lambda c: (c.label.lower(), c.category)))


async def list_categories(principal: Principal) -> CategoryCatalog:
    settings = get_settings()
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        mappings = await uow.templates.current_mappings()
    return CategoryCatalog(
        items=build_categories(mappings, settings),
        suggest_enabled=settings.category_suggest_enabled,
    )
