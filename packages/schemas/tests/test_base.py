"""Tests for mkos_schemas.base."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from mkos_schemas.base import (
    AttrKey,
    NonBlankStr,
    Slug,
    StrictModel,
    WireModel,
)
from pydantic import Field, ValidationError


class _StrictProbe(StrictModel):
    name: str
    alias_field: str | None = Field(default=None, alias="aliasField")


class _WireProbe(WireModel):
    name: str
    alias_field: str | None = Field(default=None, alias="aliasField")


def test_base_extra_strict_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError) as exc:
        _StrictProbe.model_validate({"name": "x", "extra": 1})
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_base_extra_wire_drops_unknown_key_and_never_reemits() -> None:
    m = _WireProbe.model_validate({"name": "x", "extra": 1})
    dumped = m.model_dump()
    assert "extra" not in dumped
    assert json.loads(m.model_dump_json()) == dumped


def test_base_alias_validates_by_name_and_alias() -> None:
    m = _StrictProbe.model_validate({"name": "n", "aliasField": "a"})
    assert m.alias_field == "a"
    m2 = _StrictProbe.model_validate({"name": "n", "alias_field": "b"})
    assert m2.alias_field == "b"


def test_base_serialize_by_alias_is_default() -> None:
    m = _StrictProbe(name="n", alias_field="a")
    assert "aliasField" in m.model_dump()
    assert "alias_field" not in m.model_dump()


def test_base_rejects_nan_and_infinity() -> None:
    class FStrict(StrictModel):
        x: float

    class FWire(WireModel):
        x: float

    for base in (FStrict, FWire):
        for bad in (float("nan"), float("inf"), float("-inf"), Decimal("NaN")):
            with pytest.raises(ValidationError):
                base.model_validate({"x": bad})
        with pytest.raises(ValidationError):
            base.model_validate_json('{"x": NaN}')


def test_base_strict_strips_whitespace_wire_does_not() -> None:
    class S(StrictModel):
        t: str

    class W(WireModel):
        t: str

    assert S.model_validate({"t": "  a  "}).t == "a"
    assert W.model_validate({"t": "  a  "}).t == "  a  "


def test_nonblank_rejects_empty_and_whitespace_only() -> None:
    class M(StrictModel):
        t: NonBlankStr

    for bad in ("", " ", "\t\n", "\u00a0", "\u3000"):
        with pytest.raises(ValidationError):
            M.model_validate({"t": bad})


def test_nonblank_keeps_unicode_unchanged() -> None:
    class M(StrictModel):
        t: NonBlankStr

    assert M.model_validate({"t": "हिन्दी"}).t == "हिन्दी"
    assert M.model_validate({"t": "👨‍👩‍👧‍👦"}).t == "👨‍👩‍👧‍👦"
    assert M.model_validate({"t": "\u200b"}).t == "\u200b"


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("ab", True),
        ("a" * 64, True),
        ("a", False),
        ("a" * 65, False),
        ("Ab", False),
        ("a-b", False),
        ("١٢", False),
    ],
)
def test_slug_boundaries(value: str, ok: bool) -> None:
    class M(StrictModel):
        s: Slug

    if ok:
        M.model_validate({"s": value})
    else:
        with pytest.raises(ValidationError):
            M.model_validate({"s": value})


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("a", True),
        ("a" * 64, True),
        ("", False),
        ("a" * 65, False),
        ("A", False),
        ("a.b", False),
    ],
)
def test_attr_key_boundaries(value: str, ok: bool) -> None:
    class M(StrictModel):
        k: AttrKey

    if ok:
        M.model_validate({"k": value})
    else:
        with pytest.raises(ValidationError):
            M.model_validate({"k": value})


def test_json_schema_additional_properties() -> None:
    strict = _StrictProbe.model_json_schema()
    wire = _WireProbe.model_json_schema()
    assert strict.get("additionalProperties") is False
    assert "additionalProperties" not in wire
