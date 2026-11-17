"""Client feedback 17 Nov: an honest category suggestion (a jeans photo suggested T-shirts).

Two checks after screening, both free (no AI call):

- `not_listed`: the model ranked the `not_listed` option first, so none of the offered categories
  fits. The candidates left are guesses.
- `looks_like`: the seller's own words (title, their category words, the attribute values) name a
  group (`bottomwear`, `topwear`, `ethnic`) that differs from the top candidate's group. The answer
  is the group, the candidates are guesses.

Either one keeps the suggestion in review: nothing is ever set by itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from mkos_schemas.classify import ClassifyResult, ProductText

from app.services.catalog_categories import category_group
from app.services.category_gate import NOT_LISTED

__all__ = ["GROUP_WORDS", "LOOKS_LIKE_GROUPS", "Honesty", "assess", "word_groups"]

# group -> the words that name it (whole words, case-insensitive; a space also matches a hyphen)
GROUP_WORDS: Final[Mapping[str, tuple[str, ...]]] = {
    "bottomwear": (
        "jeans",
        "jean",
        "denim",
        "trouser",
        "trousers",
        "pant",
        "pants",
        "chino",
        "chinos",
        "jogger",
        "joggers",
        "track pant",
        "track pants",
        "trackpant",
        "trackpants",
        "cargo",
        "cargos",
        "shorts",
        "short",
        "skirt",
        "skirts",
        "palazzo",
        "palazzos",
        "leggings",
    ),
    "topwear": (
        "t-shirt",
        "t-shirts",
        "tshirt",
        "tshirts",
        "tee",
        "tees",
        "shirt",
        "shirts",
        "top",
        "tops",
        "polo",
        "hoodie",
        "hoodies",
        "sweatshirt",
        "sweatshirts",
    ),
    "ethnic": (
        "kurta",
        "kurtas",
        "kurti",
        "kurtis",
        "saree",
        "sarees",
        "sari",
        "lehenga",
        "dupatta",
    ),
}
LOOKS_LIKE_GROUPS: Final = frozenset(GROUP_WORDS)
# "short sleeve" / "short kurta" describe a top, not shorts
_NOT_SHORTS: Final = r"(?![\s-]+(?:sleeve|sleeves|sleeved|kurta|kurti|length|top))"
# "table top lamp" / "top quality" are not topwear (a live lamp title must not contradict its pick)
_NOT_TOP_BEFORE: Final = r"(?<!table )(?<!table-)(?<!desk )(?<!desk-)(?<!counter )(?<!counter-)"
_NOT_TOP_AFTER: Final = (
    r"(?![\s-]+(?:quality|selling|seller|sellers|rated|brand|brands|class|grade|notch|lamp|light))"
)
# Attribute keys that describe how an item is made, not what it is: their values ("short" sleeve,
# "Denim" fabric, "polo" neck, "Table Top" lamp type) would name a group the item is not in.
_CONSTRUCTION_KEY: Final = re.compile(
    r"sleeve|neck|collar|fabric|material|fiber|fibre|pattern|fit|closure|colou?r|lamp|shade|light"
)


def _pattern(word: str) -> str:
    body = r"[\s-]+".join(re.escape(part) for part in re.split(r"[\s-]+", word))
    head, tail = "", ""
    if word == "short":
        tail = _NOT_SHORTS
    elif word in ("top", "tops"):
        head, tail = _NOT_TOP_BEFORE, _NOT_TOP_AFTER
    return rf"{head}(?<![a-z0-9]){body}(?![a-z0-9]){tail}"


_GROUP_RES: Final[Mapping[str, re.Pattern[str]]] = {
    group: re.compile("|".join(_pattern(w) for w in words), re.IGNORECASE)
    for group, words in GROUP_WORDS.items()
}


@dataclass(frozen=True, slots=True)
class Honesty:
    not_listed: bool = False
    looks_like: str | None = None

    @property
    def unsure(self) -> bool:
        return self.not_listed or self.looks_like is not None


def word_groups(texts: Iterable[str | None]) -> list[str]:
    """The groups the words name, in `GROUP_WORDS` order."""
    joined = " \n ".join(t for t in texts if t)
    return [group for group, pattern in _GROUP_RES.items() if pattern.search(joined)]


def _seller_words(text: ProductText) -> list[str | None]:
    values = (v for k, v in text.attributes.items() if not _CONSTRUCTION_KEY.search(k))
    return [text.title, text.seller_category, *values]


def ranked_not_listed(result: ClassifyResult) -> bool:
    """The model's best answer is the `not_listed` option."""
    ranked = sorted(result.candidates, key=lambda c: -c.confidence)
    return bool(ranked) and ranked[0].category == NOT_LISTED


def assess(raw: ClassifyResult, screened: ClassifyResult, text: ProductText) -> Honesty:
    """`not_listed` from the raw answer; `looks_like` when the seller's words name a group and
    the top screened candidate (if any) sits in another known group."""
    not_listed = ranked_not_listed(raw)
    groups = word_groups(_seller_words(text))
    top = screened.candidates[0].category if screened.candidates and not not_listed else None
    top_group = category_group(top) if top is not None else None
    looks_like = None
    # a top candidate of no known group ("other") is never contradicted: there is nothing to compare
    if groups and top_group != "other" and top_group not in groups:
        looks_like = groups[0]
    return Honesty(not_listed=not_listed, looks_like=looks_like)
