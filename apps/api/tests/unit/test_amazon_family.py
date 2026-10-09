"""S20 T7 (AC6; ADR-0007 §3; T45; C10): a size x colour family is one adapter operation."""

from __future__ import annotations

import dataclasses

import pytest
from tests.adapters.amazon_support import amazon_family, template


def _codes(sent: object) -> list[tuple[str, str]]:
    from app.adapters.amazon.mapping import check

    return [(e.field, e.error_code.value) for e in check(sent)]  # type: ignore[arg-type]


def test_amazon_family_payload() -> None:
    from app.adapters.amazon.mapping import check, family_payloads

    sent = amazon_family()
    items = family_payloads(sent)
    assert check(sent) == []
    assert [i.unit.item_role for i in items] == ["parent", "child", "child", "child", "child"]
    parent = items[0].payload["attributes"]
    assert parent["variation_theme"] == [{"name": "SIZE/COLOR"}]
    assert parent["parentage_level"][0]["value"] == "parent"
    assert "size" not in parent and "color" not in parent  # the children carry the axes
    for child in items[1:]:
        attrs = child.payload["attributes"]
        assert attrs["child_parent_sku_relationship"][0]["parent_sku"] == sent.seller_sku
        assert attrs["parentage_level"][0]["value"] == "child"
        assert attrs["color"][0]["value"] in {"Navy", "Maroon"}
        assert attrs["size"][0]["value"] in {"S", "M"}
        assert child.payload["productType"] == "KURTA"
    colors = [i.payload["attributes"]["color"][0]["value"] for i in items[1:]]
    assert colors == ["Navy", "Maroon", "Navy", "Maroon"]


@pytest.mark.parametrize(
    ("theme", "axis", "amazon"), [("size", "size", "SIZE"), ("color", "color", "COLOR")]
)
def test_amazon_family_single_axis(theme: str, axis: str, amazon: str) -> None:
    from app.adapters.amazon.mapping import check, family_payloads

    values = {"size": ["S", "M"], "color": ["navy blue", "maroon"]}[axis]
    sent = amazon_family(theme=theme, children=[{axis: v} for v in values])
    assert check(sent) == []
    items = family_payloads(sent)
    assert items[0].payload["attributes"]["variation_theme"] == [{"name": amazon}]
    for child in items[1:]:
        assert set(child.unit.variant_attributes) == {axis}


def test_amazon_family_unknown_axis_invalid() -> None:
    assert _codes(amazon_family(theme="pattern")) == [("variants", "VARIATION_INVALID")]


def test_amazon_family_no_children_invalid() -> None:
    sent = amazon_family()
    lone = dataclasses.replace(sent, items=sent.items[:1])
    assert ("variants", "VARIATION_INVALID") in _codes(lone)


def test_amazon_family_axis_mismatch_invalid() -> None:
    sent = amazon_family(children=[{"size": "S", "color": "maroon"}, {"size": "M"}])
    assert ("variants", "VARIATION_INVALID") in _codes(sent)


def test_amazon_family_theme_not_in_template_invalid() -> None:
    from app.adapters.base import AdapterTemplate

    t = template()
    only_size = AdapterTemplate(
        t.product_type, None, t.template.model_copy(update={"variation_themes": [["size"]]})
    )
    assert _codes(amazon_family(template=only_size)) == [("variants", "VARIATION_INVALID")]


def test_amazon_family_child_value_not_allowed() -> None:
    sent = amazon_family(children=[{"size": "S", "color": "ultraviolet"}])
    child = sent.units()[1]
    assert _codes(sent) == [(f"variants[{child.seller_sku}]", "VALUE_NOT_ALLOWED")]


def test_amazon_theme_without_parent_invalid() -> None:
    from tests.adapters.amazon_support import amazon_item

    assert _codes(amazon_item(variation_theme="size")) == [("variants", "VARIATION_INVALID")]
