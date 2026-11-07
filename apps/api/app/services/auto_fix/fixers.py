"""The deterministic fixers (SI4 A2, A3; spec L1061-1067 "formatting issue", "wrong portal enum
mapped from known canonical value").

Each fixer takes a listing and one error item and returns a **copy** with only that item's slot
changed plus the record of what it did, or None when it cannot fix the item safely. A fixer never
invents a value: a trim keeps a prefix of the existing text cut at a word boundary (at most
`TRIM_MAX_SHARE` of it, D5), a keyword fix drops whole terms, and a remap writes an allowed
spelling that the existing value folds to, exactly one, on an AI-sourced, non-critical key (D6).

Pure: no I/O, no clock, no randomness (the `auto-fix-pure` import contract).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal, cast
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import FixAction, Severity
from mkos_schemas.errors import ErrorItem
from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.rules import MaxLength, Rule, RuleSet

from app.services.auto_fix.policy import TRIM_MAX_SHARE, fact_key
from app.services.auto_fix.text import action_taken
from app.services.rules_engine.template import template_allowed
from app.services.rules_engine.text import iter_slots, measure

Unit = Literal["chars", "bytes", "words"]
SlotValue = str | list[str]

TEXT_SLOTS: Final = frozenset({"content.title", "content.description"})
KEYWORD_SLOTS: Final = frozenset({"keywords.search_terms", "keywords.hidden_terms"})
_BULLET_PREFIX: Final = "content.bullets["
TRAILING_PUNCTUATION: Final = " ,;:-\u2013\u2014/&"


@dataclass(frozen=True, slots=True)
class FixRecord:
    field: str
    error_code: ErrorCode
    fix_action: FixAction
    rule_id: UUID | None
    before_sha: str  # sha256 hex of the NFC slot text
    after_sha: str
    before_size: int
    after_size: int
    unit: str
    action_taken: str
    message: str = ""  # the engine's message on the item it fixed (the trail row's text)


@dataclass(frozen=True, slots=True, kw_only=True)
class FixContext:
    ruleset: RuleSet
    template: CategoryTemplate | None
    critical_keys: frozenset[str]  # required, no default (CP-D9: the override never fails open)
    overlay_keys: frozenset[str] = field(default_factory=frozenset)


# ---- slot access ----


def _bullet_index(path: str) -> int | None:
    if not (path.startswith(_BULLET_PREFIX) and path.endswith("]")):
        return None
    raw = path[len(_BULLET_PREFIX) : -1]
    return int(raw) if raw.isdigit() else None


def read_slot(listing: CanonicalListing, path: str) -> SlotValue | None:
    """The value of a text or keyword slot, else None (not a slot this module fixes)."""
    if path == "content.title":
        return listing.content.title
    if path == "content.description":
        return listing.content.description
    if path in KEYWORD_SLOTS:
        return list(getattr(listing.keywords, path.removeprefix("keywords.")))
    index = _bullet_index(path)
    if index is not None:
        bullets = listing.content.bullets
        return bullets[index] if index < len(bullets) else None
    return None


def write_slot(listing: CanonicalListing, path: str, value: SlotValue) -> CanonicalListing:
    """A deep copy of ``listing`` with only ``path`` changed."""
    out = listing.model_copy(deep=True)
    if path in TEXT_SLOTS:
        name = path.removeprefix("content.")
        out.content = out.content.model_copy(update={name: value})
    elif path in KEYWORD_SLOTS:
        name = path.removeprefix("keywords.")
        out.keywords = out.keywords.model_copy(update={name: list(value)})
    else:
        index = _bullet_index(path)
        if index is None or not isinstance(value, str):
            raise ValueError(f"{path!r} is not a fixable slot")
        bullets = list(out.content.bullets)
        bullets[index] = value
        out.content = out.content.model_copy(update={"bullets": bullets})
    return CanonicalListing.model_validate(out.model_dump(mode="python"))


def digest(value: SlotValue | str | int | float | bool | None) -> str:
    """sha256 hex of the NFC slot text (a list as JSON), never the text itself."""
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        text = unicodedata.normalize("NFC", text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- cleanup and the share cap ----


def clean_text(text: str) -> str:
    """NFC, whitespace runs to one space, other control characters removed, ends stripped."""
    text = unicodedata.normalize("NFC", text)
    text = " ".join(text.split())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc")
    return " ".join(text.split())


def within_share(before: int, after: int) -> bool:
    """D5: at most `TRIM_MAX_SHARE` of the original slot removed, as an exact `Decimal`."""
    if before <= 0:
        return False
    return Decimal(before - after) / Decimal(before) <= TRIM_MAX_SHARE


def _rule(ctx: FixContext, rule_id: UUID | None) -> Rule | None:
    if rule_id is None:
        return None
    for rule in ctx.ruleset.rules:
        if rule.id == rule_id:
            return rule
    return None


def _slot_limit(listing: CanonicalListing, path: str, own: Rule, ruleset: RuleSet) -> int:
    """The tightest enabled blocking `max_length` limit on this slot in the item's unit, so one
    pass satisfies every length rule on it (critique log, low: Flipkart 200 and 150)."""
    own_def = cast("MaxLength", own.rule_def)
    limits = [own_def.limit]
    for rule in ruleset.rules:
        rule_def = rule.rule_def
        if (
            rule.enabled
            and rule.severity is Severity.ERROR
            and isinstance(rule_def, MaxLength)
            and rule_def.unit == own_def.unit
            and any(p == path for p, _v in iter_slots(listing, rule.field, kind="length"))
        ):
            limits.append(rule_def.limit)
    return min(limits)


def _record(
    item: ErrorItem,
    fix_action: FixAction,
    before: SlotValue,
    after: SlotValue,
    *,
    before_size: int,
    after_size: int,
    unit: str,
    text: str,
) -> FixRecord:
    return FixRecord(
        field=item.field,
        error_code=item.error_code,
        fix_action=fix_action,
        rule_id=item.rule_id,
        before_sha=digest(before),
        after_sha=digest(after),
        before_size=before_size,
        after_size=after_size,
        unit=unit,
        action_taken=text,
        message=item.message,
    )


def _trim_text(cleaned: str, limit: int, unit: Unit) -> str | None:
    """The longest prefix cut at a space that fits, trailing punctuation stripped; else None."""
    cut = cleaned.rfind(" ")
    while cut > 0:
        prefix = cleaned[:cut]
        if measure(prefix, unit) <= limit:
            out = prefix.rstrip(TRAILING_PUNCTUATION)
            return out or None
        cut = cleaned.rfind(" ", 0, cut)
    return None


def _dedupe(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        key = term.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def fix_reformat(
    listing: CanonicalListing, item: ErrorItem, ctx: FixContext
) -> tuple[CanonicalListing, FixRecord] | None:
    """A text slot: clean up, then cut at the last word boundary that fits. A keyword list:
    clean up, drop repeats, then drop whole terms from the end. Within the D5 share only; the
    limit and unit come from the item's rule, never from its message."""
    path = item.field
    value = read_slot(listing, path)
    rule = _rule(ctx, item.rule_id)
    if value is None or rule is None or not isinstance(rule.rule_def, MaxLength):
        return None
    unit: Unit = rule.rule_def.unit
    limit = _slot_limit(listing, path, rule, ctx.ruleset)
    before = measure(value, unit)
    if before <= limit:
        return None
    if isinstance(value, str):
        return _reformat_text(listing, item, value, before=before, limit=limit, unit=unit)
    return _reformat_terms(listing, item, value, before=before, limit=limit, unit=unit)


