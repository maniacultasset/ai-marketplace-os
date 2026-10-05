"""Rule evaluators and the engine entry point ``evaluate`` (S14a CODE-PLAN T5-T9).

Evaluated types: max_length, min_length, banned_terms, required_attribute, max_items, min_items,
allowed_values, conditional_required, cross_field, claim_requires_attribute and pattern, plus the
derived template checks (template.py).

Not evaluated: ``image_requirement`` (S17a/S17b, ImageReport) and ``fact_source``/
``fact_backed_attribute``, and every rule field or ``cross_field`` side on ``facts.*``,
``brand.*``, ``variants…``, ``request``, ``file.header``, ``file.row`` (``NOT_LISTING_ROOTS``) or
``listing``: not evaluated: post-v1 (``facts.py`` contract MINOR; no v1 owner) (D2).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorReport
from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.listing import CanonicalListing
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

from .report import Draft, RuleSetMismatchError, build_report, submitted_text
from .text import (
    SlotValue,
    compiled_rule_regex,
    condition_holds,
    find_terms,
    fold,
    is_present,
    iter_slots,
    lookup,
    measure,
    scalar_equal,
)


@dataclass(frozen=True)
class Ctx:
    listing: CanonicalListing
    template: CategoryTemplate | None
    product: Mapping[str, object] | None
    account: Mapping[str, object] | None

    def lookup(self, path: str) -> tuple[bool, object]:
        return lookup(self.listing, path, product=self.product, account=self.account)


Evaluator = Callable[[Rule, Ctx], list[Draft]]


def _text(value: object) -> str:
    return submitted_text(value) or ""


def _draft(
    rule: Rule, field: str, raw: object, code: ErrorCode | None = None, /, **ph: str
) -> Draft:
    return Draft(
        field=field,
        code=rule.rule_def.error_code if code is None else code,
        value=raw,
        placeholders=ph,
        rule=rule,
        severity=rule.severity,
    )


# ---- length (T5) ------------------------------------------------------------------------------


def _length(rule: Rule, ctx: Ctx) -> list[Draft]:
    rule_def = cast("MaxLength | MinLength", rule.rule_def)
    out: list[Draft] = []
    for path, value in iter_slots(ctx.listing, rule.field, kind="length"):
        size = measure(value, rule_def.unit)
        too_long = isinstance(rule_def, MaxLength) and size > rule_def.limit
        too_short = isinstance(rule_def, MinLength) and size < rule_def.limit
        if too_long or too_short:
            out.append(_draft(rule, path, value, value=str(size), limit=str(rule_def.limit)))
    return out


# ---- banned terms (T6) and claims (T9) --------------------------------------------------------


def _grouped_hits(
    slots: Sequence[tuple[str, SlotValue]], hit: Callable[[str], tuple[str, ...]]
) -> list[tuple[str, str, tuple[str, ...]]]:
    """(path, text, matched terms) per concrete path; list-attribute items share one path."""
    order: list[str] = []
    texts: dict[str, list[str]] = {}
    terms: dict[str, list[str]] = {}
    for path, value in slots:
        if not isinstance(value, str):
            continue
        found = hit(value)
        if not found:
            continue
        if path not in texts:
            order.append(path)
            texts[path] = []
            terms[path] = []
        texts[path].append(value)
        terms[path].extend(t for t in found if t not in terms[path])
    return [(p, " ".join(texts[p]), tuple(terms[p])) for p in order]


def _banned_terms(rule: Rule, ctx: Ctx) -> list[Draft]:
    rule_def = cast("BannedTerms", rule.rule_def)

    def hit(text: str) -> tuple[str, ...]:
        return find_terms(
            text, rule_def.terms, match=rule_def.match, case_sensitive=rule_def.case_sensitive
        )

    slots = iter_slots(ctx.listing, rule.field, kind="text")
    return [
        _draft(rule, path, text, value=text, term=", ".join(found))
        for path, text, found in _grouped_hits(slots, hit)
    ]


def _value_in(value: object, allowed: Sequence[str]) -> bool:
    folded = {fold(a) for a in allowed}
    if isinstance(value, list):
        return all(fold(str(item)) in folded for item in value)
    return fold(str(value)) in folded


def _claim(rule: Rule, ctx: Ctx) -> list[Draft]:
    rule_def = cast("ClaimRequiresAttribute", rule.rule_def)
    requires = rule_def.requires
    found, value = ctx.lookup(f"attributes.{requires.attribute}")
    supported = found and is_present(value)
    if supported and requires.any_of is not None:
        wanted = {fold(v) for v in requires.any_of}
        values = value if isinstance(value, list) else [value]
        supported = any(fold(str(v)) in wanted for v in values)
    if supported:
        return []

    def hit(text: str) -> tuple[str, ...]:
        return find_terms(text, rule_def.terms, match=rule_def.match, case_sensitive=False)

    slots = iter_slots(ctx.listing, rule.field, kind="text")
    return [
        _draft(rule, path, text, value=text, term=", ".join(terms), attribute=requires.attribute)
        for path, text, terms in _grouped_hits(slots, hit)
    ]


# ---- attributes (T7) --------------------------------------------------------------------------


def _attribute_value(ctx: Ctx, attribute: str) -> tuple[bool, object]:
    found, value = ctx.lookup(f"attributes.{attribute}")
    return found and is_present(value), value


def _not_allowed(rule: Rule, attribute: str, value: object, allowed: Sequence[str]) -> Draft:
    return _draft(
        rule,
        f"attributes.{attribute}",
        value,
        ErrorCode.VALUE_NOT_ALLOWED,
        value=_text(value),
        attribute=attribute,
        allowed_values=", ".join(allowed),
    )


def _required(rule: Rule, ctx: Ctx) -> list[Draft]:
    rule_def = cast("RequiredAttribute", rule.rule_def)
    present, value = _attribute_value(ctx, rule_def.attribute)
    path = f"attributes.{rule_def.attribute}"
    if not present:
        return [_draft(rule, path, None, attribute=rule_def.attribute)]
    allowed = rule_def.allowed_values
    if allowed is not None and not _value_in(value, allowed):
        return [_not_allowed(rule, rule_def.attribute, value, allowed)]
    return []


def template_allowed(template: CategoryTemplate | None, attribute: str) -> list[str] | None:
    """``allowed_values`` plus the ``value_map`` keys of the template spec, or None (no check)."""
    if template is None or attribute not in template.specs:
        return None
    spec = template.specs[attribute]
    if spec.allowed_values is None and not spec.value_map:
        return None
    return [*(spec.allowed_values or []), *sorted(spec.value_map)]


def _allowed_values(rule: Rule, ctx: Ctx) -> list[Draft]:
    rule_def = cast("AllowedValues", rule.rule_def)
    present, value = _attribute_value(ctx, rule_def.attribute)
    if not present:
        return []
    if rule_def.source == "inline":
        allowed = rule_def.values
    else:
        allowed = template_allowed(ctx.template, rule_def.attribute)
    if allowed is None or _value_in(value, allowed):
        return []
    return [
        _draft(
            rule,
            f"attributes.{rule_def.attribute}",
            value,
            value=_text(value),
            attribute=rule_def.attribute,
            allowed_values=", ".join(allowed),
        )
    ]


def _conditional(rule: Rule, ctx: Ctx) -> list[Draft]:
    rule_def = cast("ConditionalRequired", rule.rule_def)
    holds = all(
        condition_holds(c, ctx.listing, product=ctx.product, account=ctx.account)
        for c in rule_def.when
    )
    present, _value = _attribute_value(ctx, rule_def.attribute)
    if not holds or present:
        return []
    return [_draft(rule, f"attributes.{rule_def.attribute}", None, attribute=rule_def.attribute)]


# ---- items, cross_field and pattern (T8) ------------------------------------------------------


def _count_target(ctx: Ctx, field: str) -> tuple[str, list[object]] | None:
    path = "content.bullets" if field == "content.bullets[*]" else field
    found, value = ctx.lookup(path)
    if not found or not isinstance(value, list):
        return None
    return path, list(value)


def _items(rule: Rule, ctx: Ctx) -> list[Draft]:
    rule_def = cast("MaxItems | MinItems", rule.rule_def)
    target = _count_target(ctx, rule.field)
    if target is None:
        return []
    path, items = target
    count = len(items)
    if isinstance(rule_def, MaxItems):
        folded = [fold(str(i)) for i in items]
        bad = count > rule_def.limit or (rule_def.unique and len(set(folded)) != len(folded))
    else:
        bad = count < rule_def.limit
    if not bad:
        return []
    return [
        _draft(rule, path, [str(i) for i in items], value=str(count), limit=str(rule_def.limit))
    ]


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


_ORDER: Final[Mapping[str, Callable[[float, float], bool]]] = {
    "lte": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
    "gte": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
}


def _cross_holds(op: str, left: object, right: object) -> bool:
    """Whether the relation holds; incomparable sides make it fail (fail closed)."""
    if op in _ORDER:
        if not (_is_number(left) and _is_number(right)):
            return False
        return _ORDER[op](left, right)  # type: ignore[arg-type]
    if op in ("eq", "ne"):
        comparable = (
            (_is_number(left) and _is_number(right))
            or (isinstance(left, str) and isinstance(right, str))
            or (isinstance(left, bool) and isinstance(right, bool))
        )
        if not comparable:
            return False
        equal = scalar_equal(left, right)
        return equal if op == "eq" else not equal
    # contains_value_of / not_contains_value_of: a text left, a scalar (non-bool) right.
    if not isinstance(left, str) or not (isinstance(right, str) or _is_number(right)):
        return False
    contains = bool(find_terms(left, [str(right)], match="word", case_sensitive=False))
    return contains if op == "contains_value_of" else not contains


def _cross_field(rule: Rule, ctx: Ctx) -> list[Draft]:
    rule_def = cast("CrossField", rule.rule_def)
    left_found, left = ctx.lookup(rule_def.left)
    right_found, right = ctx.lookup(rule_def.right)
    if not (left_found and is_present(left) and right_found and is_present(right)):
        return []  # a missing side gives no item (D3f)
    if _cross_holds(rule_def.op, left, right):
        return []
    return [_draft(rule, rule_def.left, left, value=_text(left))]


def _pattern_slots(rule: Rule, ctx: Ctx) -> list[tuple[str, str]]:
    slots = [
        (p, v) for p, v in iter_slots(ctx.listing, rule.field, kind="text") if isinstance(v, str)
    ]
    if not slots and rule.field.startswith("attributes."):
        value = ctx.listing.attributes.get(rule.field[len("attributes.") :])
        if _is_number(value):
            slots = [(rule.field, str(value))]
    return slots


def _pattern(rule: Rule, ctx: Ctx) -> list[Draft]:
    rule_def = cast("Pattern", rule.rule_def)
    regex = compiled_rule_regex(rule_def.regex, ignore_case="i" in rule_def.flags)
    out: list[Draft] = []
    for path, text in _pattern_slots(rule, ctx):
        matched = regex.search(text) is not None
        if matched != (rule_def.must == "match"):
            out.append(_draft(rule, path, text, value=text))
    return out


DISPATCH: Final[Mapping[str, Evaluator]] = {
    "max_length": _length,
    "min_length": _length,
    "banned_terms": _banned_terms,
    "required_attribute": _required,
    "max_items": _items,
    "min_items": _items,
    "allowed_values": _allowed_values,
    "conditional_required": _conditional,
    "cross_field": _cross_field,
    "claim_requires_attribute": _claim,
    "pattern": _pattern,
}
NOT_EVALUATED: Final[frozenset[str]] = frozenset(
    {"image_requirement", "fact_source", "fact_backed_attribute"}
)


def _check_scope(
    listing: CanonicalListing, ruleset: RuleSet, template: CategoryTemplate | None
) -> None:
    if listing.marketplace is not ruleset.marketplace or listing.category != ruleset.category:
        raise RuleSetMismatchError("listing marketplace or category differs from the RuleSet")
    for rule in ruleset.rules:
        if rule.marketplace is not ruleset.marketplace:
            raise RuleSetMismatchError(f"rule {rule.id} belongs to another marketplace")
        if rule.category is not None and rule.category != ruleset.category:
            raise RuleSetMismatchError(f"rule {rule.id} belongs to another category")
    if template is not None and (
        template.marketplace is not ruleset.marketplace or template.category != ruleset.category
    ):
        raise RuleSetMismatchError("template marketplace or category differs from the RuleSet")


def collect_drafts(ruleset: RuleSet, ctx: Ctx) -> list[Draft]:
    drafts: list[Draft] = []
    for rule in sorted(ruleset.rules, key=lambda r: str(r.id)):
        if not rule.enabled or rule.rule_def.type in NOT_EVALUATED:
            continue
        drafts.extend(DISPATCH[rule.rule_def.type](rule, ctx))
    return drafts


def evaluate(
    listing: CanonicalListing,
    ruleset: RuleSet,
    *,
    template: CategoryTemplate | None = None,
    product: Mapping[str, object] | None = None,
    account: Mapping[str, object] | None = None,
) -> ErrorReport:
    """Validate ``listing`` against a resolved ``ruleset`` (and the category template).

    Pure and deterministic: the same input always gives the same report. It raises
    ``RuleSetMismatchError`` for a listing, rule or template of another scope, and
    ``UnsafeRuleRegexError`` for a regex it refuses to run (fail closed, never "passed").
    """
    _check_scope(listing, ruleset, template)
    ctx = Ctx(listing=listing, template=template, product=product, account=account)
    drafts = collect_drafts(ruleset, ctx)
    return build_report(drafts, listing_id=listing.listing_id, marketplace=ruleset.marketplace)
