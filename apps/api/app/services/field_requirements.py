"""`GET /api/v1/listings/{listing_id}/requirements` (client feedback item 16, bullet 2): the
marketplace's limits and rules for each field of one listing, so the Review section cards show
them beside the field instead of copying them into the web app.

It reads the same effective RuleSet and current category template the Rules Engine runs on
(`load_rules`: platform rows plus the brand's own, the category rows shadowing the category-wide
ones, the highest enabled version), and summarises them per field. It gives no verdict: the
checks still decide (invariant 2). One `tenant_uow` read; another tenant's listing is 404.

Per text field (`content.title`, `content.bullets`, `content.bullets[*]` for each bullet,
`content.description`, `keywords.search_terms`, `keywords.hidden_terms`, and `*` for rules that
apply to every text field):
- length limits by unit (`max_length` / `min_length`; several rules on one field keep the
  tightest), item counts (`max_items` / `min_items`);
- banned words (`banned_terms` with word or substring match; the first `MAX_TERMS` are listed and
  `banned_count` counts them all). A regex list is never sent: it becomes one plain line;
- plain lines for the rest: claims that need an attribute, format patterns, cross-field checks.

Per attribute: the template level (`requirement_levels`, raised to `required` by a
`required_attribute` rule and to `conditional` by a `conditional_required` one), the value type,
the allowed values (the template spec's, else its value map's keys, narrowed by an inline
`allowed_values` rule), and a length limit. Image rules are left out (the photo checklist shows
them), as are the fact rules the engine does not run on text.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.rules import (
    AllowedValues,
    BannedTerms,
    ClaimRequiresAttribute,
    ConditionalRequired,
    CrossField,
    MaxItems,
    MaxLength,
    MinItems,
    MinLength,
    Pattern,
    RequiredAttribute,
    Rule,
    RuleSet,
)

from app.core.errors import NotFoundError
from app.core.rbac import Principal
from app.repositories.uow import tenant_uow
from app.services.rules_engine import requirement_levels
from app.services.rules_engine.text import distinct_spellings
from app.services.rules_loader import load_rules

__all__ = [
    "MAX_TERMS",
    "MAX_VALUES",
    "AttributeRequirement",
    "FieldRequirement",
    "ListingRequirements",
    "listing_requirements",
    "summarise",
]

# The first banned words listed per field, and the first allowed values per attribute.
MAX_TERMS: Final = 60
MAX_VALUES: Final = 100
# The text fields, in the order the marketplace page shows them (`*` is every text field).
TEXT_FIELDS: Final = (
    "content.title",
    "content.bullets",
    "content.bullets[*]",
    "content.description",
    "keywords.search_terms",
    "keywords.hidden_terms",
    "*",
)
# A regex word list is never sent; its pack names what it refuses in plain words.
_REGEX_PACK_LINE: Final = {
    "contact": "No links, web addresses, emails or phone numbers.",
}
_REGEX_FALLBACK: Final = "Some text patterns are not allowed."
_LEVEL_ORDER: Final = ("required", "conditional", "recommended", "optional", "unsupported")
_NOT_TEXT: Final = frozenset({"image_requirement", "fact_source", "fact_backed_attribute"})


@dataclass(slots=True)
class FieldRequirement:
    path: str
    max_chars: int | None = None
    max_bytes: int | None = None
    max_words: int | None = None
    min_chars: int | None = None
    min_bytes: int | None = None
    min_words: int | None = None
    max_items: int | None = None
    min_items: int | None = None
    banned_terms: list[str] = field(default_factory=list)
    banned_count: int = 0
    rules: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AttributeRequirement:
    key: str
    level: str
    value_type: str | None = None
    allowed_values: list[str] = field(default_factory=list)
    allowed_count: int = 0
    max_length: int | None = None  # characters
    unit: str | None = None  # a measurement's unit (cm, g), from the template spec


@dataclass(frozen=True, slots=True)
class ListingRequirements:
    listing_id: uuid.UUID
    marketplace: str
    category: str
    rules_version: int
    has_template: bool
    fields: list[FieldRequirement]
    attributes: list[AttributeRequirement]


def _human(name: str) -> str:
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else name


def _tighter(current: int | None, new: int, *, lower: bool) -> int:
    if current is None:
        return new
    return min(current, new) if lower else max(current, new)


def _set_length(req: FieldRequirement, bound: str, unit: str, limit: int) -> None:
    name = f"{bound}_{unit}"
    setattr(req, name, _tighter(getattr(req, name), limit, lower=bound == "max"))


def _quoted(terms: Iterable[str], n: int = 3) -> str:
    return ", ".join(f"“{t}”" for t in list(terms)[:n])


def _text_rule(req: FieldRequirement, rule: Rule, banned: dict[str, list[str]]) -> None:
    d = rule.rule_def
    if isinstance(d, MaxLength):
        _set_length(req, "max", d.unit, d.limit)
    elif isinstance(d, MinLength):
        _set_length(req, "min", d.unit, d.limit)
    elif isinstance(d, MaxItems):
        req.max_items = _tighter(req.max_items, d.limit, lower=True)
    elif isinstance(d, MinItems):
        req.min_items = _tighter(req.min_items, d.limit, lower=False)
    elif isinstance(d, BannedTerms):
        if d.match == "regex":
            line = _REGEX_PACK_LINE.get(d.pack, _REGEX_FALLBACK)
            if line not in req.rules:
                req.rules.append(line)
        else:
            words = banned.setdefault(req.path, [])
            seen = {w.casefold() for w in words}
            words.extend(t for t in d.terms if t.casefold() not in seen)
    elif isinstance(d, ClaimRequiresAttribute):
        req.rules.append(
            f"Claims like {_quoted(d.terms)} need the {_human(d.requires.attribute).lower()} "
            "attribute on the product."
        )
    elif isinstance(d, Pattern):
        verb = "Must follow" if d.must == "match" else "Must not contain"
        req.rules.append(f"{verb} the {_human(d.name).lower()} format.")
    elif isinstance(d, CrossField):
        req.rules.append(f"Checked against another field: {_human(d.name).lower()}.")


def _attribute_rows(
    ruleset: RuleSet, template: CategoryTemplate | None
) -> list[AttributeRequirement]:
    levels: dict[str, str] = dict(requirement_levels(template)) if template else {}
    inline: dict[str, list[str]] = {}
    caps: dict[str, int] = {}
    for rule in ruleset.rules:
        d = rule.rule_def
        if not rule.enabled:
            continue
        if isinstance(d, RequiredAttribute):
            levels[d.attribute] = "required"
            if d.allowed_values:
                inline[d.attribute] = list(d.allowed_values)
        elif isinstance(d, ConditionalRequired):
            if levels.get(d.attribute) != "required":
                levels[d.attribute] = "conditional"
        elif isinstance(d, AllowedValues):
            levels.setdefault(d.attribute, "optional")
            if d.source == "inline" and d.values:
                inline[d.attribute] = list(d.values)
        elif (
            isinstance(d, MaxLength) and d.unit == "chars" and rule.field.startswith("attributes.")
        ):
            key = rule.field.removeprefix("attributes.")
            levels.setdefault(key, "optional")
            caps[key] = _tighter(caps.get(key), d.limit, lower=True)
    rows: list[AttributeRequirement] = []
    for key, level in levels.items():
        spec = template.specs.get(key) if template else None
        values: list[str] = []
        if spec is not None:
            values = list(spec.allowed_values or sorted(spec.value_map))
        if key in inline:
            values = inline[key]
        values = distinct_spellings(values)  # B17: "Sleeveless" and "sleeveless" are one value
        row = AttributeRequirement(
            key=key,
            level=level,
            value_type=spec.value_type if spec else None,
            allowed_values=values[:MAX_VALUES],
            allowed_count=len(values),
            max_length=spec.max_length if spec else None,
            unit=spec.unit if spec else None,
        )
        if key in caps:
            row.max_length = _tighter(row.max_length, caps[key], lower=True)
        rows.append(row)
    rows.sort(key=lambda r: (_LEVEL_ORDER.index(r.level), r.key))
    return rows


def summarise(
    ruleset: RuleSet, template: CategoryTemplate | None
) -> tuple[list[FieldRequirement], list[AttributeRequirement]]:
    """The per-field summary (pure: the module docstring has the rules)."""
    by_path: dict[str, FieldRequirement] = {}
    banned: dict[str, list[str]] = {}
    for rule in sorted(ruleset.rules, key=lambda r: str(r.id)):
        if not rule.enabled or rule.rule_def.type in _NOT_TEXT or rule.field not in TEXT_FIELDS:
            continue
        req = by_path.setdefault(rule.field, FieldRequirement(path=rule.field))
        _text_rule(req, rule, banned)
    for path, words in banned.items():
        by_path[path].banned_terms = words[:MAX_TERMS]
        by_path[path].banned_count = len(words)
    fields = [by_path[p] for p in TEXT_FIELDS if p in by_path]
    return fields, _attribute_rows(ruleset, template)


async def listing_requirements(principal: Principal, listing_id: uuid.UUID) -> ListingRequirements:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        listing = await uow.listings.get(listing_id)
        if listing is None:
            raise NotFoundError("listing_id")
        loaded = await load_rules(
            uow,
            tenant_id=principal.tenant_id,
            marketplace=listing.marketplace,
            category=listing.category,
        )
    fields, attributes = summarise(loaded.ruleset, loaded.template)
    return ListingRequirements(
        listing_id=listing.id,
        marketplace=listing.marketplace,
        category=listing.category,
        rules_version=loaded.ruleset.rules_version,
        has_template=loaded.template is not None,
        fields=fields,
        attributes=attributes,
    )
