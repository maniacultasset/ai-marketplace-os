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


def find_seam_secret_key(value: object) -> str | None:
    """Return the first forbidden key name found in ``value``, or ``None``.

    Walks dicts, lists and tuples at any depth. Every string is checked too: text that starts with
    ``{`` or ``[`` is parsed as JSON and walked the same way. JSON nested too deeply to parse is
    treated as forbidden (fail closed).
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
            stripped = node.strip()
            if not stripped.startswith(("{", "[")):
                continue
            try:
                stack.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
            except RecursionError:
                return "<json nested too deep>"
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
