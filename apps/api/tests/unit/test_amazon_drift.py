"""DW2 T6 (AC12): believed wire names against a stored definition (pure)."""

from __future__ import annotations

import json
import random
from typing import Any

from app.adapters.amazon.drift import Drift, believed_names, diff, render
from app.db.seed_templates import seed_templates
from tests.support.fake_marketplace import ptd_fixture


def _schema(name: str = "KURTA.schema.json") -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(ptd_fixture(name))
    return loaded


def _templates(product_type: str) -> list[Any]:
    return [s.template for s in seed_templates() if s.product_type == product_type]


def test_matching_fixture_has_no_drift() -> None:
    assert diff(_schema(), _templates("KURTA"), "KURTA") == []
    assert diff(_schema("SHIRT.schema.json"), _templates("SHIRT"), "SHIRT") == []


def test_renamed_attribute() -> None:
    schema = _schema()
    schema["properties"]["neckline_style"] = schema["properties"].pop("neck_style")
    assert diff(schema, _templates("KURTA"), "KURTA") == [
        Drift("unknown_attribute", "neck_style", "neckline -> not declared by KURTA")
    ]


def test_value_map_target_outside_enum() -> None:
    schema = _schema()
    enum = schema["properties"]["color"]["items"]["properties"]["value"]["enum"]
    enum.remove("Navy")
    (drift,) = diff(schema, _templates("KURTA"), "KURTA")
    assert (drift.kind, drift.attribute) == ("unknown_value", "color")
    assert "Navy" in drift.detail and f"{len(enum)} allowed" in drift.detail
    for other in enum:
        assert other not in drift.detail


def test_drift_fixture_reports_rename_and_unfed_item_type_keyword() -> None:
    """SI8: brand is fed now; the drift fixture's unfed required name is `item_type_keyword`."""
    drifts = diff(_schema("KURTA.drift.schema.json"), _templates("KURTA"), "KURTA")
    assert {(d.kind, d.attribute) for d in drifts} == {
        ("unknown_attribute", "neck_style"),
        ("unfed_required", "item_type_keyword"),
    }


# ---- SI8 T6 (AC8): the identity and audience names ----


def test_listing_fixtures_have_no_drift() -> None:
    for product_type in ("KURTA", "SHIRT"):
        schema = _schema(f"{product_type}.listing.schema.json")
        assert diff(schema, _templates(product_type), product_type) == []


def test_audience_value_outside_the_enum() -> None:
    schema = _schema()
    enum = schema["properties"]["target_gender"]["items"]["properties"]["value"]["enum"]
    enum.remove("female")
    (drift,) = diff(schema, _templates("KURTA"), "KURTA")
    assert (drift.kind, drift.attribute) == ("unknown_value", "target_gender")
    assert "female" in drift.detail


def test_apparel_schema_without_an_audience_name_drifts() -> None:
    schema = _schema("SHIRT.schema.json")
    del schema["properties"]["department"]
    assert diff(schema, _templates("SHIRT"), "SHIRT") == [
        Drift("unknown_attribute", "department", "not declared by SHIRT")
    ]


def test_brand_missing_from_a_schema_drifts() -> None:
    schema = _schema()
    del schema["properties"]["brand"]
    schema["required"].remove("brand")
    assert diff(schema, _templates("KURTA"), "KURTA") == [
        Drift("unknown_attribute", "brand", "not declared by KURTA")
    ]


def test_lamp_without_target_gender_reports_nothing_for_it() -> None:
    schema = _schema("LAMP.schema.json")
    assert "target_gender" not in schema["properties"]
    drifts = diff(schema, _templates("LAMP"), "LAMP")
    assert [(d.kind, d.attribute) for d in drifts] == [("shape", "item_dimensions")]
    schema["required"] = [*schema["required"], "target_gender"]
    assert ("unfed_required", "target_gender") in {
        (d.kind, d.attribute) for d in diff(schema, _templates("LAMP"), "LAMP")
    }


def test_structured_dimensions_are_a_shape_drift() -> None:
    drifts = diff(_schema("LAMP.schema.json"), _templates("LAMP"), "LAMP")
    assert [(d.kind, d.attribute) for d in drifts] == [("shape", "item_dimensions")]
    assert "height, length, width" in drifts[0].detail


def test_render_sorted_and_stable() -> None:
    drifts = diff(_schema("KURTA.drift.schema.json"), _templates("KURTA"), "KURTA")
    shuffled = list(drifts)
    random.Random(7).shuffle(shuffled)  # noqa: S311 - a test shuffle
    assert render(drifts) == render(shuffled) == render(list(reversed(drifts)))
    assert render(drifts).splitlines() == sorted(render(drifts).splitlines())
    assert render([]) == ""


def test_no_properties_reports_every_believed_name() -> None:
    cases: list[dict[str, Any]] = [{}, {"properties": []}, {"properties": "x"}]
    for broken in cases:
        drifts = diff(broken, _templates("KURTA"), "KURTA")
        names = {d.attribute for d in drifts if d.kind == "unknown_attribute"}
        assert set(believed_names()) <= names


def test_product_type_without_a_template_checks_constants_only() -> None:
    schema = {"properties": {name: {} for name in believed_names()}, "required": ["luggage_size"]}
    assert diff(schema, _templates("LUGGAGE"), "LUGGAGE") == [
        Drift("unfed_required", "luggage_size", "required, and nothing here sends it")
    ]


def test_si2_listing_fixture_has_no_drift_and_catches_a_lost_name() -> None:
    """SI2 T18: the LISTING definition covers the new names (`material`, `manufacturer`, the
    composition `fabric_type`, the `country_of_origin` enum); a removed one is reported."""
    schema = _schema("KURTA.listing.schema.json")
    assert diff(schema, _templates("KURTA"), "KURTA") == []
    assert diff(_schema("SHIRT.listing.schema.json"), _templates("SHIRT"), "SHIRT") == []
    del schema["properties"]["material"]
    found = diff(schema, _templates("KURTA"), "KURTA")
    assert [d.attribute for d in found] == ["material"]
