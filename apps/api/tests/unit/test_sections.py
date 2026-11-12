"""Client feedback 14 and 16: listing sections, field locks and the answer dependency map
(`app/services/sections.py`), the whole-map merge, the section job payload and the regenerate
and content PATCH bodies."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.api.v1.schemas.listings import ListingContentPatch, ListingDetail, RegenerateRequest
from app.services.resolution.merge import merge_targets, mergeable
from app.services.sections import (
    ATTRIBUTE_FACTS,
    SECTIONS,
    TITLE_FACTS,
    affected_sections,
    covered,
    edited_paths,
    fact_key,
    fact_sections,
    keep_locked,
    lockable,
    next_locks,
    section_targets,
    stale_sections,
)
from app.workers.payloads import SectionPayload
from hypothesis import given
from hypothesis import strategies as st
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.paths import is_valid_field_path
from pydantic import ValidationError

LISTING_ID, PRODUCT_ID = str(uuid.uuid4()), str(uuid.uuid4())


def _listing(
    title: str = "Cotton kurta",
    bullets: tuple[str, ...] = ("Soft", "Breathable"),
    description: str = "A cotton kurta.",
    terms: tuple[str, ...] = ("kurta",),
    hidden: tuple[str, ...] = (),
    attrs: dict[str, Any] | None = None,
) -> CanonicalListing:
    return CanonicalListing.model_validate(
        {
            "listing_id": LISTING_ID,
            "product_id": PRODUCT_ID,
            "marketplace": "amazon",
            "category": "ethnic_wear_kurta_sets",
            "locale": "en-IN",
            "content": {"title": title, "bullets": list(bullets), "description": description},
            "keywords": {"search_terms": list(terms), "hidden_terms": list(hidden)},
            "attributes": {"fit": "regular"} if attrs is None else attrs,
        }
    )


# ---- lockable paths and edits ----


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("content.title", True),
        ("content.bullets", True),
        ("content.description", True),
        ("keywords.search_terms", True),
        ("keywords.hidden_terms", True),
        ("attributes.fit", True),
        ("attributes", False),
        ("content.bullets[0]", False),
        ("images[0]", False),
        ("attributes.Fit", False),
        ("content.title.x", False),
    ],
)
def test_lockable(path: str, ok: bool) -> None:
    assert lockable(path) is ok


def test_edited_paths_names_each_changed_path_once() -> None:
    old = _listing(attrs={"fit": "regular", "neckline": "round"})
    new = _listing(
        title="Linen kurta",
        terms=("kurta", "linen"),
        attrs={"fit": "slim", "pattern": "solid"},
    )
    assert edited_paths(old, new) == [
        "content.title",
        "keywords.search_terms",
        "attributes.fit",
        "attributes.neckline",
        "attributes.pattern",
    ]
    assert edited_paths(old, old) == []


def test_next_locks_unlocks_then_adds_edits() -> None:
    assert next_locks(["content.title", "attributes.fit"], unlock=["content.title"], edited=[]) == [
        "attributes.fit"
    ]
    # an edit in the same request wins over its unlock
    assert next_locks([], unlock=["content.title"], edited=["content.title"]) == ["content.title"]
    assert next_locks(["b", "a"], unlock=[], edited=[]) == ["a", "b"]


def test_covered_holds_a_list_item_of_a_locked_list() -> None:
    locked = ["content.bullets", "attributes.fit"]
    assert covered("content.bullets", locked)
    assert covered("content.bullets[2]", locked)
    assert covered("attributes.fit", locked)
    assert not covered("attributes.fitting", locked)
    assert not covered("attributes", locked)
    assert not covered("content.title", locked)


# ---- section targets ----


def test_section_targets_in_card_order_less_locked_plain_paths() -> None:
    assert section_targets(["description", "title"], []) == (
        "content.title",
        "content.description",
    )
    assert section_targets(["keywords"], ["keywords.search_terms"]) == ("keywords.hidden_terms",)
    assert section_targets(["title"], ["content.title"]) == ()
    # a locked attribute key never removes the map: `keep_locked` restores it
    assert section_targets(["attributes"], ["attributes.fit"]) == ("attributes",)
    assert section_targets(SECTIONS, []) == (
        "content.title",
        "content.bullets",
        "content.description",
        "keywords.search_terms",
        "keywords.hidden_terms",
        "attributes",
    )


def test_every_section_target_is_a_wire_field_path() -> None:
    # the pinned pipeline validates `GenerationContext.target_fields` with this vocabulary
    for path in section_targets(SECTIONS, []):
        assert is_valid_field_path(path), path


# ---- merge and keep_locked ----


def test_merge_takes_the_whole_attribute_map_but_the_planner_never_does() -> None:
    base = _listing(attrs={"fit": "regular", "neckline": "round"})
    result = _listing(title="New", attrs={"fit": "slim", "pattern": "solid"})
    merged = merge_targets(base, result, ["attributes"])
    assert merged.attributes == {"fit": "slim", "pattern": "solid"}
    assert merged.content.title == base.content.title
    assert not mergeable("attributes")


def test_keep_locked_restores_each_locked_path() -> None:
    base = _listing(title="Seller title", bullets=("Mine",), attrs={"fit": "regular"})
    result = _listing(
        title="Model title",
        bullets=("Model 1", "Model 2"),
        description="Model description",
        attrs={"fit": "slim", "pattern": "solid"},
    )
    kept = keep_locked(result, base, ["content.title", "content.bullets", "attributes.fit"])
    assert kept.content.title == "Seller title"
    assert kept.content.bullets == ["Mine"]
    assert kept.content.description == "Model description"
    assert kept.attributes == {"fit": "regular", "pattern": "solid"}
    assert keep_locked(result, base, []) is result


def test_keep_locked_removes_an_attribute_the_seller_deleted() -> None:
    base = _listing(attrs={})
    result = _listing(attrs={"fit": "slim"})
    assert keep_locked(result, base, ["attributes.fit"]).attributes == {}


TEXT = st.text(alphabet="abcdefgh ", min_size=2, max_size=20).map(lambda t: t.strip() or "x")
LISTINGS = st.builds(
    _listing,
    TEXT,
    st.lists(TEXT, min_size=1, max_size=3).map(tuple),
    TEXT,
    st.lists(TEXT, max_size=2).map(tuple),
    st.lists(TEXT, max_size=2).map(tuple),
    st.dictionaries(st.sampled_from(["fit", "pattern", "neckline"]), TEXT, max_size=3),
)
LOCKS = st.lists(
    st.sampled_from(
        [
            "content.title",
            "content.bullets",
            "content.description",
            "keywords.search_terms",
            "keywords.hidden_terms",
            "attributes.fit",
            "attributes.pattern",
        ]
    ),
    unique=True,
)


@given(LISTINGS, LISTINGS, LOCKS, st.lists(st.sampled_from(SECTIONS), min_size=1, unique=True))
def test_a_locked_path_never_changes_on_any_regenerate(
    base: CanonicalListing, result: CanonicalListing, locked: list[str], sections: list[Any]
) -> None:
    """Property: whole (no targets) and section regenerates keep every locked path as the base
    has it, and a section regenerate keeps every path outside its sections."""
    targets = section_targets(sections, locked)
    whole = keep_locked(result, base, locked)
    scoped = keep_locked(merge_targets(base, result, targets), base, locked)
    for out in (whole, scoped):
        assert not set(edited_paths(base, out)) & set(locked)
    allowed = {p for p in targets if p != "attributes"}
    for path in edited_paths(base, scoped):
        assert path in allowed or (path.startswith("attributes.") and "attributes" in targets)


# ---- answers -> sections ----


def test_fact_key_reads_both_target_forms() -> None:
    assert fact_key("facts.attributes.color") == "color"
    assert fact_key("facts.wash_care") == "wash_care"
    assert fact_key("brand.tone") is None
    assert fact_key("account.oauth") is None


def test_fact_sections_table() -> None:
    assert fact_sections("color") == SECTIONS
    assert fact_sections("country_of_origin") == ("attributes",)
    assert fact_sections("sleeve_type") == ("bullets", "description", "keywords", "attributes")
    assert not TITLE_FACTS & ATTRIBUTE_FACTS


def test_affected_sections_union_in_card_order_less_locked() -> None:
    assert affected_sections(["facts.attributes.country_of_origin"]) == ["attributes"]
    assert affected_sections(
        ["facts.attributes.wash_care", "facts.attributes.country_of_origin"]
    ) == ["bullets", "description", "keywords", "attributes"]
    assert affected_sections(["facts.attributes.color"], ["content.title"]) == [
        "bullets",
        "description",
        "keywords",
        "attributes",
    ]
    assert affected_sections(["brand.tone"]) == []
    assert affected_sections([]) == []


T0 = datetime(2026, 11, 11, 12, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_stale_sections_measure_each_section_from_its_last_ai_write() -> None:
    wash = [("facts.attributes.wash_care", _at(5))]
    described = ["bullets", "description", "keywords", "attributes"]
    # a fresh generation before the answer: every section it feeds waits
    assert stale_sections(wash, [(_at(0), None)]) == described
    # a whole run after the answer wrote them all
    assert stale_sections(wash, [(_at(0), None), (_at(9), None)]) == []
    # one section regenerated after the answer: only that one is up to date
    one = [(_at(0), None), (_at(9), ["content.bullets"])]
    assert stale_sections(wash, one) == ["description", "keywords", "attributes"]
    # an AI fix of one bullet or one attribute key covers its section
    fix = [(_at(0), None), (_at(9), ["content.bullets[1]", "attributes.neckline"])]
    assert stale_sections(wash, fix) == ["description", "keywords"]
    # an answer older than every write is used; no write at all leaves every section waiting
    assert stale_sections([("facts.attributes.wash_care", _at(-1))], [(_at(0), None)]) == []
    assert stale_sections(wash, []) == described
    # fully locked sections are left out; a non-fact target is ignored
    assert stale_sections(wash, [(_at(0), None)], ["content.description"]) == [
        "bullets",
        "keywords",
        "attributes",
    ]
    assert stale_sections([("brand.tone", _at(5))], []) == []


# ---- the job payload and the request bodies ----


def test_section_payload_round_trips_and_needs_targets() -> None:
    listing, base = uuid.uuid4(), uuid.uuid4()
    payload = SectionPayload(
        listing_id=listing, base_version_id=base, target_fields=("content.title",)
    ).model_dump(mode="json")
    assert payload["mode"] == "sections" and payload["reason"] == "section_regenerate"
    assert "trigger" not in payload  # a person's request: the resolution cap never counts it
    assert SectionPayload.model_validate(payload).target_fields == ("content.title",)
    with pytest.raises(ValidationError):
        SectionPayload(listing_id=listing, base_version_id=base, target_fields=())


def test_regenerate_request_sections() -> None:
    assert RegenerateRequest.model_validate({}).sections is None
    body = RegenerateRequest.model_validate({"sections": ["title", "bullets"], "for_answers": True})
    assert body.sections == ["title", "bullets"] and body.for_answers
    bad: dict[str, Any]
    for bad in (
        {"sections": []},
        {"sections": ["title", "title"]},
        {"sections": ["images"]},
        {"for_answers": True},
    ):
        with pytest.raises(ValidationError):
            RegenerateRequest.model_validate(bad)


def test_content_patch_unlock_takes_lockable_paths_only() -> None:
    content = {"title": "T", "bullets": ["B"], "description": "D"}
    ok = ListingContentPatch.model_validate({"content": content, "unlock": ["attributes.fit"]})
    assert ok.unlock == ["attributes.fit"]
    assert ListingContentPatch.model_validate({"content": content}).unlock == []
    with pytest.raises(ValidationError):
        ListingContentPatch.model_validate({"content": content, "unlock": ["attributes"]})


def test_listing_detail_defaults_for_an_older_payload() -> None:
    fields = ListingDetail.model_fields
    assert fields["locked_paths"].default_factory is not None
    assert fields["affected_sections"].default_factory is not None
