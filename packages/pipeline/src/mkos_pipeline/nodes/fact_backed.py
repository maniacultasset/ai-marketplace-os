from typing import Any

from mkos_schemas.rules import AttributeSpec

from mkos_pipeline.product import lookup_fact


def _normalise(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(_normalise(item) for item in value)
    return str(value).strip().casefold()


def _canonical_from_spec(value: Any, spec: AttributeSpec | None) -> Any | None:
    if spec is None or not spec.allowed_values:
        return None
    folded = _normalise(value)
    for allowed in spec.allowed_values:
        if _normalise(allowed) == folded:
            return allowed
    return None


def keep_attribute(key: str, value: Any, product: dict[str, Any], spec: AttributeSpec | None) -> bool:
    """True only when value equals a fact or its normalised form."""
    if value in (None, "", [], {}):
        return False
    _path, fact = lookup_fact(product, key)
    if fact is None:
        return False
    if _normalise(value) == _normalise(fact):
        return True
    allowed = _canonical_from_spec(value, spec)
    fact_allowed = _canonical_from_spec(fact, spec)
    return allowed is not None and fact_allowed is not None and allowed == fact_allowed


def canonical_attribute_value(
    key: str,
    value: Any,
    product: dict[str, Any],
    spec: AttributeSpec | None,
) -> Any | None:
    if not keep_attribute(key, value, product, spec):
        return None
    _path, fact = lookup_fact(product, key)
    allowed_from_fact = _canonical_from_spec(fact, spec)
    if allowed_from_fact is not None:
        return allowed_from_fact
    return fact