def _reformat_text(
    listing: CanonicalListing, item: ErrorItem, value: str, *, before: int, limit: int, unit: Unit
) -> tuple[CanonicalListing, FixRecord] | None:
    cleaned = clean_text(value)
    trimmed = measure(cleaned, unit) > limit
    result = _trim_text(cleaned, limit, unit) if trimmed else cleaned
    if not result or result == value:
        return None
    after = measure(result, unit)
    if not within_share(before, after):
        return None
    text = action_taken(
        FixAction.REFORMAT,
        item.field,
        before_size=before,
        after_size=after,
        unit=unit,
        limit=limit,
        trimmed=trimmed,
    )
    record = _record(
        item, FixAction.REFORMAT, value, result,
        before_size=before, after_size=after, unit=unit, text=text,
    )  # fmt: skip
    return write_slot(listing, item.field, result), record


def _reformat_terms(
    listing: CanonicalListing,
    item: ErrorItem,
    value: list[str],
    *,
    before: int,
    limit: int,
    unit: Unit,
) -> tuple[CanonicalListing, FixRecord] | None:
    cleaned = [t for t in (clean_text(term) for term in value) if t]
    terms = _dedupe(cleaned)
    duplicates = len(cleaned) - len(terms)
    deduped = measure(terms, unit)
    trimmed = deduped > limit
    while terms and measure(terms, unit) > limit:
        terms = terms[:-1]
    if not terms or terms == value:
        return None
    after = measure(terms, unit)
    # The share counts only the terms dropped from the end: repeats and spacing lose nothing.
    if not within_share(before, before - (deduped - after)):
        return None
    text = action_taken(
        FixAction.REFORMAT,
        item.field,
        before_size=before,
        after_size=after,
        unit=unit,
        limit=limit,
        duplicates=duplicates,
        trimmed=trimmed,
    )
    record = _record(
        item, FixAction.REFORMAT, value, terms,
        before_size=before, after_size=after, unit=unit, text=text,
    )  # fmt: skip
    return write_slot(listing, item.field, terms), record


