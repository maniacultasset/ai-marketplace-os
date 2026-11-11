"""Listing sections, field locks and the answer dependency map (client feedback 14 and 16, 11 Nov
2026). Pure (no I/O).

A section is one card on Review: `title`, `bullets`, `description`, `keywords` or `attributes`.
Each owns the canonical paths in `SECTION_PATHS`. A section regenerate (`POST
/listings/{id}/regenerate` with `sections`) rewrites only those paths: the worker merges the
model's result with `merge_targets`, so every other path stays the base version's.

Field locks: a path the seller edited (`PATCH /listings/{id}/content`) is locked
(`edited_paths`): `content.title`, `content.bullets`, `content.description`,
`keywords.search_terms`, `keywords.hidden_terms` or one `attributes.<key>`. No regenerate (whole
listing, a section, or the resolution engine's AI rewrite) changes a locked path: the worker copies
it back from the base version (`keep_locked`). The seller unlocks a path on the same PATCH
(`unlock`). The locks live on the listing (`listings.locked_paths`), so they carry across versions.

Answers: `affected_sections` maps the fact paths the seller answered (`facts.attributes.<key>` or
`facts.<key>`) to the sections that use them, from a fixed table (`fact_sections`), never a model
call. A section whose every path is locked is left out: regenerating it would change nothing.
`stale_sections` keeps a section only while an answer to a fact it uses is newer than the last AI
run that wrote it (`refreshes`: a fresh generation, or a regeneration's model runs with their
scope), so a seller's edit, a fact-only version or a regenerate of another section never hides
it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Final, Literal, get_args

from mkos_schemas.listing import CanonicalListing

from app.services.resolution.merge import merge_targets

Section = Literal["title", "bullets", "description", "keywords", "attributes"]
SECTIONS: Final[tuple[Section, ...]] = get_args(Section)
ATTRIBUTE_MAP: Final = "attributes"
SECTION_PATHS: Final[Mapping[Section, tuple[str, ...]]] = {
    "title": ("content.title",),
    "bullets": ("content.bullets",),
    "description": ("content.description",),
    "keywords": ("keywords.search_terms", "keywords.hidden_terms"),
    # the whole map; a locked key inside it is restored by `keep_locked`
    "attributes": (ATTRIBUTE_MAP,),
}
PLAIN_LOCKS: Final = (
    "content.title",
    "content.bullets",
    "content.description",
    "keywords.search_terms",
    "keywords.hidden_terms",
)
LOCK_PATTERN: Final = (
    r"^(content\.(title|bullets|description)|keywords\.(search_terms|hidden_terms)"
    r"|attributes\.[a-z0-9_]{1,64})$"
)
MAX_LOCKS: Final = 256  # `ck_listings_locked_paths`
_LOCK: Final = re.compile(LOCK_PATTERN)
_ATTRIBUTE: Final = re.compile(r"attributes\.([a-z0-9_]{1,64})")
_FACT: Final = re.compile(r"facts\.(?:attributes\.)?([a-z0-9_]{1,64})")

# The facts a title is built from: an answer to one of them can change every section.
TITLE_FACTS: Final = frozenset(
    {
        "brand_name",
        "title",
        "product_type",
        "generic_name",
        "gender",
        "age_group",
        "color",
        "size",
        "fabric_type",
        "fiber_composition",
        "pattern",
        "net_quantity",
        "number_of_pieces",
        "set_composition",
        "kurta_type",
        "dress_type",
        "style_number",
    }
)
# Compliance, price, identity and shipping facts: they reach the listing as attribute values only,
# never its text.
ATTRIBUTE_FACTS: Final = frozenset(
    {
        "mrp",
        "selling_price",
        "tax_rate",
        "hsn_code",
        "country_of_origin",
        "identifier",
        "package_dimensions",
        "package_weight",
        "product_weight",
        "manufacturer",
        "marketer",
        "packer",
        "importer",
        "consumer_care",
        "stock_ref",
        "size_chart_url",
    }
)
# Any other fact (care, fit, sleeve, occasion, ...) is described in the text and searched for.
DESCRIBED: Final[tuple[Section, ...]] = ("bullets", "description", "keywords", "attributes")


def lockable(path: str) -> bool:
    return bool(_LOCK.fullmatch(path))


def _value(listing: CanonicalListing, path: str) -> object:
    root, name = path.split(".", 1)
    return getattr(getattr(listing, root), name)


def edited_paths(old: CanonicalListing, new: CanonicalListing) -> list[str]:
    """The lockable paths `new` changes: the plain ones in `PLAIN_LOCKS` order, then each
    attribute key added, removed or changed (sorted)."""
    paths = [p for p in PLAIN_LOCKS if _value(old, p) != _value(new, p)]
    keys = sorted(set(old.attributes) | set(new.attributes))
    paths += [f"attributes.{k}" for k in keys if old.attributes.get(k) != new.attributes.get(k)]
    return paths


def next_locks(
    current: Iterable[str], *, unlock: Iterable[str], edited: Iterable[str]
) -> list[str]:
    """The locks after a PATCH: the current ones less `unlock`, plus every path it edited (an
    edit in the same request wins over its unlock). Sorted, so equal sets compare equal."""
    return sorted((set(current) - set(unlock)) | set(edited))


def covered(path: str, locked: Iterable[str]) -> bool:
    """Whether a lock holds `path` (itself, or one bullet or term of a locked list)."""
    return any(path == p or path.startswith((f"{p}[", f"{p}.")) for p in locked)


def section_targets(sections: Iterable[Section], locked: Iterable[str]) -> tuple[str, ...]:
    """The paths a section regenerate rewrites, in `SECTIONS` order, less the locked plain paths
    (a locked attribute key inside `attributes` is restored by `keep_locked`). Empty when every
    path asked for is locked."""
    asked, held = set(sections), set(locked)
    return tuple(
        path
        for section in SECTIONS
        if section in asked
        for path in SECTION_PATHS[section]
        if path not in held
    )


def keep_locked(
    listing: CanonicalListing, base: CanonicalListing, locked: Sequence[str]
) -> CanonicalListing:
    """`listing` with every locked path copied back from `base` (a locked attribute `base` lacks
    is removed: the seller deleted it)."""
    paths = [p for p in locked if lockable(p)]
    return merge_targets(listing, base, paths) if paths else listing


def fact_key(target_path: str) -> str | None:
    """`facts.attributes.color` and `facts.color` -> `color`; any other target -> None."""
    found = _FACT.fullmatch(target_path)
    return None if found is None else found.group(1)


def fact_sections(key: str) -> tuple[Section, ...]:
    if key in ATTRIBUTE_FACTS:
        return ("attributes",)
    if key in TITLE_FACTS:
        return SECTIONS
    return DESCRIBED


def affected_sections(target_paths: Iterable[str], locked: Iterable[str] = ()) -> list[Section]:
    """The sections the answered facts feed, in `SECTIONS` order, less the fully locked ones."""
    hit: set[Section] = set()
    for path in target_paths:
        key = fact_key(path)
        if key is not None:
            hit.update(fact_sections(key))
    held = set(locked)
    return [s for s in SECTIONS if s in hit and section_targets((s,), held)]


def stale_sections(
    answers: Iterable[tuple[str, datetime]],
    refreshes: Iterable[tuple[datetime, Sequence[str] | None]],
    locked: Iterable[str] = (),
) -> list[Section]:
    """The sections an answer (`(target_path, answered_at)`) is newer than the last AI write of,
    in `SECTIONS` order, less the fully locked ones. `refreshes` are `(at, scope)` pairs: the
    paths an AI run rewrote at `at` (None: the whole listing)."""
    last: dict[Section, datetime] = {}
    for at, scope in refreshes:
        for section in SECTIONS:
            hit = scope is None or any(covered(p, SECTION_PATHS[section]) for p in scope)
            if hit and (section not in last or at > last[section]):
                last[section] = at
    stale: set[Section] = set()
    for path, at in answers:
        key = fact_key(path)
        if key is None:
            continue
        stale.update(s for s in fact_sections(key) if s not in last or at > last[s])
    held = set(locked)
    return [s for s in SECTIONS if s in stale and section_targets((s,), held)]
