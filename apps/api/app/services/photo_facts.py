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
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any, Final
from uuid import UUID

from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.jobs import CategoryTemplate, ContextImage, PhotoObservation, PhotoTarget

from app.core.resolved_facts import ProductFacts, ResolvedFact, is_unconfirmed_ai

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
    "PhotoDecision",
    "PhotoSet",
    "PhotoTask",
    "PhotoWrite",
    "allowed_for",
    "build_targets",
    "canonical",
    "fold",
    "photo_set",
    "same_as_rejected",
    "screen",
    "template_spelling",
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
# the most pieces a seller may answer to a piece-count question (tasks_answer)
ANSWER_MAX_PIECES: Final = 50

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


def _spellings(key: str, template: CategoryTemplate) -> list[str] | None:
    """The template's accepted spellings for `key`: its `allowed_values`, then its `value_map`
    keys (sorted), the list the Rules check accepts (`rules_engine.template.template_allowed`,
    inlined: this module imports no Rules code); None when the spec has neither."""
    spec = template.specs.get(key)
    if spec is None or (spec.allowed_values is None and not spec.value_map):
        return None
    return [*(spec.allowed_values or []), *sorted(spec.value_map)]


def allowed_for(key: str, template: CategoryTemplate) -> tuple[str, ...]:
    """The `VOCAB[key]` values this listing's marketplace enum can hold, in vocabulary space
    (SI9 CP-D16): every VOCAB value some template spelling means. A spec with neither
    `allowed_values` nor a `value_map` (or no spec) allows the whole vocabulary."""
    spellings = _spellings(key, template)
    if spellings is None:
        return VOCAB[key]
    mapped = {canonical(key, value) for value in spellings}
    return tuple(value for value in VOCAB[key] if value in mapped)


def template_spelling(key: str, value: str, template: CategoryTemplate) -> str:
    """SI9 CP-D25: the template's own spelling of the VOCAB `value` (the first `allowed_values`
    entry that means it, else the first sorted `value_map` key), so the Rules check accepts it as
    written; `value` itself without an enum spec."""
    for spelling in _spellings(key, template) or []:
        if canonical(key, spelling) == value:
            return spelling
    return value


def _template_keys(template: CategoryTemplate) -> set[str]:
    return {
        *template.required_attributes,
        *template.optional_attributes,
        *template.conditional_attributes,
        *template.specs,
    }


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
        if not allowed_for(key, template):
            continue  # a listed enum no vocabulary value fits
        stored = facts.attributes.get(key)
        fill = key not in CHECK_ONLY and (stored is None or is_unconfirmed_ai(stored))
        if not fill and key not in listed:
            continue  # SI9 D9: a key the template does not list is read as a copy hint only
        targets.append(
            PhotoTarget(
                key=key,
                mode="fill" if fill else "check",
                # SI9 CP-D16: the pipeline reads in vocabulary space; the backend screens
                allowed_values=list(VOCAB[key]),
                min_confidence=FLOORS[key],
            )
        )
    return targets


# ---- screening (T7, T8) ----

# Plain words for the question and reason templates; never pipeline text.
LABELS: Final[Mapping[str, str]] = {
    "neckline": "neckline",
    "sleeve_length": "sleeve length",
    "fit": "fit",
    "garment_length": "length",
    "embroidery": "embroidery",
    "occasion": "occasion",
    "color": "colour",
    "pattern": "pattern",
    "number_of_pieces": "number of pieces",
}
MAX_SUGGESTIONS: Final = 3
_TASK_ORDER: Final = {"pieces": 0, "conflicting_facts": 1, "missing_fact": 2}


@dataclass(frozen=True)
class PhotoWrite:
    """One photo value to store as an unconfirmed `ai` fact."""

    key: str
    value: str
    confidence: float
    image_id: UUID


@dataclass(frozen=True)
class PhotoTask:
    """One question for the seller (`human_tasks`, a fact kind)."""

    kind: str
    key: str
    question: str
    reason: str
    suggestions: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    expected_answer: dict[str, Any]
    image_id: UUID

    @property
    def target_path(self) -> str:
        return f"facts.attributes.{self.key}"


@dataclass(frozen=True)
class PhotoDecision:
    """What the backend keeps from one result. `dropped` holds `(key or "-", reason)`, never a
    value, so it is safe to log."""

    run_id: UUID | None
    writes: tuple[PhotoWrite, ...] = ()
    tasks: tuple[PhotoTask, ...] = ()
    dropped: tuple[tuple[str, str], ...] = ()


