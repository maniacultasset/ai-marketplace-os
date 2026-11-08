"""SI12 B6: where each field's value came from, for the source chips (pure, no I/O).

`field_sources(facts, inherited=...)` reads the effective Fact envelopes (own, then the style's;
for a typed column the caller adds the effective value under `value`) and answers one
`FieldSource` per key that has a value:

- `sheet`: a seller value from the CSV import (`captured_via import`);
- `size_chart`: the seller's size chart saved from a size-chart photo (`image:` evidence);
- `you`: any other seller value, or a document (`source document`);
- `photo`: a value read from a photo and not yet confirmed (`ai` with `image:` evidence), with
  the image and the confidence; `confirmed` once the seller confirmed it;
- `brand`: a value the brand default supplied (`inherited` flags).

Any other source (pim, marketplace, derived, system, an `ai` value without image evidence, or a
source this module does not know) gives no entry: the chip says nothing rather than guess.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = ["FieldSource", "field_sources"]

FieldSourceKind = Literal["sheet", "you", "photo", "confirmed", "size_chart", "brand"]
SIZE_CHART_KEY: Final = "size_measurements"
IMAGE_PREFIX: Final = "image:"
# `inherited` flag → the key whose value the brand default supplied
BRAND_FLAGS: Final[Mapping[str, str]] = {
    "manufacturer_inherited": "manufacturer",
    "brand_name_inherited": "brand_name",
    "country_of_origin_from_brand": "country_of_origin",
}


@dataclass(frozen=True, slots=True)
class FieldSource:
    kind: FieldSourceKind
    image_id: uuid.UUID | None = None
    confidence: float | None = None


def _image_id(ref: object) -> uuid.UUID | None:
    if not isinstance(ref, str) or not ref.startswith(IMAGE_PREFIX):
        return None
    try:
        return uuid.UUID(ref.removeprefix(IMAGE_PREFIX))
    except ValueError:
        return None


def _confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return float(value)


def _source_of(key: str, fact: Mapping[str, Any]) -> FieldSource | None:
    if fact.get("value") is None and fact.get("normalized") is None:
        return None
    source = fact.get("source")
    ref = fact.get("evidence_ref")
    from_image = isinstance(ref, str) and ref.startswith(IMAGE_PREFIX)
    if source == "seller":
        if fact.get("captured_via") == "import":
            return FieldSource(kind="sheet")
        if key == SIZE_CHART_KEY and from_image:
            return FieldSource(kind="size_chart", image_id=_image_id(ref))
        return FieldSource(kind="you")
    if source == "document":
        return FieldSource(kind="you")
    if source == "ai" and from_image:
        if fact.get("confirmed_at") is not None:
            return FieldSource(kind="confirmed", image_id=_image_id(ref))
        return FieldSource(
            kind="photo", image_id=_image_id(ref), confidence=_confidence(fact.get("confidence"))
        )
    return None


def field_sources(
    facts: Mapping[str, Any], *, inherited: Mapping[str, bool]
) -> dict[str, FieldSource]:
    out: dict[str, FieldSource] = {}
    for key, fact in facts.items():
        if not isinstance(fact, Mapping):
            continue
        found = _source_of(key, fact)
        if found is not None:
            out[key] = found
    for flag, key in BRAND_FLAGS.items():
        if inherited.get(flag):
            out[key] = FieldSource(kind="brand")
    return dict(sorted(out.items()))
