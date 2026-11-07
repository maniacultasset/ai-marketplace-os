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

SI1 T7: the build is level-aware (`level`: a key outside the row's owner levels is
`FACT_LEVEL_INVALID` on a style or variant, `VALIDATION_FAILED` on a standalone create, which keeps
create's declared codes). A `composition` key is parsed (`app.services.composition`): the Fact keeps
the seller's text as `value` and the fibres as `normalized`. `size_chart_url` must be an https URL.
The size chart is stored as `size_measurements` on a style or standalone row only
(`SIZE_CHART_LEVELS`; the dictionary also lists colour and variant for that key, so the rule is
explicit).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from urllib.parse import urlsplit

from mkos_schemas.codes import ErrorCode

from app.core.errors import InputError
from app.repositories.attribute_definitions import AttributeDef
from app.services.composition import CompositionError, normalized, parse_composition
from app.services.intake_values import SizeChartError, normalize_size_chart

__all__ = [
    "FACT_MAP_MAX_BYTES",
    "SIZE_CHART_LEVELS",
    "AttributeValue",
    "build_attributes",
    "fact_stamp",
    "jsonb_text_bytes",
    "owned_at",
    "seller_column_fact",
    "seller_provenance",
    "size_chart_fact",
]

AttributeValue = str | int | float | bool
STANDALONE = "standalone"
SIZE_CHART_KEY: Final = "size_measurements"
SIZE_CHART_URL_KEY: Final = "size_chart_url"
SIZE_CHART_URL_MAX: Final = 2000
SIZE_CHART_LEVELS: Final = frozenset({"style", "standalone"})
"""PLAN D1/D1b: the size chart sits on the style or standalone row (CODE-PLAN T7)."""
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


def _https_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > SIZE_CHART_URL_MAX:
        return False
    parts = urlsplit(value)
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and not any(c.isspace() or c == "\x00" for c in value)
    )


def _envelope(definition: AttributeDef, value: AttributeValue, stamp: str) -> dict[str, object]:
    field = f"attributes.{definition.key}"
    if definition.value_type == "composition":
        if not isinstance(value, str):
            raise InputError(ErrorCode.VALIDATION_FAILED, field)
        try:
            fibres = parse_composition(value)
        except CompositionError as exc:
            raise InputError(ErrorCode.VALIDATION_FAILED, field) from exc
        return {"value": value, "normalized": normalized(fibres), **_seller(stamp)}
    if definition.key == SIZE_CHART_URL_KEY and not _https_url(value):
        raise InputError(ErrorCode.VALIDATION_FAILED, field)
    return {"value": value, **_seller(stamp)}


def build_attributes(
    inputs: Mapping[str, AttributeValue],
    defs: Sequence[AttributeDef],
    *,
    now: datetime,
    level: str = STANDALONE,
) -> dict[str, dict[str, object]]:
    stamp = fact_stamp(now)
    built: dict[str, dict[str, object]] = {}
    for name, value in inputs.items():
        definition = _canonical(name, defs)
        if definition is None:
            raise InputError(ErrorCode.UNKNOWN_FACT_KEY, f"attributes.{name}")
        if definition.storage != "attributes":
            raise InputError(ErrorCode.VALIDATION_FAILED, f"attributes.{name}")
        if level not in definition.owner_levels:
            code = (
                ErrorCode.VALIDATION_FAILED if level == STANDALONE else ErrorCode.FACT_LEVEL_INVALID
            )
            raise InputError(code, f"attributes.{name}")
        if definition.key in built:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"attributes.{definition.key}")
        built[definition.key] = _envelope(definition, value, stamp)
    if jsonb_text_bytes(built) > FACT_MAP_MAX_BYTES:
        raise InputError(ErrorCode.VALIDATION_FAILED, "attributes")
    return built


def owned_at(defs: Sequence[AttributeDef], key: str, level: str) -> bool:
    """True when dictionary key `key` may sit on a row of `level` (its `owner_levels`)."""
    for definition in defs:
        if definition.key == key:
            return level in definition.owner_levels
    return False


def size_chart_fact(chart: Mapping[str, Any], *, now: datetime) -> dict[str, object]:
    """The `size_measurements` Fact: the normalized table and no `value` (`fact_map_valid`
    accepts a Fact with only `normalized`). A refused chart is 422 at `size_chart`."""
    try:
        table = normalize_size_chart(chart)
    except SizeChartError as exc:
        raise InputError(ErrorCode.VALIDATION_FAILED, "size_chart") from exc
    return {"normalized": table, **_seller(fact_stamp(now))}


def seller_column_fact(*, now: datetime) -> dict[str, object]:
    """The value-less seller provenance of a typed column (CP4: never the party's address)."""
    return _seller(fact_stamp(now))


def seller_provenance(*, now: datetime) -> dict[str, dict[str, object]]:
    stamp = fact_stamp(now)
    return {"title": _seller(stamp), "category": _seller(stamp)}