def _photo_run(records: Sequence[AgentRunRecord]) -> UUID | None:
    for rec in reversed(records):
        if (
            rec.agent_name == "image"
            and rec.trigger == "vision"
            and rec.status == "completed"
            and (rec.prompt_version or "").startswith(PHOTO_PROMPT_PREFIX)
        ):
            return rec.id
    return None


def _log_key(key: str, targets: Mapping[str, PhotoTarget]) -> str:
    """Only a key the backend itself knows is logged; anything else is "-"."""
    return key if key in targets or key in VOCAB or key in FORBIDDEN_KEYS else "-"


def _allowed_entry(value: str, target: PhotoTarget) -> str | None:
    folded = fold(value)
    hits = [entry for entry in target.allowed_values if fold(entry) == folded]
    return hits[0] if len(hits) == 1 else None


def _ai_suggestion(value: str, image_id: UUID) -> dict[str, Any]:
    return {"value": value, "label": value, "source": "ai", "evidence_ref": f"image:{image_id}"}


def _stored_suggestion(fact: ResolvedFact) -> dict[str, Any]:
    value = fact.resolved
    out: dict[str, Any] = {"value": value, "label": str(value)[:120], "source": fact.source}
    if fact.evidence_ref:
        out["evidence_ref"] = fact.evidence_ref
    return out


def _evidence(text: str, image_id: UUID) -> tuple[dict[str, Any], ...]:
    return ({"kind": "source_value", "text": f"photos: {text}", "ref": f"image:{image_id}"},)


def _expected(key: str, template: CategoryTemplate) -> dict[str, Any]:
    """The task's AnswerSpec (DATABASE.md:1411): a piece count is a number, any other key one of
    the template's accepted values in its own spellings (SI9 CP-D23), so an answer can never fail
    the Rules check's `VALUE_NOT_ALLOWED`."""
    if key == "number_of_pieces":
        return {"kind": "number", "min": 1, "max": ANSWER_MAX_PIECES}
    spelled = dict.fromkeys(template_spelling(key, v, template) for v in allowed_for(key, template))
    return {"kind": "enum", "allowed_values": list(spelled)}


def _missing(
    key: str, value: str, obs: PhotoObservation, target: PhotoTarget, template: CategoryTemplate
) -> PhotoTask:
    allowed = allowed_for(key, template)
    shown = template_spelling(key, value, template)
    suggestions: list[dict[str, Any]] = [_ai_suggestion(shown, obs.image_id)]
    for alt in obs.alternatives:
        entry = _allowed_entry(alt.value, target)
        if entry is None or entry not in allowed:
            continue  # outside the template subset (SI9 CP-D17)
        spelled = template_spelling(key, entry, template)
        if all(s["value"] != spelled for s in suggestions):
            suggestions.append(_ai_suggestion(spelled, obs.image_id))
    label = LABELS[key]
    return PhotoTask(
        kind="missing_fact",
        key=key,
        question=f"What is the {label} of this product?",
        reason=f"The photos suggest {value}, but not clearly enough to use it.",
        suggestions=tuple(suggestions[:MAX_SUGGESTIONS]),
        evidence=_evidence(value, obs.image_id),
        expected_answer=_expected(key, template),
        image_id=obs.image_id,
    )


def _conflict(
    key: str,
    value: str,
    stored: ResolvedFact,
    said: str,
    obs: PhotoObservation,
    template: CategoryTemplate,
) -> PhotoTask:
    label = LABELS[key]
    return PhotoTask(
        kind="conflicting_facts",
        key=key,
        question=f"Which {label} is right for this product?",
        reason=f"The photos suggest {value}, but the product says {said}.",
        suggestions=(
            _stored_suggestion(stored),
            _ai_suggestion(template_spelling(key, value, template), obs.image_id),
        ),
        evidence=_evidence(value, obs.image_id),
        expected_answer=_expected(key, template),
        image_id=obs.image_id,
    )


