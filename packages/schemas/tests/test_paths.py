"""Field-path vocabulary and resolve()."""

from __future__ import annotations

import re
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.enums import ImageSlot, ListingStatus, Marketplace
from mkos_schemas.listing import CanonicalListing, ImageRef, ListingContent, ListingKeywords
from mkos_schemas.paths import (
    FIELD_PATH_PATTERNS,
    MAX_PATH_LENGTH,
    RULE_FIELD_EXTRAS,
    is_valid_condition_path,
    is_valid_field_path,
    is_valid_rule_field,
    resolve,
)

VALID_PATHS = [
    "listing",
    "request",
    "file.header",
    "file.row",
    "content.title",
    "content.bullets",
    "content.bullets[0]",
    "content.description",
    "keywords.search_terms",
    "keywords.search_terms[0]",
    "keywords.hidden_terms[1]",
    "attributes",
    "attributes.size_2xl",
    "images[0]",
    "facts.mrp",
    "variants",
    "variants[FSH-TEE-001-BLK-XL]",
    "variants[FSH-TEE-001-BLK-XL].facts.mrp",
    "brand.tone",
]

INVALID_PATHS = [
    "",
    " ",
    "title",
    "content.title\n",
    "content.title ",
    "Content.title",
    "content..title",
    "content.bullets[-1]",
    "content.bullets[1000]",
    "content.bullets[\u0661]",
    "attributes.",
    "attributes.Colour",
    "attributes.a-b",
    "images",
    "file.header.x",
    "facts.x",
    "variants[]",
    "variants[a b]",
    "rules",
    "\x00",
    "content.\x00title",
]


@pytest.mark.parametrize("path", VALID_PATHS)
def test_paths_accepts_listing_paths(path: str) -> None:
    assert is_valid_field_path(path)


def test_paths_accepts_file_header_and_row() -> None:
    assert is_valid_field_path("file.header")
    assert is_valid_field_path("file.row")


def test_paths_accepts_attribute_keys_with_digits() -> None:
    assert is_valid_field_path("attributes.size_2xl")
    assert is_valid_field_path("attributes.l2_cat")


def test_paths_accepts_request_and_listing() -> None:
    assert is_valid_field_path("request")
    assert is_valid_field_path("listing")


def test_paths_accepts_rev3_fact_variant_brand_paths() -> None:
    assert is_valid_field_path("variants[FSH-TEE-001-BLK-XL].facts.mrp")
    assert is_valid_field_path("facts.mrp")
    assert is_valid_field_path("brand.tone")


@pytest.mark.parametrize("path", INVALID_PATHS)
def test_paths_rejects_illegal(path: str) -> None:
    assert not is_valid_field_path(path)


def test_paths_length_cap() -> None:
    base = "attributes." + "a" * (MAX_PATH_LENGTH - len("attributes.") + 1)
    assert len(base) == MAX_PATH_LENGTH + 1
    assert is_valid_field_path(base) is False


def test_paths_patterns_are_bounded() -> None:
    for pat in FIELD_PATH_PATTERNS:
        assert "+" not in pat or pat.endswith("{1,3}") or "{1," in pat
        assert not re.search(r"\([^)]*[+*][^)]*\)[+*]", pat)


@settings(max_examples=500, deadline=None)
@given(st.text())
def test_paths_property_never_raises(raw: str) -> None:
    is_valid_field_path(raw)


@settings(max_examples=200, deadline=None)
@given(
    st.sampled_from(
        [
            "content.title",
            "attributes.size_2xl",
            "images[0]",
            "brand.tone",
        ]
    )
)
def test_paths_property_generated_paths_are_valid(path: str) -> None:
    assert is_valid_field_path(path)


def _sample_listing() -> CanonicalListing:
    return CanonicalListing(
        product_id=UUID("11111111-1111-1111-1111-111111111111"),
        marketplace=Marketplace.AMAZON,
        category="apparel",
        status=ListingStatus.CREATED,
        content=ListingContent(
            title="T",
            bullets=["one", "two", "three"],
            description="D",
        ),
        keywords=ListingKeywords(hidden_terms=["hidden"]),
        attributes={"size_2xl": "yes"},
        images=[
            ImageRef(
                image_id=UUID("22222222-2222-2222-2222-222222222222"),
                slot=ImageSlot.MAIN,
            )
        ],
    )


def test_resolve_reads_nested_values() -> None:
    listing = _sample_listing()
    assert resolve(listing, "listing") is listing
    assert resolve(listing, "content.title") == "T"
    assert resolve(listing, "content.bullets[2]") == "three"
    assert resolve(listing, "attributes.size_2xl") == "yes"
    assert resolve(listing, "keywords.hidden_terms[0]") == "hidden"
    image = resolve(listing, "images[0]")
    assert isinstance(image, ImageRef)
    assert image.slot is ImageSlot.MAIN


@pytest.mark.parametrize(
    "path",
    ["request", "file.header", "facts.mrp", "variants[sku]", "brand.tone"],
)
def test_resolve_rejects_non_listing_roots(path: str) -> None:
    with pytest.raises(ValueError):
        resolve(_sample_listing(), path)


def test_resolve_missing_raises_lookup_error() -> None:
    with pytest.raises(LookupError):
        resolve(_sample_listing(), "attributes.missing")
    with pytest.raises(IndexError):
        resolve(_sample_listing(), "content.bullets[9]")


def test_rule_field_extras() -> None:
    for extra in RULE_FIELD_EXTRAS:
        assert is_valid_rule_field(extra)
        assert not is_valid_field_path(extra)


def test_condition_paths() -> None:
    assert is_valid_condition_path("content.title")
    assert is_valid_condition_path("product.mrp")
    assert is_valid_condition_path("account.settings.gst")
    assert not is_valid_condition_path("product.")
    assert not is_valid_condition_path("account..x")
    assert not is_valid_condition_path("facts.mrp")
