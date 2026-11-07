"""SI8 T5 (AC6; CP-D11): a child's `item_name` names its colour and size; the family title is cut
at a word boundary, never the suffix."""

from __future__ import annotations

import json
import unicodedata
from typing import Any

import pytest
from app.adapters.amazon import mapping
from app.adapters.amazon.mapping import child_title
from app.db.seed_templates import COLORS, SIZES
from hypothesis import given
from hypothesis import strategies as st
from tests.adapters.amazon_support import amazon_family, amazon_item, listing
from tests.support.fake_marketplace import ptd_fixture
from tests.unit.test_amazon_schema_check import ready


def _titles(sent: Any) -> dict[str, str]:
    return {
        i.unit.seller_sku: i.payload["attributes"]["item_name"][0]["value"]
        for i in mapping.build(sent)[0]
    }


def _family(title: str = "Cotton Kurta Set", **over: Any) -> Any:
    body = listing()
    body = body.model_copy(update={"content": body.content.model_copy(update={"title": title})})
    return amazon_family(body, **over)


def test_size_colour_child_ends_with_mapped_colour_and_size() -> None:
    sent = _family(children=[{"size": "m", "color": "navy blue"}, {"size": "L", "color": "maroon"}])
    titles = _titles(sent)
    assert titles[sent.seller_sku] == "Cotton Kurta Set"  # the parent keeps the family title
    kids = [u for u in sent.units() if u.item_role == "child"]
    assert titles[kids[0].seller_sku] == "Cotton Kurta Set - Navy, M"
    assert titles[kids[1].seller_sku] == "Cotton Kurta Set - Maroon, L"


def test_one_axis_families() -> None:
    colour = _family(theme="color", children=[{"color": "navy blue"}, {"color": "maroon"}])
    size = _family(theme="size", children=[{"size": "m"}, {"size": "L"}])
    assert sorted(_titles(colour).values())[1:] == [
        "Cotton Kurta Set - Maroon",
        "Cotton Kurta Set - Navy",
    ]
    assert sorted(_titles(size).values())[1:] == ["Cotton Kurta Set - L", "Cotton Kurta Set - M"]


def test_standalone_title_unchanged() -> None:
    (title,) = _titles(amazon_item()).values()
    assert title == listing().content.title


def test_unmappable_colour_adds_no_part() -> None:
    sent = _family(children=[{"size": "M", "color": "zzz"}, {"size": "L", "color": "maroon"}])
    titles = sorted(_titles(sent).values())
    assert "Cotton Kurta Set - M" in titles


def test_long_title_cut_at_a_word_boundary_keeps_the_suffix() -> None:
    words = ("Handwoven Cotton Kurta " * 12)[:195]
    out = child_title(words, "Navy", "M", 200)
    assert len(out) <= 200 and out.endswith(" - Navy, M")
    head = out.removesuffix(" - Navy, M")
    assert words.startswith(head)
    assert words[len(head)] == " "  # it stopped before a word, not inside one


def test_one_long_word_is_hard_cut() -> None:
    out = child_title("x" * 195, "Navy", "M", 200)
    assert len(out) == 200 and out == "x" * 190 + " - Navy, M"


def test_devanagari_cut_leaves_no_combining_mark() -> None:
    title = "कुर्ता" * 40  # combining marks throughout, no whitespace
    out = child_title(title, "Navy", "M", 200)
    head = out.removesuffix(" - Navy, M")
    assert len(out) <= 200 and head and not unicodedata.combining(head[-1])


def test_short_and_exact_fit() -> None:
    assert child_title("K", "Navy", "M", 200) == "K - Navy, M"
    exact = "a" * (200 - len(" - Navy, M"))
    assert child_title(exact, "Navy", "M", 200) == exact + " - Navy, M"


def test_no_room_keeps_the_title() -> None:
    assert child_title("Kurta", "Navy", "M", 8) == "Kurta"


def test_no_parts_keeps_the_title() -> None:
    assert child_title("Kurta", None, None, 3) == "Kurta"


async def test_limit_from_the_stored_definition() -> None:
    schema = json.loads(ptd_fixture("KURTA.listing.schema.json"))
    schema["properties"]["item_name"]["items"]["properties"]["value"]["maxLength"] = 150
    ref = await ready(schema)
    sent = _family(title="Handwoven Cotton Kurta " * 10, product_type_schema=ref)
    for item in mapping.build(sent)[0][1:]:
        assert len(item.payload["attributes"]["item_name"][0]["value"]) <= 150


def test_value_max_length_none_without_a_prepared_schema() -> None:
    from app.adapters.amazon.schema_check import value_max_length

    assert value_max_length(None, "item_name") is None


@given(
    title=st.text(
        alphabet=st.sampled_from(list("abcकुर्ता ,-|/\t")), min_size=1, max_size=260
    ).filter(lambda t: t.strip()),
    colour=st.one_of(st.none(), st.sampled_from(COLORS)),
    size=st.one_of(st.none(), st.sampled_from(SIZES)),
    limit=st.integers(min_value=5, max_value=220),
)
def test_child_title_property(title: str, colour: str | None, size: str | None, limit: int) -> None:
    parts = [p for p in (colour, size) if p]
    out = child_title(title, colour, size, limit)
    if not parts:
        assert out == title
        return
    suffix = " - " + ", ".join(parts)
    if limit - len(suffix) < 1 and len(title) + len(suffix) > limit:
        assert out == title
        return
    assert len(out) <= limit
    assert out.endswith(suffix)
    assert title.startswith(out.removesuffix(suffix))


@pytest.mark.parametrize("role", ["parent", "standalone"])
def test_only_children_change_title(role: str) -> None:
    sent = _family() if role == "parent" else amazon_item()
    first = mapping.build(sent)[0][0]
    assert first.payload["attributes"]["item_name"][0]["value"] == sent.listing.content.title
