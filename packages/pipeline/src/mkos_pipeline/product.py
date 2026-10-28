from typing import Any

from mkos_schemas.rules import AttributeSpec, CategoryTemplate, Rule, RuleSet

ATTRIBUTE_ALIASES: dict[str, tuple[str, ...]] = {
    "color": ("color", "color_name", "colour", "colour_name"),
    "color_name": ("color_name", "color", "colour", "colour_name"),
    "fabric_type": ("fabric_type", "fabric", "material"),
    "material": ("material", "fabric_type", "fabric"),
    "sleeve_length": ("sleeve_length", "sleeve", "sleeve_type"),
    "sleeve_type": ("sleeve_type", "sleeve_length", "sleeve"),
    "pattern": ("pattern",),
    "fit": ("fit",),
    "neckline": ("neckline", "neck_style", "collar_style"),
    "occasion": ("occasion",),
}

INVENTED_CLAIM_TERMS = ("organic", "gots", "certified", "waterproof", "anti-bacterial", "handmade")
DEFAULT_BANNED = ("sale", "discount", "cheapest", "best", "number one")


def dotted_get(product: dict[str, Any], path: str) -> Any:
    current: Any = product
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    if current in (None, "", [], {}):
        return None
    return current


def identity(product: dict[str, Any]) -> dict[str, Any]:
    value = product.get("identity")
    return value if isinstance(value, dict) else {}


def product_attributes(product: dict[str, Any]) -> dict[str, Any]:
    value = product.get("attributes")
    return value if isinstance(value, dict) else {}


def lookup_fact(product: dict[str, Any], key: str) -> tuple[str | None, Any]:
    attrs = product_attributes(product)
    aliases = ATTRIBUTE_ALIASES.get(key, (key,))
    for alias in aliases:
        if alias in attrs and attrs[alias] not in (None, "", [], {}):
            return f"attributes.{alias}", attrs[alias]
    ident = identity(product)
    if key in ident and ident[key] not in (None, "", []):
        return f"identity.{key}", ident[key]
    return None, None


def fact_exists(product: dict[str, Any], path: str) -> bool:
    return dotted_get(product, path) is not None


def enabled_rules(rule_set: RuleSet, field: str | None = None) -> list[Rule]:
    rules = [rule for rule in rule_set.rules if rule.enabled]
    if field is None:
        return rules
    return [rule for rule in rules if rule.field in {field, "*"}]


def max_length_for(rule_set: RuleSet, field: str, default: int) -> int:
    lengths = [
        int(rule.rule_def["max_length"])
        for rule in enabled_rules(rule_set, field)
        if rule.rule_type == "max_length" and "max_length" in rule.rule_def
    ]
    return min(lengths) if lengths else default


def min_length_for(rule_set: RuleSet, field: str, default: int = 1) -> int:
    lengths = [
        int(rule.rule_def["min_length"])
        for rule in enabled_rules(rule_set, field)
        if rule.rule_type == "min_length" and "min_length" in rule.rule_def
    ]
    return max(lengths) if lengths else default


def max_items_for(rule_set: RuleSet, field: str, default: int) -> int:
    counts = [
        int(rule.rule_def["max_items"])
        for rule in enabled_rules(rule_set, field)
        if rule.rule_type == "max_items" and "max_items" in rule.rule_def
    ]
    return min(counts) if counts else default


def banned_terms(rule_set: RuleSet) -> tuple[str, ...]:
    terms: list[str] = list(DEFAULT_BANNED)
    for rule in enabled_rules(rule_set):
        if rule.rule_type != "banned_terms":
            continue
        extra = rule.rule_def.get("terms") or rule.rule_def.get("banned_terms") or []
        terms.extend(str(term) for term in extra)
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        folded = term.casefold()
        if folded not in seen:
            seen.add(folded)
            unique.append(term)
    return tuple(unique)


def contains_banned(text: str, terms: tuple[str, ...]) -> str | None:
    folded = text.casefold()
    for term in terms:
        if term.casefold() in folded:
            return term
    return None


def spec_for(template: CategoryTemplate, key: str) -> AttributeSpec | None:
    return template.specs.get(key)


def compact_rules(rule_set: RuleSet) -> dict[str, Any]:
    return {
        "max_lengths": {
            rule.field: rule.rule_def.get("max_length")
            for rule in enabled_rules(rule_set)
            if rule.rule_type == "max_length"
        },
        "banned_terms": list(banned_terms(rule_set)),
        "max_items": {
            rule.field: rule.rule_def.get("max_items")
            for rule in enabled_rules(rule_set)
            if rule.rule_type == "max_items"
        },
    }
