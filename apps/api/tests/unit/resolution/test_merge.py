"""SI11 T16 (AC10): `merge_targets` keeps the base outside the targets and the result inside them,
for any base/result pair and any target subset (property)."""

from __future__ import annotations

import uuid
from typing import Any

from app.services.resolution.merge import merge_targets, mergeable
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.listing import CanonicalListing

LISTING_ID, PRODUCT_ID = str(uuid.uuid4()), str(uuid.uuid4())
WORDS = st.text(alphabet="abcdefghij ", min_size=3, max_size=40).map(lambda t: t.strip() or "x")
TARGETS = (
    "content.title",
    "content.description",
    "content.bullets",
    "content.bullets[0]",
    "content.bullets[1]",
    "keywords.search_terms",
    "keywords.hidden_terms",
    "attributes.color",
    "attributes.fabric",
)


def _listing(
    title: str, desc: str, bullets: list[str], terms: list[str], attrs: dict[str, Any]
) -> CanonicalListing:
    return CanonicalListing.model_validate(
        {
            "listing_id": LISTING_ID,
            "product_id": PRODUCT_ID,
            "marketplace": "amazon",
            "category": "ethnic_wear_kurta_sets",
            "locale": "en-IN",
            "content": {"title": title, "bullets": bullets, "description": desc},
            "keywords": {"search_terms": terms, "hidden_terms": terms[:1]},
            "attributes": attrs,
        }
    )


LISTINGS = st.builds(
    _listing,
    WORDS,
    WORDS,
    st.lists(WORDS, min_size=2, max_size=4),
    st.lists(WORDS, min_size=1, max_size=3),
    st.dictionaries(st.sampled_from(["color", "fabric", "fit"]), WORDS, max_size=3),
)


def _value(listing: CanonicalListing, path: str) -> Any:
    data = listing.model_dump(mode="python")
    if path.startswith("content.bullets["):
        return data["content"]["bullets"][int(path[16:-1])]
    root, name = path.split(".", 1)
    return data[root].get(name)


@settings(max_examples=200, deadline=None)
@given(LISTINGS, LISTINGS, st.sets(st.sampled_from(TARGETS)))
def test_only_the_targets_change(
    base: CanonicalListing, result: CanonicalListing, targets: set[str]
) -> None:
    merged = merge_targets(base, result, sorted(targets))
    whole_bullets = "content.bullets" in targets
    for path in TARGETS:
        if path.startswith("content.bullets[") and whole_bullets:
            continue  # the whole list came from the result
        if (
            path == "content.bullets"
            and not whole_bullets
            and any(t.startswith("content.bullets[") for t in targets)
        ):
            continue  # one item came from the result: checked by index below
        inside = path in targets
        expected = result if inside else base
        if path.startswith("content.bullets[") and not whole_bullets and not inside:
            expected = base
        assert _value(merged, path) == _value(expected, path), path
    if not targets:
        assert merged == base


def test_an_empty_target_list_returns_the_base() -> None:
    base = _listing("a b", "c d", ["e", "f"], ["g"], {"color": "red"})
    other = _listing("x y", "z w", ["q", "r"], ["s"], {})
    assert merge_targets(base, other, []) is base


def test_mergeable_paths() -> None:
    assert all(mergeable(p) for p in TARGETS)
    for path in ("listing", "images[0]", "facts.gender", "content", "attributes", "brand.name"):
        assert not mergeable(path)
