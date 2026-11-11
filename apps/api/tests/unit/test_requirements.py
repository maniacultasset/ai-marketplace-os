"""SI12 T20 (AC12): the marketplace needs per category, and the drift guards."""

from __future__ import annotations

import json
from pathlib import Path

import mkos_schemas
import pytest
from app.adapters.amazon import constants as amazon
from app.adapters.flipkart import constants as flipkart
from app.services.requirements import needs_for
from mkos_schemas.enums import Marketplace

DICTIONARY = Path(mkos_schemas.__file__).resolve().parent / "data" / "attribute_dictionary.json"
FLIPKART = Path(__file__).resolve().parents[2] / "app" / "adapters" / "flipkart"


def _keys() -> set[str]:
    data = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    return {row["key"] for row in data["attributes"]}


def test_tshirts_lists() -> None:
    template = ("color", "fabric_type")
    assert needs_for(Marketplace.AMAZON, "tshirts", template) == (
        "brand_name",
        "color",
        "country_of_origin",
        "fabric_type",
        "gender",
        "identifier",
        "manufacturer",
        "mrp",
        "package_dimensions",
        "package_weight",
        "selling_price",
    )
    assert needs_for(Marketplace.FLIPKART, "tshirts", template) == (
        "color",
        "country_of_origin",
        "fabric_type",
        "hsn_code",
        "manufacturer",
        "mrp",
        "package_dimensions",
        "package_weight",
        "selling_price",
    )


def test_union_is_sorted_and_deduped() -> None:
    got = needs_for(Marketplace.FLIPKART, "x_y", ("mrp", "hsn_code", "mrp"))
    assert list(got) == sorted(set(got)) and got.count("mrp") == 1


@pytest.mark.parametrize("category", ["unknown_slug", "home_decor_lamps"])
def test_every_category_extras_apply(category: str) -> None:
    for marketplace, module in ((Marketplace.AMAZON, amazon), (Marketplace.FLIPKART, flipkart)):
        got = set(needs_for(marketplace, category))
        assert set(module.REQUIRED_FACTS) <= got
        assert set(module.EXTRA_NEEDS[None]) <= got
    assert "gender" not in needs_for(Marketplace.AMAZON, "home_decor_lamps")


def _drift(entries: set[str]) -> set[str]:
    return entries - _keys()


def test_every_entry_is_a_dictionary_key() -> None:
    for module in (amazon, flipkart):
        entries = set(module.REQUIRED_FACTS)
        for extra in module.EXTRA_NEEDS.values():
            entries |= set(extra)
        assert _drift(entries) == set(), module.__name__


def test_the_drift_guard_catches_a_typo() -> None:
    # guard-must-fail: a misspelt row is not a dictionary key
    assert _drift({"identifer"}) == {"identifer"}


def test_flipkart_never_sends_the_extras() -> None:
    assert {"hsn", "package_length", "package_breadth", "package_height", "package_weight"} <= set(
        flipkart.FORBIDDEN_ATTRIBUTES
    )
    for path in FLIPKART.glob("*.py"):
        if path.name == "constants.py":
            continue
        assert "EXTRA_NEEDS" not in path.read_text(encoding="utf-8"), path.name
