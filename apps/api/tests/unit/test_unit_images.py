"""SI8 T1 (AC5; CP-D5, CP-D6): `unit_images`, the one rule for which version images an item
sends. A colour child sends its own colourway's images (main first), then the style-level
images; never another colourway's."""

from __future__ import annotations

import uuid
from typing import Any

from app.adapters.base import AdapterItem, AdapterListing, UnitImage, unit_images
from mkos_schemas.enums import ImageSlot
from mkos_schemas.listing import ImageRef
from tests.adapters.contract import sample_listing

# the AC5 family: style S (main), Z (size chart); Red R1 (main), R2; Black B1 (main)
S, Z, R1, R2, B1 = (uuid.uuid4() for _ in range(5))
SLOTS = {
    S: ImageSlot.MAIN,
    Z: ImageSlot.SIZE_CHART,
    R1: ImageSlot.MAIN,
    R2: ImageSlot.SECONDARY,
    B1: ImageSlot.MAIN,
}
NAMES = {S: "S", Z: "Z", R1: "R1", R2: "R2", B1: "B1"}
COLOURS = {"red": (R1, R2), "black": (B1,)}


def _listing(
    order: tuple[uuid.UUID, ...] = (S, Z, R1, B1, R2),
    colours: dict[str, tuple[uuid.UUID, ...]] | None = None,
    *,
    missing: frozenset[uuid.UUID] = frozenset(),
    **over: Any,
) -> AdapterListing:
    body = sample_listing().model_copy(
        update={
            "images": [ImageRef(image_id=i, slot=SLOTS[i], position=n) for n, i in enumerate(order)]
        }
    )
    style = "STYLE-01"
    kids = tuple(
        AdapterItem(uuid.uuid4(), f"{style}-{key}", "child", {"color": key}, colour_key=key)
        for key in ("red", "black", "green")
    )
    return AdapterListing(
        listing_id=uuid.uuid4(),
        seller_sku=style,
        idempotency_key=uuid.uuid4().hex,
        listing=body,
        items=(AdapterItem(body.product_id, style, "parent"), *kids),
        variation_theme="color",
        image_refs={
            i: f"https://images.example.test/{NAMES[i]}.jpg" for i in order if i not in missing
        },
        colour_image_ids=COLOURS if colours is None else colours,
        **over,
    )


def _child(sent: AdapterListing, key: str) -> AdapterItem:
    (one,) = [u for u in sent.units() if u.colour_key == key]
    return one


def _names(sent: AdapterListing, unit: AdapterItem) -> list[str]:
    ids = [img.image_id for img in sent.listing.images]
    return [NAMES[ids[u.index]] for u in unit_images(sent, unit)]


def test_two_colour_family_sends_own_colour_then_style() -> None:
    sent = _listing()
    assert _names(sent, _child(sent, "red")) == ["R1", "R2", "Z"]
    assert _names(sent, _child(sent, "black")) == ["B1", "Z"]


def test_no_child_carries_another_colour_or_the_style_main() -> None:
    sent = _listing()
    red, black = _names(sent, _child(sent, "red")), _names(sent, _child(sent, "black"))
    assert not {"B1", "S"} & set(red)
    assert not {"R1", "R2", "S"} & set(black)


def test_colourway_without_images_sends_the_style_images() -> None:
    sent = _listing()
    assert _names(sent, _child(sent, "green")) == ["S", "Z"]


def test_parent_size_only_child_and_standalone_get_every_image() -> None:
    sent = _listing()
    every = ["S", "Z", "R1", "B1", "R2"]
    assert _names(sent, sent.units()[0]) == every
    size_child = AdapterItem(uuid.uuid4(), "STYLE-01-M", "child", {"size": "M"})
    assert _names(sent, size_child) == every
    alone = AdapterItem(sent.listing.product_id, "STYLE-01", "standalone")
    assert _names(sent, alone) == every


def test_colourway_with_only_a_secondary_leads_with_the_style_main() -> None:
    sent = _listing(order=(S, Z, B1, R2), colours={"red": (R2,), "black": (B1,)})
    assert _names(sent, _child(sent, "red")) == ["S", "R2", "Z"]


def test_stale_order_still_puts_the_colour_main_first() -> None:
    sent = _listing(order=(S, Z, R2, R1, B1))
    assert _names(sent, _child(sent, "red")) == ["R1", "R2", "Z"]


def test_no_version_images() -> None:
    sent = _listing(order=())
    assert unit_images(sent, _child(sent, "red")) == ()


def test_image_in_two_colourways_is_sent_once_per_child() -> None:
    sent = _listing(colours={"red": (R1, R2, R2), "black": (B1, R2)})
    red = _names(sent, _child(sent, "red"))
    black = _names(sent, _child(sent, "black"))
    assert red == ["R1", "R2", "Z"]
    assert black == ["B1", "R2", "Z"]
    assert len(red) == len(set(red)) and len(black) == len(set(black))


def test_index_is_the_version_position() -> None:
    sent = _listing(order=(R2, B1, Z, S, R1))
    ids = [img.image_id for img in sent.listing.images]
    for unit in sent.units():
        for one in unit_images(sent, unit):
            assert sent.image_refs[ids[one.index]] == one.locator


def test_missing_locator_is_none() -> None:
    sent = _listing(missing=frozenset({R2}))
    red = unit_images(sent, _child(sent, "red"))
    assert UnitImage(4, None) in red
    assert [u.locator for u in red if u.index != 4] == [
        "https://images.example.test/R1.jpg",
        "https://images.example.test/Z.jpg",
    ]


def test_colour_image_ids_frozen() -> None:
    sent = _listing()
    try:
        sent.colour_image_ids["red"] = ()  # type: ignore[index]
    except TypeError:
        return
    raise AssertionError("colour_image_ids must be read-only")
