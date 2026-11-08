"""SI8 tester round 1 (AC5; CP-D5, CP-D6): Hypothesis properties of `base.unit_images` over
random colour families, and of the Amazon child payload built from it. A colour child never
carries another colourway's image, carries every image of its own, sends each image once, and
leads with a main whenever any main it may send exists."""

from __future__ import annotations

import dataclasses
import uuid

from app.adapters.amazon import constants as c
from app.adapters.amazon.mapping import family_payloads
from app.adapters.base import AdapterItem, AdapterListing, unit_images
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.enums import ImageSlot
from mkos_schemas.listing import ImageRef
from tests.adapters.contract import sample_listing

COLOURS = ("red", "black", "green")
SLOTS = (ImageSlot.MAIN, ImageSlot.SECONDARY, ImageSlot.LIFESTYLE, ImageSlot.SIZE_CHART)

# one image: (owner colour or None for a style-level image, slot)
image_spec = st.tuples(st.sampled_from((None, *COLOURS)), st.sampled_from(SLOTS))


def _family(specs: list[tuple[str | None, ImageSlot]]) -> tuple[AdapterListing, list[uuid.UUID]]:
    ids = [uuid.uuid4() for _ in specs]
    body = sample_listing().model_copy(
        update={
            "images": [
                ImageRef(image_id=i, slot=slot, position=n)
                for n, (i, (_, slot)) in enumerate(zip(ids, specs, strict=True))
            ]
        }
    )
    colour_ids = {
        key: tuple(i for i, (owner, _) in zip(ids, specs, strict=True) if owner == key)
        for key in COLOURS
    }
    style = "STYLE-P"
    kids = tuple(
        AdapterItem(uuid.uuid4(), f"{style}-{key}", "child", {"color": key}, colour_key=key)
        for key in COLOURS
    )
    sent = AdapterListing(
        listing_id=uuid.uuid4(),
        seller_sku=style,
        idempotency_key=uuid.uuid4().hex,
        listing=body,
        items=(AdapterItem(body.product_id, style, "parent"), *kids),
        variation_theme="color",
        image_refs={i: f"https://images.example.test/{n}.jpg" for n, i in enumerate(ids)},
        colour_image_ids={k: v for k, v in colour_ids.items() if v},
    )
    return sent, ids


@settings(max_examples=300, deadline=None)
@given(st.lists(image_spec, max_size=20))
def test_colour_child_images_properties(specs: list[tuple[str | None, ImageSlot]]) -> None:
    sent, ids = _family(specs)
    owner = dict(zip(ids, (o for o, _ in specs), strict=True))
    slot = dict(zip(ids, (s for _, s in specs), strict=True))
    for unit in sent.units():
        if unit.item_role != "child":
            continue
        got = [ids[u.index] for u in unit_images(sent, unit)]
        own = [i for i in ids if owner[i] == unit.colour_key]
        style = [i for i in ids if owner[i] is None]
        assert len(got) == len(set(got)), "an image is sent twice"
        assert all(owner[i] in (None, unit.colour_key) for i in got), "another colour's image"
        assert set(own) <= set(got), "an own image is dropped"
        if own:
            assert set(got) - set(own) <= set(style)
            own_main = any(slot[i] is ImageSlot.MAIN for i in own)
            if own_main:
                assert slot[got[0]] is ImageSlot.MAIN and owner[got[0]] == unit.colour_key
                assert not any(slot[i] is ImageSlot.MAIN and owner[i] is None for i in got)
            elif any(slot[i] is ImageSlot.MAIN for i in style):
                assert slot[got[0]] is ImageSlot.MAIN and owner[got[0]] is None
        else:
            assert got == style, "a colourway with no images sends the style-level images"
        for u in unit_images(sent, unit):
            assert u.locator == sent.image_refs[ids[u.index]]


@settings(max_examples=150, deadline=None)
@given(st.lists(image_spec, min_size=1, max_size=20))
def test_amazon_child_slots_are_contiguous_and_capped(
    specs: list[tuple[str | None, ImageSlot]],
) -> None:
    """Every Amazon child fills `main`, then `other_1..` with no gap, at most 9, and its
    `image_names` index points each slot at the version image it carries."""
    from tests.adapters.amazon_support import amazon_family, listing

    sent, ids = _family(specs)
    family = amazon_family(
        listing().model_copy(update={"images": sent.listing.images}),
        children=[{"size": "M", "color": key} for key in COLOURS],
    )
    kids = tuple(
        dataclasses.replace(u, colour_key=u.variant_attributes.get("color")) for u in family.items
    )
    built = dataclasses.replace(
        family,
        items=kids,
        colour_image_ids=sent.colour_image_ids,
        image_refs=sent.image_refs,
    )
    order = [c.MAIN_IMAGE, *c.OTHER_IMAGES]
    for item in family_payloads(built):
        attrs = item.payload["attributes"]
        names = [n for n in order if n in attrs]
        if item.unit.item_role == "parent":
            assert names == []
            continue
        expected = min(len(unit_images(built, item.unit)), c.MAX_IMAGES)
        assert names == order[:expected]
        for name, path in item.image_names.items():
            index = int(path.removeprefix("images[").removesuffix("]"))
            assert attrs[name][0]["media_location"] == built.image_refs[ids[index]]
