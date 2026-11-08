"""The PTD schema check (DW2 T5; AC8, AC10, AC11): each mapped Listings payload against the stored
Amazon Product Type Definition. Pure: no HTTP, socket or database; the one I/O is the `load`
callable `prepare` is given (C6). The only module that imports `jsonschema_rs` (import contract
`jsonschema-only-in-amazon-schema-check`).

- `prepare(ref, load)`: a compiled validator from the LRU cache (16 entries, keyed by the schema
  digest), else `load(ref.definition_id)` once and compile. A schema that does not compile is
  returned as a failure handle and never cached, so the next call tries again.
- Compile: Draft 2019-09 with `$schema` stripped from a copy (the Amazon meta-schema URI is not
  resolvable offline), `offline=True` and no retriever (an external `$ref` fails to compile),
  formats off, every value masked, a fancy-regex backtrack limit, and Amazon's vocabulary
  keywords `maxUtf8ByteLength`, `minUtf8ByteLength` and `maxUniqueItems` (which reads its
  sibling `selectors`). `editable`, `hidden`, `enumNames` and `selectors` stay as annotations.
- `check_item(attributes, schema, index)`: the errors of one item. The field is every canonical
  path `index` gives for the Amazon attribute (so `generic_keyword` fails both keyword paths),
  else `listing`. Codes: required -> MISSING_ATTRIBUTE, enum/const -> VALUE_NOT_ALLOWED,
  maxLength/maxItems/maxUniqueItems -> FIELD_TOO_LONG, minLength/minItems/minUtf8ByteLength ->
  FIELD_TOO_SHORT, maxUtf8ByteLength -> BYTE_BUDGET_EXCEEDED, pattern -> PATTERN_MISMATCH,
  anything else (a regex that hit its backtrack limit included) -> SCHEMA_INVALID. The message
  is built from the schema, never from the library's message, and no item holds the submitted
  value. A missing required attribute is a WARNING when the definition is NOT_ENFORCED. At most
  `MAX_SCHEMA_ERRORS` items per payload, then one summary. Output is sorted.
- A schema that is not prepared, or did not compile, is one SCHEMA_INVALID on `listing`: the
  check fails closed.
- SI8 T5: `value_max_length(schema, attribute)` is the stored definition's
  `properties.<attribute>.items.properties.value.maxLength` (a positive int), else None.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final
from uuid import UUID

import jsonschema_rs
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, RetryClass, Severity
from mkos_schemas.errors import ErrorItem
from mkos_schemas.paths import is_valid_field_path

from app.adapters.amazon import constants as c
from app.adapters.base import ProductTypeSchema

CACHE_SIZE: Final = 16
BACKTRACK_LIMIT: Final = 100_000
MASK: Final = "<value>"
MAX_TITLE: Final = 80
RAW_CODE_MAX: Final = 128
_RETRYABLE: Final = frozenset({RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT})
_KEYWORD_RE: Final = re.compile(r"[A-Za-z0-9_]{1,64}")


# ---- Amazon vocabulary keywords ----


def _limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("the keyword's limit must be a non-negative integer")
    return value


class MaxUtf8ByteLength:
    """Fails a string longer than `value` bytes in UTF-8 (other instances pass)."""

    def __init__(self, parent_schema: dict[str, Any], value: Any, schema_path: list[Any]) -> None:
        self.limit = _limit(value)

    def validate(self, instance: Any) -> None:
        if isinstance(instance, str) and len(instance.encode("utf-8")) > self.limit:
            raise ValueError(str(self.limit))


class MinUtf8ByteLength:
    """Fails a string shorter than `value` bytes in UTF-8 (other instances pass)."""

    def __init__(self, parent_schema: dict[str, Any], value: Any, schema_path: list[Any]) -> None:
        self.limit = _limit(value)

    def validate(self, instance: Any) -> None:
        if isinstance(instance, str) and len(instance.encode("utf-8")) < self.limit:
            raise ValueError(str(self.limit))


class MaxUniqueItems:
    """Fails an array with more than `value` distinct entries. Two entries are the same when
    their sibling `selectors` values match (a missing key counts as None); with no selectors,
    when the whole entries match. Non-array instances pass."""

    def __init__(self, parent_schema: dict[str, Any], value: Any, schema_path: list[Any]) -> None:
        self.limit = _limit(value)
        selectors = parent_schema.get("selectors")
        self.selectors: tuple[str, ...] | None = (
            tuple(str(s) for s in selectors) if isinstance(selectors, list) else None
        )

    def _key(self, entry: Any) -> str:
        if self.selectors is not None:
            picked = [entry.get(s) if isinstance(entry, dict) else None for s in self.selectors]
            return json.dumps(picked, sort_keys=True, default=str)
        return json.dumps(entry, sort_keys=True, default=str)

    def validate(self, instance: Any) -> None:
        if isinstance(instance, list) and len({self._key(e) for e in instance}) > self.limit:
            raise ValueError(str(self.limit))


KEYWORDS: Final[Mapping[str, type[Any]]] = {
    "maxUtf8ByteLength": MaxUtf8ByteLength,
    "minUtf8ByteLength": MinUtf8ByteLength,
    "maxUniqueItems": MaxUniqueItems,
}


# ---- compile and cache ----


@dataclass(frozen=True)
class _Compiled:
    validator: Any
    titles: Mapping[str, str]
    max_lengths: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _CompileFailed:
    reason: str


_CACHE: OrderedDict[bytes, _Compiled] = OrderedDict()


def clear_cache() -> None:
    _CACHE.clear()


def _titles(schema: Mapping[str, Any]) -> dict[str, str]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    titles: dict[str, str] = {}
    for name, spec in properties.items():
        title = spec.get("title") if isinstance(spec, dict) else None
        if isinstance(title, str) and title.strip():
            titles[str(name)] = title.strip()[:MAX_TITLE]
    return titles


def _max_lengths(schema: Mapping[str, Any]) -> dict[str, int]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    out: dict[str, int] = {}
    for name, spec in properties.items():
        items = spec.get("items") if isinstance(spec, dict) else None
        props = items.get("properties") if isinstance(items, dict) else None
        value = props.get("value") if isinstance(props, dict) else None
        limit = value.get("maxLength") if isinstance(value, dict) else None
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            out[str(name)] = limit
    return out


def value_max_length(schema: ProductTypeSchema | None, attribute: str) -> int | None:
    """The stored definition's `maxLength` for `attribute`'s value; None when there is no
    schema, it is not prepared or did not compile, or it gives none."""
    handle = None if schema is None else schema.validator
    if not isinstance(handle, _Compiled):
        return None
    return handle.max_lengths.get(attribute)


def compile_schema(schema: Mapping[str, Any]) -> _Compiled | _CompileFailed:
    # shallow: a deeply nested (untrusted) schema must reach the library, which refuses it
    body = {k: v for k, v in schema.items() if k != "$schema"}
    try:
        validator = jsonschema_rs.Draft201909Validator(
            body,
            keywords=dict(KEYWORDS),
            offline=True,
            validate_formats=False,
            mask=MASK,
            pattern_options=jsonschema_rs.FancyRegexOptions(backtrack_limit=BACKTRACK_LIMIT),
        )
    except (ValueError, TypeError, RecursionError) as exc:  # SCHEMA_INVALID by every check
        return _CompileFailed(type(exc).__name__)
    return _Compiled(validator, _titles(body), _max_lengths(body))


async def prepare(
    ref: ProductTypeSchema, load: Callable[[UUID], Awaitable[Mapping[str, Any]]]
) -> ProductTypeSchema:
    """`ref` with its validator set: from the cache by digest, else loaded and compiled."""
    hit = _CACHE.get(ref.digest)
    if hit is not None:
        _CACHE.move_to_end(ref.digest)
        return replace(ref, validator=hit)
    compiled = compile_schema(await load(ref.definition_id))
    if isinstance(compiled, _Compiled):
        _CACHE[ref.digest] = compiled
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)
    return replace(ref, validator=compiled)


# ---- the check ----


def _item(
    path: str, code: ErrorCode, message: str, *, raw_code: str, severity: Severity
) -> ErrorItem:
    meta = ERROR_CODE_META[code]
    return ErrorItem(
        field=path if is_valid_field_path(path) else "listing",
        error_code=code,
        severity=severity,
        message=message[:1000],
        value_submitted=None,
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=meta.retry_class in _RETRYABLE,
        marketplace=Marketplace.AMAZON,
        raw_code=raw_code[:RAW_CODE_MAX],
        error_class=meta.error_class,
        retry_class=meta.retry_class,
    )


def _raw(keyword: str, schema: ProductTypeSchema) -> str:
    return f"ptd:{keyword}:{schema.product_type_version}"


def _schema_tail(schema: ProductTypeSchema) -> str:
    return f"Schema {schema.product_type} version {schema.product_type_version}."


def _unusable(schema: ProductTypeSchema, reason: str) -> list[ErrorItem]:
    return [
        _item(
            "listing",
            ErrorCode.SCHEMA_INVALID,
            f"The stored Amazon schema for {schema.product_type} could not be used ({reason}). "
            + _schema_tail(schema),
            raw_code=_raw("unusable", schema),
            severity=Severity.ERROR,
        )
    ]


def _custom_limit(kind: Any) -> str:
    message = str(kind.as_dict().get("message", ""))
    return message if message.isdigit() else "?"


def _classify(error: Any) -> tuple[str, ErrorCode, str]:
    """(keyword, code, rule text) from the error kind and the schema's limits only."""
    kind = error.kind
    kind_name = type(kind).__name__
    keyword = kind.name if isinstance(kind.name, str) and _KEYWORD_RE.fullmatch(kind.name) else "x"
    if kind_name == "Required":
        return "required", ErrorCode.MISSING_ATTRIBUTE, "is required"
    if kind_name in ("Enum", "Constant"):
        return keyword, ErrorCode.VALUE_NOT_ALLOWED, "the value is not one Amazon allows"
    if kind_name == "MaxLength":
        return keyword, ErrorCode.FIELD_TOO_LONG, f"is longer than {kind.limit} characters"
    if kind_name == "MinLength":
        return keyword, ErrorCode.FIELD_TOO_SHORT, f"is shorter than {kind.limit} characters"
    if kind_name == "MaxItems":
        return keyword, ErrorCode.FIELD_TOO_LONG, f"has more than {kind.limit} entries"
    if kind_name == "MinItems":
        return keyword, ErrorCode.FIELD_TOO_SHORT, f"needs at least {kind.limit} entries"
    if kind_name == "Pattern":
        return keyword, ErrorCode.PATTERN_MISMATCH, "does not match the pattern Amazon requires"
    if kind_name in ("BacktrackLimitExceeded", "RegexEngineFailure"):
        return keyword, ErrorCode.SCHEMA_INVALID, "could not be checked against Amazon's pattern"
    if kind_name == "Custom":
        limit = _custom_limit(kind)
        if keyword == "maxUtf8ByteLength":
            return keyword, ErrorCode.BYTE_BUDGET_EXCEEDED, f"is over {limit} bytes in UTF-8"
        if keyword == "minUtf8ByteLength":
            return keyword, ErrorCode.FIELD_TOO_SHORT, f"is under {limit} bytes in UTF-8"
        if keyword == "maxUniqueItems":
            return keyword, ErrorCode.FIELD_TOO_LONG, f"has more than {limit} distinct entries"
    return keyword, ErrorCode.SCHEMA_INVALID, f"breaks the Amazon schema rule {keyword}"


