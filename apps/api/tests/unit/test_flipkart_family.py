"""S22 T6 (AC4; D4; C1, C7, C9): Flipkart families have no parent row (DATABASE.md:471, :754-755;
T45): the children only, each with its style code, its colour group and its colourway's images
(DATABASE.md:438)."""

from __future__ import annotations

import dataclasses
import uuid
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


def _colour_body() -> Any:
    """Style S (main) and Z (size chart); navy N1 (main), N2; maroon M1 (main)."""
    from mkos_schemas.enums import ImageSlot
    from mkos_schemas.listing import ImageRef

    slots = ["main", "size_chart", "main", "secondary", "main"]
    images = [
        ImageRef(image_id=uuid.uuid4(), slot=ImageSlot(slot), position=n)
        for n, slot in enumerate(slots)
    ]
    return listing().model_copy(update={"images": images})


def _urls(sent: Any, *indexes: int) -> list[str]:
    return [sent.image_refs[sent.listing.images[i].image_id] for i in indexes]


def test_flipkart_colour_images_then_style_images() -> None:
    """SI8 T1 (AC5): a colour child sends its colourway's images, then the shared style
    images without the style main; never another colour's. Paths are version indexes."""
    body = _colour_body()
    ids = [i.image_id for i in body.images]
    colours = {"navy blue": (ids[2], ids[3]), "maroon": (ids[4],)}
    sent = fk_family(body, colour_image_ids=colours)
    want = {"navy blue": (2, 3, 1), "maroon": (4, 1)}
    for one in _payloads(sent):
        order = want[one.unit.colour_key]
        assert _value(one, "images") == _urls(sent, *order)
        assert [p for p in one.submitted if p.startswith("images[")] == sorted(
            f"images[{i}]" for i in order
        )
        assert {f"images[{i}]" for i in range(len(ids))} <= set(one.paths)


def test_flipkart_colourway_without_images_uses_style_images() -> None:
    body = _colour_body()
    ids = [i.image_id for i in body.images]
    sent = fk_family(body, colour_image_ids={"navy blue": (ids[2], ids[3], ids[4])})
    for one in _payloads(sent):
        if one.unit.colour_key == "maroon":
            assert _value(one, "images") == _urls(sent, 0, 1)
        else:
            assert _value(one, "images") == _urls(sent, 2, 4, 3, 1)  # mains first


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


# ---- SI8 T7 (AC4): `style_code` is the style number, else the style SKU ----


def _numbered(number: str | None) -> Any:
    from tests.adapters.flipkart_support import SELLER_FACTS, with_facts

    sent = fk_family(style="KF-STYLE")
    return with_facts(sent, dataclasses.replace(SELLER_FACTS, style_number=number))


def test_flipkart_style_code_is_the_style_number() -> None:
    for one in _payloads(_numbered(" KS-24 ")):
        assert _value(one, "style_code") == ["KS-24"]
        assert "facts.style_number" in one.submitted


@pytest.mark.parametrize("number", [None, "", "   "])
def test_flipkart_style_code_falls_back_to_the_style_sku(number: str | None) -> None:
    for one in _payloads(_numbered(number)):
        assert _value(one, "style_code") == ["KF-STYLE"]
        assert "facts.style_number" in one.submitted


def test_flipkart_reverse_index_style_code_keeps_the_group_feed() -> None:
    from app.adapters.flipkart.mapping import reverse_index
    from tests.adapters.flipkart_support import template

    index = reverse_index(template().template)
    assert index["style_code"] == ("facts.style_number", "variants")
    assert index["colour_group"] == ("variants",)


async def test_flipkart_colour_child_read_never_reports_a_wrong_image() -> None:
    """SI8 fix round 2: a colour child's images are sent at version indexes, which a read has no
    version to rebuild, so the read reports no image path and an image issue fails `listing`."""
    from tests.adapters.flipkart_support import flipkart_adapter
    from tests.support.fake_marketplace import build_fake

    body = _colour_body()
    ids = [i.image_id for i in body.images]
    sent = fk_family(body, colour_image_ids={"navy blue": (ids[2], ids[3]), "maroon": (ids[4],)})
    app = build_fake()
    adapter = flipkart_adapter(app)
    await adapter.create_listing(sent)
    child = next(u for u in sent.items if u.colour_key == "navy blue")
    [key] = [k for k in app.state.fake.fk_items if k[1] == child.seller_sku]
    app.state.fake.fk_items[key]["errors"] = [
        {"code": "X1", "description": "bad", "severity": "ERROR", "attribute": "images"}
    ]
    got = await adapter.get_listing(child.seller_sku)
    assert not [f for f in got.fields if f.field.startswith("images[")]
    assert [e.field for e in got.errors] == ["listing"]
