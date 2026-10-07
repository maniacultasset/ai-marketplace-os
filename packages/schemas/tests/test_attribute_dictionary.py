"""The attribute dictionary data file (S13 Q6; DATABASE.md:1152-1179): its shape, and the A.6
attribute and care columns it must name. Loaded by the migrator's `app.cli dictionary sync`."""

from __future__ import annotations

import json
import re
import zipfile
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

KEY = re.compile(r"[a-z][a-z0-9_]{1,63}")
LEVELS = {"brand", "style", "colour", "variant", "standalone"}
SOURCES = {"seller", "document", "pim", "marketplace", "derived", "ai"}
SECTIONS = {
    "identity", "classification", "attributes", "care", "compliance", "commercial", "dimensions",
}  # fmt: skip
VALUE_TYPES = {
    "text", "text_list", "enum", "enum_list", "number", "integer", "boolean", "money", "measure",
    "composition", "object", "party",
}  # fmt: skip
FIELDS = {
    "key", "section", "storage", "owner_levels", "value_type", "unit", "vocabulary",
    "input_aliases", "is_critical", "allowed_sources", "min_confidence",
    "confirm_agent_captures", "brd_ref", "enabled", "columns",
}  # fmt: skip
# A.6 attribute and care columns (DATABASE.md:2192-2193) → their dictionary keys.
A6_ATTRIBUTE_KEYS = {
    "color_name": "color", "size_name": "size", "size_system": "size_system", "fit": "fit",
    "pattern": "pattern", "fabric_type": "fabric_type", "fiber_composition": "fiber_composition",
    "lining": "lining", "stretch": "stretch", "neckline": "neckline", "collar_type": "collar_type",
    "sleeve_type": "sleeve_type", "sleeve_length": "sleeve_length",
    "garment_length": "garment_length", "rise": "rise", "leg_style": "leg_style",
    "closure": "closure", "pocket_type": "pocket_type", "occasion": "occasion", "season": "season",
    "wash_care": "wash_care", "iron_care": "iron_care",
}  # fmt: skip


@pytest.fixture(scope="module")
def dictionary() -> dict[str, Any]:
    text = resources.files("mkos_schemas").joinpath("data/attribute_dictionary.json").read_text()
    data: dict[str, Any] = json.loads(text)
    return data


def test_attribute_dictionary_shape(dictionary: dict[str, Any]) -> None:
    assert set(dictionary) == {"dictionary_version", "attributes", "category_attributes"}
    assert dictionary["dictionary_version"] >= 1
    keys = [entry["key"] for entry in dictionary["attributes"]]
    assert len(keys) == len(set(keys))
    columns: list[tuple[str, str]] = []
    for entry in dictionary["attributes"]:
        assert set(entry) == FIELDS, entry["key"]
        assert KEY.fullmatch(entry["key"])
        assert entry["section"] in SECTIONS
        assert entry["value_type"] in VALUE_TYPES
        assert entry["owner_levels"] and set(entry["owner_levels"]) <= LEVELS
        assert entry["allowed_sources"] and set(entry["allowed_sources"]) <= SOURCES
        if entry["is_critical"]:
            assert "ai" not in entry["allowed_sources"] and entry["confirm_agent_captures"]
        assert (entry["storage"] == "column") == bool(entry["columns"]), entry["key"]
        for ref in entry["columns"]:
            assert ref["table"] in {"products", "brand_profiles"}
            columns.append((ref["table"], ref["column"]))
    assert len(columns) == len(set(columns)), "a typed column maps to two keys"
    aliases = [a for entry in dictionary["attributes"] for a in entry["input_aliases"]]
    assert len(aliases) == len(set(aliases)) and not set(aliases) & set(keys)


def test_attribute_dictionary_keys_match_a6_attribute_columns(dictionary: dict[str, Any]) -> None:
    by_key = {entry["key"]: entry for entry in dictionary["attributes"]}
    for column, key in A6_ATTRIBUTE_KEYS.items():
        assert key in by_key, column
        assert by_key[key]["storage"] == "attributes", key
        assert by_key[key]["enabled"], key
    assert by_key["fabric_type"]["input_aliases"] == ["material"]
    for key in ("title", "category", "mrp", "selling_price", "hsn_code", "manufacturer"):
        assert by_key[key]["storage"] == "column", key


def test_attribute_dictionary_ships_in_the_wheel(tmp_path: Path) -> None:
    import subprocess

    root = Path(__file__).resolve().parents[1]
    built = subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(tmp_path), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr[-2000:]
    (wheel,) = tmp_path.glob("mkos_schemas-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        assert "mkos_schemas/data/attribute_dictionary.json" in archive.namelist()
