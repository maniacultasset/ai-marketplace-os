"""SI12 T13 (AC10): the HSN heading suggested from the category."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.services.hsn_suggest import HSN_BY_CATEGORY, HsnSuggestion, suggest_hsn
from app.services.intake_values import HSN_PATTERN

TEMPLATES = (
    Path(__file__).resolve().parents[2] / "app" / "db" / "seed_data" / "platform_templates.json"
)


@pytest.mark.parametrize(
    ("category", "heading"),
    [("tshirts", "6109"), ("ethnic_wear_kurta_sets", "6204"), ("home_decor_lamps", "9405")],
)
def test_seeded_categories(category: str, heading: str) -> None:
    assert suggest_hsn(category, None) == HsnSuggestion(value=heading, basis="category")


@pytest.mark.parametrize("category", ["kurtas", "t_shirts", "", None, "TSHIRTS"])
def test_unseeded_or_empty(category: str | None) -> None:
    assert suggest_hsn(category, None) is None


def test_no_suggestion_once_set() -> None:
    assert suggest_hsn("tshirts", "6109") is None
    assert suggest_hsn("tshirts", "61091000") is None


def test_every_key_is_a_seeded_category() -> None:
    rows = json.loads(TEMPLATES.read_text(encoding="utf-8"))
    seeded = {row["template"]["category"] for row in rows}
    assert set(HSN_BY_CATEGORY) <= seeded
    assert set(HSN_BY_CATEGORY) == seeded  # every seeded category has a reviewed heading


def test_every_value_is_a_4_digit_heading() -> None:
    for value in HSN_BY_CATEGORY.values():
        assert HSN_PATTERN.fullmatch(value) and len(value) == 4


def test_map_is_read_only() -> None:
    with pytest.raises(TypeError):
        HSN_BY_CATEGORY["x"] = "1234"  # type: ignore[index]
