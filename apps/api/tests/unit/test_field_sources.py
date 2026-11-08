"""SI12 T12 (AC11): where each field's value came from, as a pure function of the envelopes."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.services.field_sources import FieldSource, field_sources

IMAGE = uuid.UUID("8a3f5c2e-1d4b-4e6a-9b7c-0d2e4f6a8b13")
AT = "2026-11-08T00:00:00Z"


def _fact(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "value": "x",
        "source": "seller",
        "captured_via": "api",
        "updated_at": AT,
    }
    base.update(over)
    return base


def _one(fact: dict[str, Any], key: str = "color", **inherited: bool) -> FieldSource | None:
    return field_sources({key: fact}, inherited=inherited).get(key)


@pytest.mark.parametrize(
    ("fact", "kind"),
    [
        (_fact(captured_via="import"), "sheet"),
        (_fact(), "you"),
        (_fact(captured_via="form"), "you"),
        (_fact(captured_via="human_task"), "you"),
        (_fact(source="document", evidence_ref="document:abc"), "you"),
    ],
)
def test_seller_kinds(fact: dict[str, Any], kind: Any) -> None:
    assert _one(fact) == FieldSource(kind=kind)


def test_size_chart_from_photo() -> None:
    fact = _fact(value=None, normalized={"unit": "cm"}, evidence_ref=f"image:{IMAGE}")
    assert _one(fact, "size_measurements") == FieldSource(kind="size_chart", image_id=IMAGE)


def test_size_chart_without_evidence_is_you() -> None:
    fact = _fact(value=None, normalized={"unit": "cm"})
    assert _one(fact, "size_measurements") == FieldSource(kind="you")


def test_image_evidence_on_another_seller_key_is_you() -> None:
    assert _one(_fact(evidence_ref=f"image:{IMAGE}")) == FieldSource(kind="you")


def test_unconfirmed_photo_value() -> None:
    fact = _fact(source="ai", captured_via="agent", evidence_ref=f"image:{IMAGE}", confidence=0.82)
    assert _one(fact) == FieldSource(kind="photo", image_id=IMAGE, confidence=0.82)


def test_confirmed_photo_value() -> None:
    fact = _fact(source="ai", captured_via="agent", evidence_ref=f"image:{IMAGE}", confirmed_at=AT)
    assert _one(fact) == FieldSource(kind="confirmed", image_id=IMAGE)


@pytest.mark.parametrize(
    "fact",
    [
        _fact(source="ai", captured_via="agent"),
        _fact(source="ai", evidence_ref="document:1"),
        _fact(source="pim"),
        _fact(source="system"),
        _fact(source="derived"),
        _fact(source="marketplace"),
        _fact(source="who-knows"),
        _fact(source=None),
        _fact(value=None),
        _fact(value=None, normalized=None),
    ],
)
def test_absent(fact: dict[str, Any]) -> None:
    assert _one(fact) is None


def test_malformed_image_evidence_keeps_the_kind() -> None:
    photo = _fact(source="ai", evidence_ref="image:not-a-uuid", confidence=0.5)
    assert _one(photo) == FieldSource(kind="photo", confidence=0.5)
    chart = _fact(value=None, normalized={"unit": "cm"}, evidence_ref="image:nope")
    assert _one(chart, "size_measurements") == FieldSource(kind="size_chart")


@pytest.mark.parametrize("confidence", [float("nan"), "0.9", True, 1.5])
def test_bad_confidence_dropped(confidence: Any) -> None:
    fact = _fact(source="ai", evidence_ref=f"image:{IMAGE}", confidence=confidence)
    assert _one(fact) == FieldSource(kind="photo", image_id=IMAGE)


@pytest.mark.parametrize(
    ("key", "flag"),
    [
        ("manufacturer", "manufacturer_inherited"),
        ("brand_name", "brand_name_inherited"),
        ("country_of_origin", "country_of_origin_from_brand"),
    ],
)
def test_brand_inherited(key: str, flag: str) -> None:
    assert field_sources({}, inherited={flag: True}) == {key: FieldSource(kind="brand")}
    # the flag wins over any stored envelope (the value shown is the brand's)
    assert _one(_fact(), key, **{flag: True}) == FieldSource(kind="brand")
    assert _one(_fact(), key, **{flag: False}) == FieldSource(kind="you")


def test_empty() -> None:
    assert field_sources({}, inherited={}) == {}


def test_non_mapping_envelope_ignored() -> None:
    assert field_sources({"color": "red", "fit": None}, inherited={}) == {}
