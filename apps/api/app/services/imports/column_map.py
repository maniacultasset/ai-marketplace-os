"""The spreadsheet column mapper (SI14, AC7): a seller's header row mapped onto the importer's
accepted header names before the upload, with no model. Pure: no I/O.

`TARGETS` is the one list of names a column may map to (CODE-PLAN Q-d): the A.6 and extension
names (`headers._KNOWN`) and the alias names (`ALIASES` keys), minus the export-only columns
(`IGNORED_WITH_WARNING`) and the bullet names. Every entry passes `check_header` as a lone column,
so a mapped header list is always one the importer accepts. A key is a header name, never a
`row_key` output.

`map_headers(headers)` runs one ladder per column on `normalize_header(h)`:
1. exact: the name is a target, a bullet name (`bullet_1` ... `bullet_99`, each its own column),
   or an export-only name (kept as itself, as `check_header` accepts it with a warning);
2. alias: `EXTENDED_ALIASES`, common seller spellings (separators and brackets folded);
3. fuzzy: `difflib` ratio on the separator-folded name against `TARGETS` (never a bullet name),
   accepted at 0.80 or more.
Two columns never share a key identity (`headers._column_identity`, so `package_weight` and a
grams alias clash as they do in `check_header`): the first exact or alias column keeps it, a
fuzzy claim never takes an identity an exact or alias column holds, and of two fuzzy claims the
higher ratio wins. A column left without a key is `none` and needs review. Exact and alias
mappings are 1.0 and need no review.

`apply_map(entries)` (SI14 T18) is the position-based renamer the upload and the import job both
apply, so the stored file stays the seller's original.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from app.services.imports.headers import (
    _KNOWN,
    ALIASES,
    IGNORED_WITH_WARNING,
    _column_identity,
    is_bullet,
    normalize_header,
    row_key,
)

__all__ = [
    "ALIASES_VERSION",
    "EXTENDED_ALIASES",
    "FUZZY_MIN",
    "TARGETS",
    "Mapped",
    "Renamer",
    "apply_map",
    "fold",
    "identity_of",
    "map_headers",
]

ALIASES_VERSION: Final = "2026-11-09.1"
FUZZY_MIN: Final = 0.80
Method = Literal["exact", "alias", "fuzzy", "ai", "none"]

TARGETS: Final[tuple[str, ...]] = tuple(
    sorted(
        name
        for name in (_KNOWN | frozenset(ALIASES))
        if name not in IGNORED_WITH_WARNING and not is_bullet(name)
    )
)
_TARGET_SET: Final = frozenset(TARGETS)
_SEPARATORS: Final = re.compile(r"[\s_\-.()\[\]/]+")


def fold(name: str) -> str:
    """The name with every separator run (space, `_`, `-`, `.`, brackets, `/`) as one space."""
    return _SEPARATORS.sub(" ", name).strip()


# Common seller spellings, keyed by their folded form; every value is a `TARGETS` entry
# (`tests/unit/test_column_map.py` checks it).
EXTENDED_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "sku": "seller_sku",
        "item sku": "seller_sku",
        "sku code": "seller_sku",
        "sku id": "seller_sku",
        "style code": "style_number",
        "style no": "style_number",
        "style number": "style_number",
        "product title": "product_name",
        "item name": "product_name",
        "product name": "product_name",
        "name": "product_name",
        "brand": "brand_name",
        "brand name": "brand_name",
        "colour": "color_name",
        "colour name": "color_name",
        "color": "color_name",
        "shade": "color_name",
        "size": "size_name",
        "fabric": "fabric_type",
        "material": "fabric_type",
        "composition": "fiber_composition",
        "mrp inr": "mrp_inr",
        "mrp rs": "mrp_inr",
        "max retail price": "mrp",
        "selling price": "selling_price",
        "sale price": "selling_price",
        "price": "selling_price",
        "hsn": "hsn_code",
        "hsn code": "hsn_code",
        "gst": "tax_rate",
        "gst rate": "tax_rate",
        "ean code": "ean",
        "upc code": "upc",
        "gtin code": "gtin",
        "barcode no": "barcode",
        "origin": "country_of_origin",
        "country": "country_of_origin",
        "made in": "country_of_origin",
        "weight grams": "weight_grams",
        "weight g": "weight_grams",
        "neck": "neckline",
        "sleeve": "sleeve_length",
        "parent sku": "parent_sku",
        "category name": "category",
        "sub category": "sub_category",
        "subcategory": "sub_category",
        "gender": "gender",
        "care": "wash_care",
        "wash instructions": "wash_care",
    }
)
_FOLDED_TARGETS: Final[tuple[tuple[str, str], ...]] = tuple((fold(t), t) for t in TARGETS)


@dataclass(frozen=True, slots=True)
class Mapped:
    """One column: its 1-based position, the raw header, the key (an accepted header name) or
    None, the rung that set it and whether a person must answer it."""

    position: int
    header: str
    key: str | None
    method: Method
    confidence: float
    needs_review: bool


def identity_of(key: str) -> str:
    """The column a key names, for the one-key rule (bullets are each their own column)."""
    if is_bullet(key):
        return key
    found = row_key(key)
    return _column_identity(found if found is not None else key)


def _exact(name: str) -> str | None:
    if name in _TARGET_SET or is_bullet(name) or name in IGNORED_WITH_WARNING:
        return name
    return None


def _fuzzy(name: str) -> tuple[str, float] | None:
    folded = fold(name)
    if not folded:
        return None
    best: tuple[str, float] | None = None
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(folded)
    for target_folded, target in _FOLDED_TARGETS:
        matcher.set_seq1(target_folded)
        if matcher.real_quick_ratio() < FUZZY_MIN or matcher.quick_ratio() < FUZZY_MIN:
            continue
        ratio = matcher.ratio()
        if ratio >= FUZZY_MIN and (best is None or ratio > best[1]):
            best = (target, ratio)
    return best


def _none(position: int, header: str) -> Mapped:
    return Mapped(position, header, None, "none", 0.0, True)


def map_headers(headers: Sequence[str]) -> list[Mapped]:
    out: list[Mapped | None] = [None] * len(headers)
    taken: dict[str, int] = {}
    fuzzy: dict[int, tuple[str, float]] = {}
    for index, raw in enumerate(headers):
        position = index + 1
        name = normalize_header(raw if isinstance(raw, str) else "")
        key = _exact(name)
        method: Method = "exact"
        if key is None:
            key = EXTENDED_ALIASES.get(fold(name))
            method = "alias"
        if key is None:
            found = _fuzzy(name)
            if found is not None:
                fuzzy[index] = found
            continue
        identity = identity_of(key)
        if identity in taken:
            out[index] = _none(position, raw)
            continue
        taken[identity] = index
        out[index] = Mapped(position, raw, key, method, 1.0, False)
    claims: dict[str, tuple[int, str, float]] = {}
    for index, (key, ratio) in fuzzy.items():
        identity = identity_of(key)
        if identity in taken:
            continue
        held = claims.get(identity)
        if held is None or ratio > held[2]:
            claims[identity] = (index, key, ratio)
    winners = {index: (key, ratio) for index, key, ratio in claims.values()}
    for index, raw in enumerate(headers):
        if out[index] is not None:
            continue
        if index in winners:
            key, ratio = winners[index]
            confidence = round(ratio, 4)
            out[index] = Mapped(index + 1, raw, key, "fuzzy", confidence, confidence < FUZZY_MIN)
        else:
            out[index] = _none(index + 1, raw)
    return [entry for entry in out if entry is not None]


@dataclass(frozen=True, slots=True)
class Renamer:
    """The confirmed map as a renamer from the original (normalised) header to the mapped one; a
    column mapped to None is dropped."""

    by_header: Mapping[str, str | None]
    order: tuple[str, ...]

    def header(self, columns: Sequence[str]) -> list[str]:
        """The mapped header list of `columns` (the file's own header row), nulls dropped."""
        mapped = [self.by_header.get(normalize_header(c)) for c in columns]
        return [name for name in mapped if name is not None]

    def row(self, raw: Mapping[str, str | None]) -> dict[str, str | None]:
        """One row's cells under the mapped names; a null-mapped or unknown column is dropped."""
        out: dict[str, str | None] = {}
        for column, value in raw.items():
            mapped = self.by_header.get(normalize_header(column))
            if mapped is not None:
                out[mapped] = value
        return out


def apply_map(headers: Sequence[str], entries: Sequence[Mapping[str, object]]) -> Renamer:
    """The renamer of a confirmed map: `headers` are the map's stored (normalised) headers in
    position order, `entries` its mappings (`position`, `key`)."""
    keys: dict[int, str | None] = {}
    for entry in entries:
        position = entry.get("position")
        key = entry.get("key")
        if isinstance(position, int):
            keys[position] = key if isinstance(key, str) else None
    by_header = {name: keys.get(index + 1) for index, name in enumerate(headers)}
    return Renamer(by_header=MappingProxyType(by_header), order=tuple(headers))