def _pieces(value: str, stored: ResolvedFact | None, obs: PhotoObservation) -> PhotoTask | None:
    """A piece count is critical: never written, never an `ai` suggestion (C:46-47)."""
    if stored is None:
        if obs.confidence < PIECES_ASK_FLOOR:
            return None
        return PhotoTask(
            kind="missing_fact",
            key="number_of_pieces",
            question="How many pieces are in this set?",
            reason=f"The photos seem to show {value} pieces. Please give the number of pieces.",
            suggestions=(),
            evidence=_evidence(value, obs.image_id),
            expected_answer={"kind": "number", "min": 1, "max": ANSWER_MAX_PIECES},
            image_id=obs.image_id,
        )
    said = canonical("number_of_pieces", stored.resolved)
    if said is None or said == value or obs.confidence < CONFLICT_FLOOR:
        return None
    return PhotoTask(
        kind="conflicting_facts",
        key="number_of_pieces",
        question="How many pieces are in this set?",
        reason=f"The photos seem to show {value} pieces, but the product says {said}.",
        suggestions=(_stored_suggestion(stored),),
        evidence=_evidence(value, obs.image_id),
        expected_answer={"kind": "number", "min": 1, "max": ANSWER_MAX_PIECES},
        image_id=obs.image_id,
    )


def _rank(task: PhotoTask) -> tuple[int, int]:
    group = "pieces" if task.key in CHECK_ONLY else task.kind
    return (_TASK_ORDER[group], V1_KEYS.index(task.key))


def same_as_rejected(key: str, value: str, marker: str) -> bool:
    """SI9 B5 (CP-D27): `value` is the value the seller rejected for `key`, compared in vocabulary
    space (the marker may hold a template spelling such as `Regular`); a marker `canonical`
    cannot read is compared by `fold` only."""
    said = canonical(key, marker)
    if said is not None:
        return said == (canonical(key, value) or value)
    return fold(marker) == fold(value)


def screen(
    observations: Sequence[PhotoObservation],
    records: Sequence[AgentRunRecord],
    *,
    targets: Sequence[PhotoTarget],
    images: Sequence[ContextImage],
    facts: ProductFacts,
    required: frozenset[str],
    rejected: Mapping[str, str],
    template: CategoryTemplate,
) -> PhotoDecision:
    """Keep only what the module docstring allows; everything else lands in `dropped`. SI9: an
    observation equal to the key's rejected value (`rejected`, `products.photo_rejections`) is
    dropped `rejected_before` before any write, question or conflict."""
    by_key = {t.key: t for t in targets}
    run_id = _photo_run(records)
    if run_id is None:
        return PhotoDecision(
            None, dropped=tuple((_log_key(o.key, by_key), "no_run") for o in observations)
        )
    photos = {image.image_id for image in images if image.slot in PHOTO_SLOTS}
    listed = _template_keys(template)
    counts = Counter(o.key for o in observations)
    dropped: list[tuple[str, str]] = []
    writes: list[PhotoWrite] = []
    tasks: list[PhotoTask] = []
    for obs in observations:
        target = by_key.get(obs.key)
        if target is None:
            dropped.append((_log_key(obs.key, by_key), "not_target"))
            continue
        if counts[obs.key] > 1:
            dropped.append((target.key, "duplicate_key"))
            continue
        entry = _allowed_entry(obs.value, target)
        if entry is None:
            dropped.append((target.key, "not_allowed"))
            continue
        if obs.image_id not in photos:
            dropped.append((target.key, "bad_image"))
            continue
        key = target.key
        if key not in CHECK_ONLY and entry not in allowed_for(key, template):
            dropped.append((key, "not_in_template"))  # SI9 CP-D17: never forced to a near value
            continue
        marker = rejected.get(key)
        if marker is not None and same_as_rejected(key, entry, marker):
            dropped.append((key, "rejected_before"))
            continue
        stored = facts.attributes.get(key)
        if key in CHECK_ONLY:
            task = _pieces(entry, stored, obs)
            if task is not None:
                tasks.append(task)
            continue
        if target.mode == "fill":
            if obs.confidence >= target.min_confidence:
                # SI9 CP-D25: written in the template's own spelling
                spelled = template_spelling(key, entry, template)
                writes.append(PhotoWrite(key, spelled, round(obs.confidence, 2), obs.image_id))
            elif obs.confidence >= ASK_FLOOR and key in required and stored is None:
                tasks.append(_missing(key, entry, obs, target, template))
            continue
        if key not in listed:
            continue  # SI9 D9: no question for a key the template does not list
        said = None if stored is None else canonical(key, stored.resolved)
        if (
            stored is not None
            and said is not None
            and said != entry
            and obs.confidence >= CONFLICT_FLOOR
        ):
            tasks.append(_conflict(key, entry, stored, said, obs, template))
    tasks.sort(key=_rank)
    return PhotoDecision(
        run_id,
        writes=tuple(writes),
        tasks=tuple(tasks[:MAX_PHOTO_TASKS]),
        dropped=tuple(dropped),
    )
