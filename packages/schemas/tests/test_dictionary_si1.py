"""SI1 T4 (decision 13; §6 row D): the `fabric_composition` alias of `fiber_composition` and the
new `size_chart_url` key (style or standalone, text, seller/document/pim)."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import pytest

BEFORE_SI1_KEYS = 86


@pytest.fixture(scope="module")
def attributes() -> list[dict[str, Any]]:
    text = resources.files("mkos_schemas").joinpath("data/attribute_dictionary.json").read_text()
    data: dict[str, Any] = json.loads(text)
    rows: list[dict[str, Any]] = data["attributes"]
    return rows


def _entry(attributes: list[dict[str, Any]], key: str) -> dict[str, Any]:
    (entry,) = [a for a in attributes if a["key"] == key]
    return entry


def _resolve(attributes: list[dict[str, Any]], name: str) -> str | None:
    """The build rule: an exact key beats an alias."""
    for a in attributes:
        if a["key"] == name:
            return str(a["key"])
    for a in attributes:
        if name in a["input_aliases"]:
            return str(a["key"])
    return None


def test_fabric_composition_alias_resolves(attributes: list[dict[str, Any]]) -> None:
    assert _resolve(attributes, "fabric_composition") == "fiber_composition"
    assert _entry(attributes, "fiber_composition")["input_aliases"] == ["fabric_composition"]


def test_size_chart_url_levels(attributes: list[dict[str, Any]]) -> None:
    entry = _entry(attributes, "size_chart_url")
    assert entry["owner_levels"] == ["style", "standalone"]
    assert entry == {
        "key": "size_chart_url",
        "section": "attributes",
        "storage": "attributes",
        "owner_levels": ["style", "standalone"],
        "value_type": "text",
        "unit": None,
        "vocabulary": None,
        "input_aliases": [],
        "is_critical": False,
        "allowed_sources": ["seller", "document", "pim"],
        "min_confidence": None,
        "confirm_agent_captures": False,
        "brd_ref": "A.6 size_chart",
        "enabled": True,
        "columns": [],
    }


def test_key_count_up_by_one(attributes: list[dict[str, Any]]) -> None:
    assert len(attributes) == BEFORE_SI1_KEYS + 1


def test_no_alias_collides_with_a_key(attributes: list[dict[str, Any]]) -> None:
    keys = {a["key"] for a in attributes}
    aliases = [alias for a in attributes for alias in a["input_aliases"]]
    assert not keys & set(aliases)
    assert len(aliases) == len(set(aliases))
