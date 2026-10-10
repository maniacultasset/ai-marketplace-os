"""Seller facts for a product create (CODE-PLAN S12 T4; DATABASE.md:1082-1115, :1197).

Pure: no I/O. `build_attributes` resolves each flat input key against the dictionary rows the
repository returned (an exact `key` beats an `input_aliases` match) and wraps the value as
`Fact{value, source: "seller", captured_via: "api", updated_at}`. `seller_provenance` is the
`provenance` the typed `title` and `category` columns need (`ck_products_typed_facts_sourced`),
with no `value` (PLAN D3). Refusals name the field (`attributes.<key>`), never the value.

The built map must fit `fact_map_valid`'s cap (`FACT_MAP_MAX_BYTES` octets of its `jsonb::text`
form; migration 0001): `jsonb_text_bytes` computes that length, so an over-size map is a 422
`VALIDATION_FAILED` on `attributes`, never the CHECK's 500 (audit DB-1). The schema caps values
in characters; the database counts UTF-8 bytes of the whole fact envelope.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from mkos_schemas.codes import ErrorCode

from app.core.errors import InputError
from app.repositories.attribute_definitions import AttributeDef

__all__ = [
    "FACT_MAP_MAX_BYTES",
    "AttributeValue",
    "build_attributes",
    "fact_stamp",
    "jsonb_text_bytes",
    "seller_provenance",
]

AttributeValue = str | int | float | bool
STANDALONE = "standalone"
FACT_MAP_MAX_BYTES: Final = 131_072
"""`fact_map_valid`: `octet_length(m::text) > 131072` is refused (migration 0001)."""


def jsonb_text_bytes(value: object) -> int:
    """The UTF-8 length of PostgreSQL's `jsonb::text` for `value`: `", "` and `": "`
    separators, strings escaped as JSON with non-ASCII kept raw, numbers in `numeric`'s plain
    decimal form (`1e+20` is printed as 21 digits)."""
    if isinstance(value, Mapping):
        if not value:
            return 2
        inner = sum(jsonb_text_bytes(str(k)) + 2 + jsonb_text_bytes(v) for k, v in value.items())
        return 2 + inner + 2 * (len(value) - 1)
    if isinstance(value, list | tuple):
        if not value:
            return 2
        return 2 + sum(jsonb_text_bytes(v) for v in value) + 2 * (len(value) - 1)
    if isinstance(value, str):
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8", "surrogatepass"))
    if value is None or isinstance(value, bool):
        return len(json.dumps(value))
    if isinstance(value, int):
        return len(str(value))
    if isinstance(value, float):
        return len(format(Decimal(repr(value)), "f"))
    raise TypeError(f"not a JSON value: {type(value).__name__}")


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
    if jsonb_text_bytes(built) > FACT_MAP_MAX_BYTES:
        raise InputError(ErrorCode.VALIDATION_FAILED, "attributes")
    return built


def seller_provenance(*, now: datetime) -> dict[str, dict[str, object]]:
    stamp = fact_stamp(now)
    return {"title": _seller(stamp), "category": _seller(stamp)}
