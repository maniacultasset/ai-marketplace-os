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


# ---- SI8 T4 (AC5; CP-D4): each child sends its own colour's images, then the style's ----


def _child(items: list, colour: str, size: str = "M") -> object:  # type: ignore[type-arg]
    (one,) = [
        i
        for i in items
        if i.unit.item_role == "child"
        and i.unit.variant_attributes == {"size": size, "color": colour}
    ]
    return one


def _slots(item: object) -> dict[str, str]:
    attrs = item.payload["attributes"]  # type: ignore[attr-defined]
    return {
        name: attrs[name][0]["media_location"].rsplit("/", 1)[-1].removesuffix(".jpg")
        for name in attrs
        if name.endswith("_image_locator") or "_image_locator_" in name
    }


def test_colour_children_send_their_own_images_then_the_style() -> None:
    from app.adapters.amazon.mapping import check, family_payloads
    from tests.adapters.amazon_support import colour_family

    sent, at = colour_family(green=True)
    assert check(sent) == []
    items = family_payloads(sent)
    red, black, green = _child(items, "red"), _child(items, "black", "L"), _child(items, "green")
    assert _slots(red) == {
        "main_product_image_locator": "R1",
        "other_product_image_locator_1": "R2",
        "other_product_image_locator_2": "Z",
    }
    assert _slots(black) == {
        "main_product_image_locator": "B1",
        "other_product_image_locator_1": "Z",
    }
    assert _slots(green) == {
        "main_product_image_locator": "S",
        "other_product_image_locator_1": "Z",
    }
    assert red.image_names == {  # type: ignore[attr-defined]
        "main_product_image_locator": f"images[{at['R1']}]",
        "other_product_image_locator_1": f"images[{at['R2']}]",
        "other_product_image_locator_2": f"images[{at['Z']}]",
    }
    assert _slots(items[0]) == {}  # the parent sends no image
    every = {f"images[{i}]" for i in at.values()}
    for child in items[1:]:
        assert every <= set(child.paths)


def test_colour_with_many_images_pushes_the_size_chart_past_the_cut() -> None:
    """P:§6 "Image cut": 9 red images (R1, R2 and 7 more) plus Z -> Z is position 9 (0-based),
    past Amazon's 9 slots, and skipped (accepted)."""
    from app.adapters.amazon.mapping import family_payloads
    from tests.adapters.amazon_support import colour_family

    sent, at = colour_family(extra_red=7)
    red = _child(family_payloads(sent), "red")
    assert len(_slots(red)) == 9
    assert "Z" not in _slots(red).values()
    assert f"images[{at['Z']}]" in red.skipped  # type: ignore[attr-defined]
