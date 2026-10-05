"""Shared base models and string types for mkos-schemas."""

from __future__ import annotations

import json
import re
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION: Final = "1.0.0"

SEAM_SECRET_DENY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "refresh_token",
        "access_token",
        "client_secret",
        "api_key",
        "authorization",
        "password",
        "secret",
    }
)


SEAM_REDACTED: Final = "<redacted>"

SEAM_JSON_SCAN_MAX_ATTEMPTS: Final = 64
"""Failed decode attempts per string when looking for JSON embedded in text; past it the walk fails
closed. Successful decodes move the scan past the value, so they cost nothing."""

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalize_seam_key(key: str) -> str:
    """``apiKey``, ``API_KEY``, ``X-Api-Key`` and ``api_key`` all normalize to snake_case."""
    return _CAMEL_BOUNDARY.sub("_", key).casefold().replace("-", "_")


def is_seam_secret_key(key: str) -> bool:
    """True when ``key`` equals a deny name or ends with ``_<deny name>`` after normalizing.

    Case-insensitive; a plain substring is not enough (``secret_sauce_flavor`` and ``token_count``
    stay legal), but ``lwa_client_secret`` and ``x_api_key`` are refused.
    """
    norm = normalize_seam_key(key)
    return any(norm == name or norm.endswith("_" + name) for name in SEAM_SECRET_DENY_NAMES)


_JSON_DECODER: Final = json.JSONDecoder()

# Only positions where a JSON value worth walking can really start cost a decode attempt: ``{``
# must be followed by a key or ``}``, ``[`` by a value or ``]``, and ``"`` (a JSON string literal,
# i.e. double-encoded JSON) by ``{``, ``[`` or an escaped quote. Template braces such as ``{name}``,
# prose such as ``[see note]`` and plain quoted words are skipped for free.
_JSON_START = re.compile(r'\{\s*["}]|\[\s*[\]"{\[\-0-9tfnNI]|"(?=\s*[{\[]|\\")')
_ESCAPED_QUOTE: Final = '\\"'

_JSON_TOO_DEEP: Final = "<json nested too deep>"
_JSON_SCAN_LIMIT: Final = "<json scan limit reached>"


class _ScanBudget:
    """Failed decode attempts shared by every pass over one string."""

    def __init__(self) -> None:
        self.failures = 0


def _scan_json_values(text: str, budget: _ScanBudget, found: list[object]) -> str | None:
    """Append every JSON object, array or JSON string literal found in ``text`` to ``found``.

    Returns a fail-closed marker when nesting is too deep or the failure budget is spent.
    """
    pos = 0
    while (match := _JSON_START.search(text, pos)) is not None:
        start = match.start()
        try:
            value, end = _JSON_DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            if budget.failures >= SEAM_JSON_SCAN_MAX_ATTEMPTS:
                return _JSON_SCAN_LIMIT
            budget.failures += 1
            pos = start + 1
            continue
        except RecursionError:
            return _JSON_TOO_DEEP
        found.append(value)
        pos = end
    return None


def _decode_json_in_text(text: str) -> tuple[list[object], str | None]:
    """Decode every JSON value found in ``text``: the whole text as a JSON string literal
    (double-encoded JSON), each object or array embedded anywhere in it, and each JSON string
    literal holding JSON (double-encoded JSON after a prefix or before trailing text). Decoded
    strings are walked again by the caller. When the text holds escaped quotes (``{\\"key\\": ...}``
    without its enclosing quotes), it is scanned once more with them unescaped.

    Returns the decoded values, plus a marker when the walk must fail closed (nesting too deep to
    parse, or more than ``SEAM_JSON_SCAN_MAX_ATTEMPTS`` failed decode attempts).
    """
    stripped = text.strip().lstrip("\ufeff").strip()
    if stripped.startswith('"'):
        try:
            literal = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        except RecursionError:
            return [], _JSON_TOO_DEEP
        else:
            if isinstance(literal, str):
                return [literal], None
    found: list[object] = []
    budget = _ScanBudget()
    fail_closed = _scan_json_values(stripped, budget, found)
    if fail_closed is None and _ESCAPED_QUOTE in stripped:
        fail_closed = _scan_json_values(stripped.replace(_ESCAPED_QUOTE, '"'), budget, found)
    return found, fail_closed


def find_seam_secret_key(value: object) -> str | None:
    """Return the first forbidden key name found in ``value``, or ``None``.

    Walks dicts, lists and tuples at any depth. Every string is checked too: a JSON object or
    array anywhere in the text (after a prefix, after a BOM) and a whole-text JSON string literal
    (double-encoded JSON) are decoded and walked the same way. JSON nested too deeply to parse,
    or text with more JSON candidates than the scan allows, is treated as forbidden (fail closed).
    """
    stack: list[object] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(key, str) and is_seam_secret_key(key):
                    return key
                stack.append(item)
        elif isinstance(node, list | tuple):
            stack.extend(node)
        elif isinstance(node, str):
            decoded, fail_closed = _decode_json_in_text(node)
            if fail_closed is not None:
                return fail_closed
            stack.extend(decoded)
    return None


def walk_seam_dicts_and_text(root: BaseModel) -> None:
    """Reject forbidden secret key names anywhere in ``root``'s dump (AC 17).

    The error never carries the offending value: ``input`` is always ``<redacted>``.
    """
    key = find_seam_secret_key(root.model_dump(mode="python"))
    if key is None:
        return
    raise ValidationError.from_exception_data(
        root.__class__.__name__,
        [
            {
                "type": "value_error",
                "loc": ("__root__",),
                "input": SEAM_REDACTED,
                "ctx": {"error": ValueError(f"forbidden seam key {key[:64]!r}")},
            }
        ],
    )


class StrictModel(BaseModel):
    """Inbound HTTP bodies: unknown keys are rejected."""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        allow_inf_nan=False,
        protected_namespaces=(),
        str_strip_whitespace=True,
    )


class WireModel(BaseModel):
    """Cross-team payloads: unknown keys are dropped, never re-emitted."""

    model_config = ConfigDict(
        extra="ignore",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        allow_inf_nan=False,
        protected_namespaces=(),
        ser_json_timedelta="iso8601",
    )


def _reject_blank(value: str) -> str:
    if value.strip() == "":
        raise ValueError("must not be empty or whitespace-only")
    return value


NonBlankStr = Annotated[str, Field(min_length=1), AfterValidator(_reject_blank)]

Slug = Annotated[str, Field(pattern=r"^[a-z0-9_]{2,64}$")]
AttrKey = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")]
RequestId = Annotated[NonBlankStr, Field(max_length=128)]