def _attribute(error: Any) -> str | None:
    path = list(error.instance_path)
    if path:
        return str(path[0])
    if type(error.kind).__name__ == "Required":
        return str(error.kind.property)
    return None


def _iter_errors(validator: Any, attributes: Mapping[str, Any]) -> tuple[list[Any], bool]:
    """Every validation error, and whether the run itself failed (reported, never raised)."""
    try:
        return list(validator.iter_errors(dict(attributes))), False
    except ValueError:  # the library refused the run: one SCHEMA_INVALID, never a crash
        return [], True


def check_item(
    attributes: Mapping[str, Any],
    schema: ProductTypeSchema,
    index: Mapping[str, tuple[str, ...]],
    *,
    titles: Mapping[str, str] | None = None,
    reported: Collection[str] = (),
) -> list[ErrorItem]:
    """The schema errors of one item's `attributes`. `reported` holds the canonical paths the
    template check already reported missing: a schema `required` error on an attribute fed only
    by those paths is left out, so it is reported once."""
    handle = schema.validator
    if handle is None:
        return _unusable(schema, "not prepared")
    if isinstance(handle, _CompileFailed):
        return _unusable(schema, handle.reason)
    if not isinstance(handle, _Compiled):
        return _unusable(schema, "unknown handle")
    named = titles if titles is not None else handle.titles
    found, failed = _iter_errors(handle.validator, attributes)
    if failed:
        return _unusable(schema, "validation failed")
    rows: list[tuple[str, str, str, ErrorItem]] = []
    for problem in found:
        attribute = _attribute(problem)
        keyword, code, rule = _classify(problem)
        paths = index.get(attribute, ()) if attribute is not None else ()
        if code is ErrorCode.MISSING_ATTRIBUTE and paths and set(paths) <= set(reported):
            continue
        severity = Severity.ERROR
        if code is ErrorCode.MISSING_ATTRIBUTE and not schema.enforced:
            severity = Severity.WARNING
        label = attribute if attribute is not None else "listing"
        title = named.get(label)
        subject = f"Amazon {label} ({title})" if title else f"Amazon {label}"
        message = f"{subject}: {rule}. {_schema_tail(schema)}"
        for path in paths or ("listing",):
            item = _item(path, code, message, raw_code=_raw(keyword, schema), severity=severity)
            rows.append((item.field, code.value, label, item))
    rows.sort(key=lambda row: row[:3])
    items = [row[3] for row in rows]
    if len(items) <= c.MAX_SCHEMA_ERRORS:
        return items
    kept, rest = items[: c.MAX_SCHEMA_ERRORS], items[c.MAX_SCHEMA_ERRORS :]
    blocking = any(i.severity is Severity.ERROR for i in rest)
    summary = _item(
        "listing",
        ErrorCode.SCHEMA_INVALID,
        f"{len(rest)} more schema errors. {_schema_tail(schema)}",
        raw_code=_raw("summary", schema),
        severity=Severity.ERROR if blocking else Severity.WARNING,
    )
    return [*kept, summary]
