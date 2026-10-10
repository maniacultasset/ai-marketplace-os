"""S22 T6 (AC4; D4; C1, C7, C9): Flipkart families have no parent row (DATABASE.md:471, :754-755;
T45): the children only, each with its style code, its colour group and its colourway's images
(DATABASE.md:438)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from tests.adapters.flipkart_support import fk_family, fk_item, listing


def _payloads(sent: Any) -> list[Any]:
    from app.adapters.flipkart.mapping import family_payloads

    return family_payloads(sent)


def _value(one: Any, name: str) -> Any:
    return [e["value"] for e in one.payload["attributes"].get(name, [])]


def test_flipkart_family() -> None:
    """2 colours x 2 sizes -> 4 child payloads, no parent SKU anywhere, each naming its style and
    its colour group."""
    sent = fk_family(style="KURTA-FAM-01")
    payloads = _payloads(sent)
    assert [p.unit.item_role for p in payloads] == ["child"] * 4
    skus = [p.payload["sku"] for p in payloads]
    assert "KURTA-FAM-01" not in skus and skus == sorted(skus)
    for one in payloads:
        assert _value(one, "style_code") == ["KURTA-FAM-01"]
        colour = one.unit.variant_attributes["color"]
        assert _value(one, "colour_group") == [colour]
        assert _value(one, "Colour") == ["Navy Blue" if colour == "navy blue" else "Maroon"]
        assert _value(one, "Size") == [one.unit.variant_attributes["size"]]
        assert one.payload["vertical"] == "kurta"
    from app.adapters.flipkart.mapping import check

    assert check(sent) == []


def test_flipkart_family_colour_sku_preferred() -> None:
    sent = fk_family(colour_sku={"navy blue": "KF01-NVY", "maroon": "KF01-MRN"})
    groups = {p.unit.colour_key: _value(p, "colour_group") for p in _payloads(sent)}
    assert groups == {"navy blue": ["KF01-NVY"], "maroon": ["KF01-MRN"]}


def test_flipkart_family_colour_key_fallback() -> None:
    sent = fk_family(colour_sku={"navy blue": "KF01-NVY"})
    groups = {p.unit.colour_key: _value(p, "colour_group") for p in _payloads(sent)}
    assert groups == {"navy blue": ["KF01-NVY"], "maroon": ["maroon"]}


def test_flipkart_colour_images_fanned_out() -> None:
    refs = {"navy blue": ("img:navy-1", "img:navy-2"), "maroon": ("img:maroon-1",)}
    sent = fk_family(colour_image_refs=refs)
    for one in _payloads(sent):
        expected = list(refs[one.unit.colour_key])
        assert _value(one, "images") == expected
        assert [p for p in one.submitted if p.startswith("images[")] == [
            f"images[{i}]" for i in range(len(expected))
        ]


def test_flipkart_colourway_without_images_uses_listing_images() -> None:
    sent = fk_family(colour_image_refs={"navy blue": ("img:navy-1",)})
    listing_refs = [sent.image_refs[i.image_id] for i in sent.listing.images]
    for one in _payloads(sent):
        if one.unit.colour_key == "maroon":
            assert _value(one, "images") == listing_refs
        else:
            assert _value(one, "images") == ["img:navy-1"]


@pytest.mark.parametrize("axis", ["size", "color"])
def test_flipkart_family_single_axis(axis: str) -> None:
    rows = [{"size": "S"}, {"size": "M"}] if axis == "size" else [{"color": "navy blue"}, {"color": "maroon"}]  # fmt: skip
    sent = fk_family(theme=axis, children=rows)
    from app.adapters.flipkart.mapping import check

    assert check(sent) == []
    payloads = _payloads(sent)
    assert len(payloads) == 2
    for one in payloads:
        assert _value(one, "style_code") == [sent.seller_sku]
        if axis == "size":
            assert "colour_group" not in one.payload["attributes"]
        else:
            assert _value(one, "colour_group") == [one.unit.colour_key]


def _codes(sent: Any) -> list[tuple[str, str]]:
    from app.adapters.flipkart.mapping import check

    return [(e.field, e.error_code.value) for e in check(sent)]


def test_flipkart_family_no_children_invalid() -> None:
    sent = fk_item(variation_theme="size_color")
    assert ("variants", "VARIATION_INVALID") in _codes(sent)


def test_flipkart_family_axis_mismatch_invalid() -> None:
    sent = fk_family(children=[{"size": "S", "color": "maroon"}, {"size": "M"}])
    assert ("variants", "VARIATION_INVALID") in _codes(sent)


def test_flipkart_family_with_a_parent_row_invalid() -> None:
    from app.adapters.base import AdapterItem

    sent = fk_family()
    parent = AdapterItem(sent.listing.product_id, sent.seller_sku, "parent")
    with_parent = dataclasses.replace(sent, items=(parent, *sent.items))
    assert ("variants", "VARIATION_INVALID") in _codes(with_parent)


def test_flipkart_standalone_one_item() -> None:
    sent = fk_item(sku="KURTA-001")
    (one,) = _payloads(sent)
    assert one.unit.item_role == "standalone" and one.payload["sku"] == "KURTA-001"
    assert "style_code" not in one.payload["attributes"]
    assert "colour_group" not in one.payload["attributes"]


def test_flipkart_family_preview_children_only() -> None:
    from app.adapters.flipkart.mapping import preview

    sent = fk_family()
    result = preview(sent)
    assert [i.item_role for i in result.items] == ["child"] * 4
    assert [i.seller_sku for i in result.items] == [u.seller_sku for u in sent.items]
    assert result.report.status == "passed" and result.product_type == "kurta"


def test_flipkart_family_listing_values_shared() -> None:
    sent = fk_family(listing(fabric_type="organic cotton"))
    for one in _payloads(sent):
        assert _value(one, "Fabric") == ["Cotton"]
