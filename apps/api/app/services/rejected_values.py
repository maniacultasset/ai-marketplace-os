"""The "facts changed, regenerate" check (SI9 B5; PLAN D12; CP-D14, CP-D26, CP-D27).

Pure and deterministic (the `photo-facts-are-pure` contract): it reads a listing and the
product's rejected photo values (`products.photo_rejections`, {key: value}) and gives one
`FACT_MISMATCH` item, source `rules_engine`, per field that still uses a rejected value. It runs
inside the Rules re-check, so an ERROR fails the run (approve stays refused) and a WARNING only
shows (`needs_review`, approve allowed).

- A marker's phrases are the marker and its vocabulary form `canonical(key, marker)`, deduped by
  `fold`. A phrase hits a text as a whole phrase on the folded text (NFKC, casefold, dashes and
  `_` as spaces): "v-neck" hits "V NECK" but never "v-necked".
- Longer-value rule: a hit that lies inside a hit of a longer vocabulary value (of any key) in the
  same text does not count: `work` in "zari work", `white` in "off white", `blue` in "navy blue".
- Fields: `attributes.<key>` (when it means the marker's value), `content.title`,
  `content.bullets[i]`, `content.description`, `keywords.search_terms[i]`. Severity: ERROR for an
  `attributes.<key>` hit and for a title hit of a non-generic marker (`GENERIC`); WARNING for
  every other hit. A field hit by several markers gets one item, ERROR when any hit is.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import MAX_ERROR_ITEMS, ErrorItem, ErrorReport
from mkos_schemas.listing import CanonicalListing

from app.services.photo_facts import LABELS, VOCAB, canonical, fold

# Values that occur in ordinary copy: a title hit only warns (None: every value of the key)
GENERIC: Final[Mapping[str, frozenset[str] | None]] = {
    "occasion": frozenset(VOCAB["occasion"]),
    "embroidery": frozenset({"none"}),
    "pattern": frozenset({"solid", "printed", "checked", "embroidered"}),
    "garment_length": frozenset({"crop"}),
    "number_of_pieces": None,
}
SUGGESTION: Final = "Regenerate the listing, or edit the text to remove it."
MESSAGE_MAX: Final = 1000
_ALL_VALUES: Final = tuple(sorted({fold(v) for values in VOCAB.values() for v in values}))

Span = tuple[int, int]


def _pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")


def phrase_spans(text: str, value: str) -> list[Span]:
    """The whole-phrase spans of `value` in `text`, both folded (spans on the folded text)."""
    phrase = fold(value)
    if not phrase:
        return []
    return [m.span() for m in _pattern(phrase).finditer(fold(text))]


def phrase_in(text: str, value: str) -> bool:
    return bool(phrase_spans(text, value))


def _phrases(key: str, marker: str) -> tuple[str, ...]:
    said = canonical(key, marker)
    return tuple(dict.fromkeys(p for p in (fold(marker), fold(said or "")) if p))


@lru_cache(maxsize=512)
def _longer(phrase: str, own: str) -> tuple[re.Pattern[str], ...]:
    """The vocabulary values that hold `phrase` as a whole phrase and are longer (never the
    marker's own vocabulary value)."""
    inner = _pattern(phrase)
    return tuple(
        _pattern(value)
        for value in _ALL_VALUES
        if value != phrase and value != own and inner.search(value)
    )


def _hits(text: str, key: str, marker: str) -> bool:
    folded = fold(text)
    own = fold(canonical(key, marker) or "")
    for phrase in _phrases(key, marker):
        covers = [m.span() for p in _longer(phrase, own) for m in p.finditer(folded)]
        for start, end in (m.span() for m in _pattern(phrase).finditer(folded)):
            if not any(s <= start and end <= e for s, e in covers):
                return True
    return False


def _generic(key: str, marker: str) -> bool:
    if key not in GENERIC:
        return False
    values = GENERIC[key]
    return values is None or (canonical(key, marker) or fold(marker)) in values


def _attribute_hit(listing: CanonicalListing, key: str, marker: str) -> bool:
    if key not in listing.attributes:
        return False
    stored = listing.attributes[key]
    said = canonical(key, marker)
    if said is not None:
        return canonical(key, stored) == said
    return isinstance(stored, str) and fold(stored) == fold(marker)


def _texts(listing: CanonicalListing) -> list[tuple[str, str, bool]]:
    """(path, text, is_title) of every checked text field."""
    out = [("content.title", listing.content.title, True)]
    out += [(f"content.bullets[{i}]", b, False) for i, b in enumerate(listing.content.bullets)]
    out.append(("content.description", listing.content.description, False))
    out += [
        (f"keywords.search_terms[{i}]", t, False)
        for i, t in enumerate(listing.keywords.search_terms)
    ]
    return out


def _item(path: str, hits: list[tuple[str, str]], severity: Severity) -> ErrorItem:
    parts = " and ".join(f"{marker} as the {LABELS[key]}" for key, marker in hits)
    tail = "it" if len(hits) == 1 else "them"
    message = f"You rejected {parts}, but this listing still uses {tail}. Regenerate the listing."
    return ErrorItem(
        field=path,
        error_code=ErrorCode.FACT_MISMATCH,
        severity=severity,
        message=message[:MESSAGE_MAX],
        value_submitted=", ".join(marker for _key, marker in hits),
        suggestion=SUGGESTION,
        source=ErrorSource.RULES_ENGINE,
        retryable=False,
    )


def rejected_value_items(
    listing: CanonicalListing, rejections: Mapping[str, str]
) -> list[ErrorItem]:
    found: dict[str, tuple[list[tuple[str, str]], bool]] = {}

    def hit(path: str, key: str, marker: str, error: bool) -> None:
        held, was_error = found.get(path, ([], False))
        found[path] = ([*held, (key, marker)], was_error or error)

    for key, marker in sorted(rejections.items()):
        if key not in LABELS or not marker:
            continue
        if _attribute_hit(listing, key, marker):
            hit(f"attributes.{key}", key, marker, True)
        for path, text, is_title in _texts(listing):
            if text and _hits(text, key, marker):
                hit(path, key, marker, is_title and not _generic(key, marker))
    return [
        _item(path, hits, Severity.ERROR if error else Severity.WARNING)
        for path, (hits, error) in found.items()
    ]


def with_rejections(
    report: ErrorReport,
    listing: CanonicalListing,
    rejections: Mapping[str, str],
    *,
    errors_only: bool = False,
) -> ErrorReport:
    """`report` with the check's items appended (`errors_only`: the ERROR ones, CP-D26); only an
    ERROR item fails it. No item: `report` itself."""
    items = rejected_value_items(listing, rejections) if rejections else []
    if errors_only:
        items = [i for i in items if i.severity is Severity.ERROR]
    if not items:
        return report
    errors = [*report.errors, *items][:MAX_ERROR_ITEMS]
    blocking = [i for i in errors if i.severity is Severity.ERROR]
    return report.model_copy(
        update={
            "errors": errors,
            "status": "failed" if blocking else "passed",
            "error_code": report.error_code or (blocking[0].error_code if blocking else None),
        }
    )
