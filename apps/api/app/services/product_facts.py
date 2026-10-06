"""Seller facts for a product create (CODE-PLAN S12 T4; DATABASE.md:1082-1115, :1197).

Pure: no I/O. `build_attributes` resolves each flat input key against the dictionary rows the
repository returned (an exact `key` beats an `input_aliases` match) and wraps the value as
`Fact{value, source: "seller", captured_via: "api", updated_at}`. `seller_provenance` is the
`provenance` the typed `title` and `category` columns need (`ck_products_typed_facts_sourced`),
with no `value` (PLAN D3). Refusals name the field (`attributes.<key>`), never the value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from mkos_schemas.codes import ErrorCode

from app.core.errors import InputError
from app.repositories.attribute_definitions import AttributeDef

__all__ = ["AttributeValue", "build_attributes", "fact_stamp", "seller_provenance"]

AttributeValue = str | int | float | bool
STANDALONE = "standalone"


def fact_stamp(now: datetime) -> str:
    """ISO-8601 UTC with a `Z` suffix; a naive time is refused."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("fact timestamps must be timezone-aware")
    return now.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _seller(stamp: str) -> dict[str, object]:
    return {"source": "seller", "captured_via": "api", "updated_at": stamp}


def _canonical(name: str, defs: Sequence[AttributeDef]) -> AttributeDef | None:
    for definition in defs:
        if definition.key == name:
            return definition
    for definition in defs:
        if name in definition.input_aliases:
            return definition
    return None


def build_attributes(
    inputs: Mapping[str, AttributeValue], defs: Sequence[AttributeDef], *, now: datetime
) -> dict[str, dict[str, object]]:
    stamp = fact_stamp(now)
    built: dict[str, dict[str, object]] = {}
    for name, value in inputs.items():
        definition = _canonical(name, defs)
        if definition is None:
            raise InputError(ErrorCode.UNKNOWN_FACT_KEY, f"attributes.{name}")
        if definition.storage != "attributes" or STANDALONE not in definition.owner_levels:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"attributes.{name}")
        if definition.key in built:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"attributes.{definition.key}")
        built[definition.key] = {"value": value, **_seller(stamp)}
    return built


def seller_provenance(*, now: datetime) -> dict[str, dict[str, object]]:
    stamp = fact_stamp(now)
    return {"title": _seller(stamp), "category": _seller(stamp)}
