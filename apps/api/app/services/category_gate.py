"""Category availability (SI14): the stand-in for a product without a category, and (T8) which
categories the classifier may offer.

A product may have no category yet (0027, D3). Its rules are then the category-wide ones only, and
a seam request that needs a category slug carries `UNCATEGORIZED`. That is a wire value only, never
stored: no template, rule or dictionary row uses it, so the rule query `category = $1 OR category IS
NULL` returns the category-wide rows alone.
"""

from __future__ import annotations

from typing import Final

UNCATEGORIZED: Final = "uncategorized"


def rules_category(category: str | None) -> str:
    """The category to load rules for: the product's own, or `UNCATEGORIZED` (category-wide only)."""
    return category if category is not None else UNCATEGORIZED
