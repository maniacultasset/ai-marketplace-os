"""Shared base models and string types for mkos-schemas."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Final

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


def walk_seam_dicts_and_text(root: BaseModel) -> None:
    """Reject forbidden secret key names in nested dicts and embedded JSON text."""

    def _walk_dict(d: dict[str, Any]) -> None:
        for key, value in d.items():
            if key in SEAM_SECRET_DENY_NAMES:
                raise ValidationError.from_exception_data(
                    root.__class__.__name__,
                    [
                        {
                            "type": "value_error",
                            "loc": ("__root__",),
                            "input": d,
                            "ctx": {"error": ValueError(f"forbidden seam key {key!r}")},
                        }
                    ],
                )
            if isinstance(value, dict):
                _walk_dict(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _walk_dict(item)
            elif isinstance(value, str):
                _walk_text(value)

    def _walk_text(text: str) -> None:
        stripped = text.strip()
        if not stripped.startswith("{"):
            return
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return
        if isinstance(parsed, dict):
            _walk_dict(parsed)

    data = root.model_dump(mode="python")
    _walk_dict(data)


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
