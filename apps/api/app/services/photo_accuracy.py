"""Live photo accuracy (SI10 T18; PLAN D10, decision 12; V1-RELEASE-CHECKLIST.md:37). Pure.

Every seller review of a photo-read value is already one `product.fact_updated` audit row
(`app/services/product_edit.py`): `before.attributes[key]` is the stored envelope, `after` is the
seller's answer. A photo value is `source "ai"` with an `image:` evidence ref. Per key:
- `confirmed`: the same value came back with `confirmed_at`;
- `rejected`: the key was cleared (`null`);
- `changed`: any other answer (a seller value).
Rows that are malformed, keys whose `before` is not a photo value and keys absent from `after`
are skipped. No tenant, product or value leaves this module: only keys, counts and rates.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

Review = Literal["confirmed", "rejected", "changed"]
TWO: Decimal = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class KeyAccuracy:
    key: str
    confirmed: int
    rejected: int
    changed: int
    accept_rate: Decimal
    mean_confidence: Decimal | None


def _attributes(raw: object) -> Mapping[str, Any] | None:
    if isinstance(raw, str | bytes):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, Mapping):
        return None
    attributes = raw.get("attributes")
    return attributes if isinstance(attributes, Mapping) else None


def _is_photo(fact: object) -> bool:
    return (
        isinstance(fact, Mapping)
        and fact.get("source") == "ai"
        and isinstance(fact.get("evidence_ref"), str)
        and fact["evidence_ref"].startswith("image:")
    )


def _reviews(before: object, after: object) -> dict[str, tuple[Review, object]]:
    was, now = _attributes(before), _attributes(after)
    if was is None or now is None:
        return {}
    out: dict[str, tuple[Review, object]] = {}
    for key, fact in was.items():
        if not isinstance(key, str) or not _is_photo(fact) or key not in now:
            continue
        answer = now[key]
        if answer is None:
            review: Review = "rejected"
        elif (
            isinstance(answer, Mapping)
            and answer.get("confirmed_at") is not None
            and answer.get("value") == fact.get("value")
        ):
            review = "confirmed"
        else:
            review = "changed"
        out[key] = (review, fact.get("confidence"))
    return out


def classify_photo_reviews(before: object, after: object) -> dict[str, Review]:
    return {key: review for key, (review, _c) in _reviews(before, after).items()}


def _rate(part: int, total: int) -> Decimal:
    return (Decimal(part) / Decimal(total)).quantize(TWO, rounding=ROUND_HALF_UP)


def summarise(rows: Iterable[tuple[object, object]]) -> list[KeyAccuracy]:
    counts: dict[str, dict[Review, int]] = {}
    confidences: dict[str, list[Decimal]] = {}
    for before, after in rows:
        for key, (review, confidence) in _reviews(before, after).items():
            per = counts.setdefault(key, {"confirmed": 0, "rejected": 0, "changed": 0})
            per[review] += 1
            if isinstance(confidence, int | float) and not isinstance(confidence, bool):
                confidences.setdefault(key, []).append(Decimal(str(confidence)))
    out: list[KeyAccuracy] = []
    for key in sorted(counts):
        per = counts[key]
        total = sum(per.values())
        seen = confidences.get(key, [])
        mean = (
            (sum(seen, Decimal(0)) / len(seen)).quantize(TWO, rounding=ROUND_HALF_UP)
            if seen
            else None
        )
        out.append(
            KeyAccuracy(
                key,
                per["confirmed"],
                per["rejected"],
                per["changed"],
                _rate(per["confirmed"], total),
                mean,
            )
        )
    return out