def fix_keyword_duplicates(
    listing: CanonicalListing, item: ErrorItem, ctx: FixContext
) -> tuple[CanonicalListing, FixRecord] | None:
    """`KEYWORD_DUPLICATE`: drop later case-insensitive repeats, keeping the first spelling and
    the order. None when nothing repeats."""
    del ctx  # the same signature as every fixer
    if item.field not in KEYWORD_SLOTS:
        return None
    value = read_slot(listing, item.field)
    if not isinstance(value, list):
        return None
    terms = _dedupe(value)
    if len(terms) == len(value):
        return None
    removed = len(value) - len(terms)
    text = action_taken(
        FixAction.REFORMAT,
        item.field,
        before_size=len(value),
        after_size=len(terms),
        unit="terms",
        limit=None,
        duplicates=removed,
        trimmed=False,
    )
    record = _record(
        item, FixAction.REFORMAT, value, terms,
        before_size=len(value), after_size=len(terms), unit="terms", text=text,
    )  # fmt: skip
    return write_slot(listing, item.field, terms), record


# ---- remap_value (A3) ----


def fold_spelling(value: str) -> str:
    """Strip, casefold, and read runs of whitespace, `-` and `_` as one space."""
    folded = value.strip().casefold().replace("-", " ").replace("_", " ")
    return " ".join(folded.split())


def _one_spelling(value: str, candidates: list[str]) -> str | None:
    matches = {c for c in candidates if fold_spelling(c) == fold_spelling(value)}
    return matches.pop() if len(matches) == 1 else None


def fix_remap_value(
    listing: CanonicalListing, item: ErrorItem, ctx: FixContext
) -> tuple[CanonicalListing, FixRecord] | None:
    """`VALUE_NOT_ALLOWED` on an AI-sourced, non-critical enum attribute: write the one allowed
    spelling each value folds to. No match, or two candidates folding equal, gives None."""
    key = fact_key(item.field)
    if (
        item.error_code is not ErrorCode.VALUE_NOT_ALLOWED
        or key is None
        or item.field != f"attributes.{key}"
        or key in ctx.critical_keys
        or key in listing.attribute_sources
        or key in ctx.overlay_keys
        or ctx.template is None
        or key not in ctx.template.specs
        or ctx.template.specs[key].value_type != "enum"
    ):
        return None
    candidates = template_allowed(ctx.template, key)
    value = listing.attributes.get(key)
    if not candidates or not isinstance(value, str | list):
        return None
    values = value if isinstance(value, list) else [value]
    mapped: list[str] = []
    for one in values:
        if not isinstance(one, str):
            return None
        spelling = _one_spelling(one, candidates)
        if spelling is None:
            return None
        mapped.append(spelling)
    changed = sum(1 for a, b in zip(values, mapped, strict=True) if a != b)
    if not changed:
        return None
    new_value: str | list[str] = mapped if isinstance(value, list) else mapped[0]
    out = listing.model_copy(deep=True)
    out.attributes = {**out.attributes, key: new_value}
    out = CanonicalListing.model_validate(out.model_dump(mode="python"))
    text = action_taken(
        FixAction.REMAP_VALUE,
        item.field,
        before_size=len(values),
        after_size=changed,
        unit="values",
        limit=None,
    )
    record = _record(
        item, FixAction.REMAP_VALUE, value, new_value,
        before_size=len(values), after_size=changed, unit="values", text=text,
    )  # fmt: skip
    return out, record
