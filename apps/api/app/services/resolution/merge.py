"""`merge_targets` (SI11 T16; ADR-0028 proposed; P:B5 part 2, D17): the AI's single-field
rewrite keeps only the target paths of the model's result; every other path is the base
version's, whatever the model returned. Pure (no I/O).

`mergeable(path)` names the paths a target may be: `content.title`, `content.description`,
`content.bullets` or one `content.bullets[<i>]`, `keywords.search_terms`, `keywords.hidden_terms`
and `attributes.<key>`. Any other path is never a target (the planner gives it no `ai_field`).

Client feedback 14: a seller's section regenerate may also target `attributes`, the whole map
(`services/sections.py`); it is never a planner target, so `mergeable` stays False for it.

Client feedback 11 (schemas 2.1.0): `keyword_reasons` follow the keywords. A term keeps the reason
of the side its keyword list came from (`result` when a `keywords.*` path is a target, else
`base`), then the other side's; `used_in` is recomputed on the merged copy
(`services/keyword_reasons.py`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

from mkos_schemas.listing import CanonicalListing

from app.services.keyword_reasons import refresh_keyword_reasons

_PLAIN: Final = frozenset(
    {
        "content.title",
        "content.description",
        "content.bullets",
        "keywords.search_terms",
        "keywords.hidden_terms",
    }
)
_MAP: Final = "attributes"
_BULLET: Final = re.compile(r"content\.bullets\[(\d{1,2})\]")
_ATTRIBUTE: Final = re.compile(r"attributes\.([a-z0-9_]{1,64})")


def mergeable(path: str) -> bool:
    return path in _PLAIN or bool(_BULLET.fullmatch(path) or _ATTRIBUTE.fullmatch(path))


def merge_targets(
    base: CanonicalListing, result: CanonicalListing, targets: Sequence[str]
) -> CanonicalListing:
    """`base` with each target path taken from `result` (a path `result` lacks is removed for an
    attribute, kept from `base` for a bullet index it does not have)."""
    if not targets:
        return base
    out: dict[str, Any] = base.model_dump(mode="python")
    new: dict[str, Any] = result.model_dump(mode="python")
    for path in targets:
        if path == _MAP:  # a section regenerate's whole attribute map
            out["attributes"] = new["attributes"]
            continue
        if path in _PLAIN:
            root, name = path.split(".", 1)
            out[root][name] = new[root][name]
            continue
        bullet = _BULLET.fullmatch(path)
        if bullet is not None:
            index = int(bullet.group(1))
            mine, theirs = list(out["content"]["bullets"]), new["content"]["bullets"]
            if index < len(mine) and index < len(theirs):
                mine[index] = theirs[index]
                out["content"]["bullets"] = mine
            continue
        attribute = _ATTRIBUTE.fullmatch(path)
        if attribute is not None:
            key = attribute.group(1)
            if key in new["attributes"]:
                out["attributes"][key] = new["attributes"][key]
            else:
                out["attributes"].pop(key, None)
    out["keyword_reasons"] = []
    first, second = (
        (result, base) if any(p.startswith("keywords.") for p in targets) else (base, result)
    )
    return refresh_keyword_reasons(CanonicalListing.model_validate(out), (first, second))
