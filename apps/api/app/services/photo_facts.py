"""Listing details read from the product photos (SI3): which keys to ask about, and what to keep.

Pure: imports only `mkos_schemas` and `app.core`, no I/O, no clock. The worker calls it twice:

- `build_targets` before the pipeline: the `PhotoTarget`s for `GenerationContext.photo_targets`.
  Only non-critical visual keys the dictionary lets `ai` fill, only for an apparel template, only
  for a standalone or style product, and only when the photo set changed since the last photo run
  (the skip rule compares `(image_id, clean_content_hash)` sets, never clocks).
- `screen` after a trusted result: the pipeline's observations are suggestions. Each one is kept
  only when it names a target, equals one of its allowed values, cites a product photo of this
  context, and is the only observation for its key. A confident `fill` becomes an unconfirmed `ai`
  fact; anything else can only become a question for the seller (at most `MAX_PHOTO_TASKS`).

A photo value never overrides a seller, document or PIM value: a stored value makes the key a
`check`, and a `check` can only open a `conflicting_facts` question.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Final
from uuid import UUID

from mkos_schemas.jobs import CategoryTemplate, ContextImage, PhotoTarget

from app.core.resolved_facts import ProductFacts, ResolvedFact

__all__ = [
    "ASK_FLOOR",
    "CONFLICT_FLOOR",
    "FORBIDDEN_KEYS",
    "MAX_PHOTO_TASKS",
    "PHOTO_PROMPT_PREFIX",
    "PHOTO_SLOTS",
    "PIECES_ASK_FLOOR",
    "SYNONYMS",
    "V1_KEYS",
    "VOCAB",
    "PhotoSet",
    "allowed_for",
    "build_targets",
    "canonical",
    "fold",
    "photo_set",
]

V1_KEYS: Final = (
    "neckline",
    "sleeve_length",
    "fit",
    "garment_length",
    "embroidery",
    "occasion",
    "color",
    "pattern",
    "number_of_pieces",
)
# A style's colour and pattern belong to its colourways, not to the style's photos as a whole.
STANDALONE_ONLY: Final = frozenset({"color", "pattern"})
# Critical: never written from a photo, only a question.
CHECK_ONLY: Final = frozenset({"number_of_pieces"})
# A template listing none of these is not apparel (lamps): no targets, no vision call.
APPAREL_SIGNAL: Final = frozenset({"neckline", "sleeve_length", "fit"})
# Product photos only: a packaging label or a size chart is never cited.
PHOTO_SLOTS: Final = frozenset({"main", "secondary", "lifestyle"})
PHOTO_PROMPT_PREFIX: Final = "photo_facts/"
FLOORS: Final[Mapping[str, float]] = dict.fromkeys(V1_KEYS, 0.70)
ASK_FLOOR: Final = 0.40
CONFLICT_FLOOR: Final = 0.90
PIECES_ASK_FLOOR: Final = 0.70
MAX_PHOTO_TASKS: Final = 3
MAX_PIECES: Final = 12

# Regulated, commercial or measured facts: never a photo target (C:38-39, C:75).
FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "mrp",
        "selling_price",
        "tax_rate",
        "hsn_code",
        "country_of_origin",
        "manufacturer",
        "marketer",
        "packer",
        "importer",
        "consumer_care",
        "fiber_composition",
        "fabric_type",
        "gsm",
        "wash_care",
        "iron_care",
        "certifications",
        "performance_claims",
        "size",
        "size_measurements",
        "net_quantity",
        "identifier",
        "package_dimensions",
        "package_weight",
        "product_weight",
        "product_dimensions",
        "set_composition",
        "lining",
        "generic_name",
    }
)

# The canonical lowercase values per key (PLAN D4: the AI lead reviews this draft). It covers the
# seeded Amazon and Flipkart template values (`db/seed_data/platform_templates.json`).
VOCAB: Final[Mapping[str, tuple[str, ...]]] = {
    "neckline": (
        "round neck",
        "crew neck",
        "v-neck",
        "u-neck",
        "boat neck",
        "square neck",
        "sweetheart neck",
        "high neck",
        "mandarin collar",
        "collared neck",
        "polo collar",
        "keyhole neck",
        "off-shoulder",
        "halter neck",
        "cowl neck",
        "scoop neck",
    ),
    "sleeve_length": (
        "sleeveless",
        "cap sleeve",
        "short sleeve",
        "elbow sleeve",
        "three-quarter sleeve",
        "full sleeve",
    ),
    "fit": ("regular fit", "slim fit", "relaxed fit", "loose fit", "oversized"),
    "garment_length": (
        "crop",
        "waist length",
        "hip length",
        "thigh length",
        "knee length",
        "calf length",
        "ankle length",
        "floor length",
    ),
    "embroidery": (
        "none",
        "thread work",
        "zari work",
        "sequin work",
        "mirror work",
        "chikankari",
        "gota patti",
        "bead work",
        "resham work",
    ),
    "occasion": ("casual", "daily wear", "work", "formal", "party", "festive", "wedding", "lounge"),
    "color": (
        "black",
        "white",
        "off white",
        "cream",
        "beige",
        "brown",
        "grey",
        "navy blue",
        "blue",
        "sky blue",
        "indigo",
        "teal",
        "turquoise",
        "green",
        "olive",
        "yellow",
        "mustard",
        "orange",
        "peach",
        "pink",
        "magenta",
        "red",
        "maroon",
        "purple",
        "lavender",
        "gold",
        "silver",
        "multicolour",
    ),
    "pattern": (
        "solid",
        "printed",
        "floral",
        "striped",
        "checked",
        "geometric",
        "abstract",
        "paisley",
        "polka dot",
        "ethnic motif",
        "embroidered",
        "self design",
        "colour block",
        "tie and dye",
        "bandhani",
        "ikat",
        "animal print",
    ),
    "number_of_pieces": tuple(str(n) for n in range(1, MAX_PIECES + 1)),
}

# Folded seller or marketplace text -> canonical value. Includes every seeded template `value_map`
# key that has a canonical equivalent.
SYNONYMS: Final[Mapping[str, Mapping[str, str]]] = {
    "neckline": {
        "round": "round neck",
        "crew": "crew neck",
        "vneck": "v-neck",
        "u neck": "u-neck",
        "boat": "boat neck",
        "bateau neck": "boat neck",
        "square": "square neck",
        "sweetheart": "sweetheart neck",
        "high": "high neck",
        "mandarin": "mandarin collar",
        "chinese collar": "mandarin collar",
        "band collar": "mandarin collar",
        "collar": "collared neck",
        "collared": "collared neck",
        "shirt collar": "collared neck",
        "polo": "polo collar",
        "polo neck": "polo collar",
        "keyhole": "keyhole neck",
        "off shoulder neck": "off-shoulder",
        "halter": "halter neck",
        "cowl": "cowl neck",
        "scoop": "scoop neck",
    },
    "sleeve_length": {
        "sleeve less": "sleeveless",
        "cap": "cap sleeve",
        "cap sleeves": "cap sleeve",
        "short": "short sleeve",
        "short sleeves": "short sleeve",
        "half": "short sleeve",
        "half sleeve": "short sleeve",
        "half sleeves": "short sleeve",
        "elbow": "elbow sleeve",
        "elbow sleeves": "elbow sleeve",
        "elbow length sleeve": "elbow sleeve",
        "3/4": "three-quarter sleeve",
        "3/4 sleeve": "three-quarter sleeve",
        "3/4 sleeves": "three-quarter sleeve",
        "3/4th sleeve": "three-quarter sleeve",
        "3/4th sleeves": "three-quarter sleeve",
        "three quarter": "three-quarter sleeve",
        "three quarter sleeves": "three-quarter sleeve",
        "long": "full sleeve",
        "long sleeve": "full sleeve",
        "long sleeves": "full sleeve",
        "full": "full sleeve",
        "full sleeves": "full sleeve",
    },
    "fit": {
        "regular": "regular fit",
        "slim": "slim fit",
        "relaxed": "relaxed fit",
        "loose": "loose fit",
        "oversize": "oversized",
        "oversized fit": "oversized",
    },
    "garment_length": {
        "cropped": "crop",
        "waist": "waist length",
        "hip": "hip length",
        "thigh": "thigh length",
        "knee": "knee length",
        "calf": "calf length",
        "ankle": "ankle length",
        "floor": "floor length",
    },
    "embroidery": {"no embroidery": "none", "zari": "zari work", "mirror": "mirror work"},
    "occasion": {
        "daily": "daily wear",
        "office": "work",
        "office wear": "work",
        "festival": "festive",
    },
    "color": {
        "navy": "navy blue",
        "dark blue": "navy blue",
        "gray": "grey",
        "offwhite": "off white",
        "wine": "maroon",
        "burgundy": "maroon",
        "golden": "gold",
        "multi": "multicolour",
        "multicolor": "multicolour",
        "multi colour": "multicolour",
        "multi color": "multicolour",
        "violet": "purple",
    },
    "pattern": {
        "plain": "solid",
        "print": "printed",
        "stripes": "striped",
        "stripe": "striped",
        "checks": "checked",
        "check": "checked",
        "polka dots": "polka dot",
        "tie dye": "tie and dye",
        "tie & dye": "tie and dye",
        "colourblock": "colour block",
        "color block": "colour block",
    },
}

PhotoSet = tuple[tuple[str, str], ...]


# ---- text ----


def fold(text: str) -> str:
    """NFKC, casefold, every Unicode dash and `_` as a space, whitespace collapsed and stripped."""
    normal = unicodedata.normalize("NFKC", text).casefold()
    chars = (
        " " if ch == "_" or unicodedata.category(ch) == "Pd" or ch == "\N{MINUS SIGN}" else ch
        for ch in normal
    )
    return " ".join("".join(chars).split())


_FOLDED_VOCAB: Final[Mapping[str, Mapping[str, str]]] = {
    key: {fold(value): value for value in values} for key, values in VOCAB.items()
}
_FOLDED_SYNONYMS: Final[Mapping[str, Mapping[str, str]]] = {
    key: {fold(text): value for text, value in table.items()} for key, table in SYNONYMS.items()
}


def canonical(key: str, value: object) -> str | None:
    """The `VOCAB[key]` member a stored value means, or None (unknown, blank or the wrong type)."""
    if key not in VOCAB:
        return None
    if key == "number_of_pieces" and isinstance(value, int) and not isinstance(value, bool):
        return str(value) if 1 <= value <= MAX_PIECES else None
    if not isinstance(value, str):
        return None
    folded = fold(value)
    if not folded:
        return None
    hit = _FOLDED_VOCAB[key].get(folded)
    if hit is not None:
        return hit
    return _FOLDED_SYNONYMS.get(key, {}).get(folded)


# ---- the dictionary cross-check (import time) ----


def _check_dictionary() -> None:
    text = (
        resources.files("mkos_schemas")
        .joinpath("data/attribute_dictionary.json")
        .read_text(encoding="utf-8")
    )
    entries = {entry["key"]: entry for entry in json.loads(text)["attributes"]}
    for key in V1_KEYS:
        entry = entries.get(key)
        if entry is None:
            raise RuntimeError(f"photo key {key!r} is not in the attribute dictionary")
        if key in CHECK_ONLY:
            continue
        if key in FORBIDDEN_KEYS or entry["is_critical"] or "ai" not in entry["allowed_sources"]:
            raise RuntimeError(f"photo key {key!r} is critical or does not allow source 'ai'")


_check_dictionary()


# ---- targets ----


def photo_set(images: Sequence[ContextImage], hashes: Mapping[UUID, str]) -> PhotoSet:
    """The sorted `(image_id, clean_content_hash)` pairs of the product photos in the context."""
    return tuple(
        sorted(
            (str(image.image_id), hashes.get(image.image_id, ""))
            for image in images
            if image.slot in PHOTO_SLOTS
        )
    )


def allowed_for(key: str, template: CategoryTemplate) -> tuple[str, ...]:
    """The `VOCAB[key]` values this listing's marketplace enum can hold (CP-D18), in VOCAB order.

    A spec without `allowed_values` (or no spec) allows the whole vocabulary."""
    spec = template.specs.get(key)
    if spec is None or spec.allowed_values is None:
        return VOCAB[key]
    mapped = {canonical(key, value) for value in spec.allowed_values}
    return tuple(value for value in VOCAB[key] if value in mapped)


def _template_keys(template: CategoryTemplate) -> set[str]:
    return {
        *template.required_attributes,
        *template.optional_attributes,
        *template.conditional_attributes,
        *template.specs,
    }


def is_unconfirmed_ai(fact: ResolvedFact | None) -> bool:
    return fact is not None and fact.source == "ai" and fact.confirmed_at is None


def build_targets(
    facts: ProductFacts,
    template: CategoryTemplate,
    images: Sequence[ContextImage],
    *,
    current_set: PhotoSet,
    last_set: PhotoSet | None,
) -> list[PhotoTarget]:
    """The keys to read from the photos for this listing (module docstring); `[]` means no call."""
    listed = _template_keys(template)
    if (
        facts.level == "variant"
        or not (listed & APPAREL_SIGNAL)
        or not any(image.slot in PHOTO_SLOTS for image in images)
        or not current_set
        or last_set == current_set
    ):
        return []
    targets: list[PhotoTarget] = []
    for key in V1_KEYS:
        if key in STANDALONE_ONLY and facts.level != "standalone":
            continue
        if key == "number_of_pieces" and key not in listed:
            continue
        allowed = allowed_for(key, template)
        if not allowed:
            continue
        stored = facts.attributes.get(key)
        fill = key not in CHECK_ONLY and (stored is None or is_unconfirmed_ai(stored))
        targets.append(
            PhotoTarget(
                key=key,
                mode="fill" if fill else "check",
                allowed_values=list(allowed),
                min_confidence=FLOORS[key],
            )
        )
    return targets
