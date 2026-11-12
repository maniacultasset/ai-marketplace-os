"""CanonicalListing and related models."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.listing import (
    MAX_KEYWORD_REASONS,
    MAX_LISTING_ATTRIBUTES,
    MAX_LISTING_CLAIMS,
    MAX_LISTING_IMAGES,
    CanonicalListing,
    Claim,
    ImageRef,
    KeywordReason,
    ListingContent,
    ListingKeywords,
)
from pydantic import ValidationError

FORBIDDEN = {
    "backend_keywords",
    "browse_node",
    "fk_category_id",
    "marketplace_payload",
    "bullet_point",
    "item_name",
    "asin",
    "fsn",
    "url",
    "image_url",
}


def _listing(**overrides: object) -> CanonicalListing:
    base = {
        "product_id": UUID("11111111-1111-1111-1111-111111111111"),
        "marketplace": Marketplace.AMAZON,
        "category": "apparel",
        "content": ListingContent(title="Title", bullets=["b"], description="desc"),
    }
    base.update(overrides)
    return CanonicalListing.model_validate(base)


def test_canonical_listing_shape() -> None:
    lid = UUID("22222222-2222-2222-2222-222222222222")
    run = UUID("33333333-3333-3333-3333-333333333333")
    obj = _listing(
        listing_id=lid,
        keywords=ListingKeywords(hidden_terms=["h"]),
        images=[ImageRef(image_id=lid, slot=ImageSlot.MAIN)],
        generated_by_agent_run_id=run,
    )
    assert obj.schema_version == "2.1.0"
    assert obj.content.title == "Title"
    assert obj.keywords.hidden_terms == ["h"]
    assert obj.images[0].slot is ImageSlot.MAIN
    assert obj.generated_by_agent_run_id == run


def test_canonical_listing_has_no_tenant_id() -> None:
    assert "tenant_id" not in CanonicalListing.model_fields


def test_canonical_listing_no_marketplace_specific_keys() -> None:
    schema = CanonicalListing.model_json_schema()

    def walk(props: dict[str, Any]) -> None:
        for name, spec in props.items():
            assert name not in FORBIDDEN
            assert "amazon" not in name.lower() and "flipkart" not in name.lower()
            if isinstance(spec, dict) and "properties" in spec:
                walk(spec["properties"])

    walk(schema.get("properties", {}))
    defs = schema.get("$defs", {})
    for d in defs.values():
        if "properties" in d:
            walk(d["properties"])


def test_image_ref_has_no_url() -> None:
    assert "url" not in ImageRef.model_fields


def test_image_ref_refuses_packaging_label() -> None:
    with pytest.raises(ValidationError):
        ImageRef(
            image_id=UUID("11111111-1111-1111-1111-111111111111"),
            slot=ImageSlot.PACKAGING_LABEL,
        )


def test_listing_content_bounds() -> None:
    title500 = "अ" * 500
    _listing(content=ListingContent(title=title500, bullets=["x"], description="d"))
    with pytest.raises(ValidationError):
        _listing(content=ListingContent(title="अ" * 501, bullets=["x"], description="d"))
    with pytest.raises(ValidationError):
        _listing(content=ListingContent(title="t", bullets=[], description="d"))
    with pytest.raises(ValidationError):
        _listing(content=ListingContent(title="t", bullets=["x"] * 11, description="d"))
    with pytest.raises(ValidationError):
        _listing(content=ListingContent(title="t", bullets=["x"], description="d" * 5001))
    with pytest.raises(ValidationError):
        _listing(content=ListingContent(title="   ", bullets=["x"], description="d"))


def test_listing_keywords_caps() -> None:
    terms = [f"t{i}" for i in range(50)]
    _listing(keywords=ListingKeywords(search_terms=terms))
    with pytest.raises(ValidationError):
        _listing(keywords=ListingKeywords(search_terms=[*terms, "extra"]))
    with pytest.raises(ValidationError):
        _listing(keywords=ListingKeywords(search_terms=[" "]))
    with pytest.raises(ValidationError):
        _listing(keywords=ListingKeywords(search_terms=["x" * 257]))


def test_listing_images_and_attributes_count_caps() -> None:
    imgs = [ImageRef(image_id=UUID(int=i), slot=ImageSlot.MAIN) for i in range(MAX_LISTING_IMAGES)]
    _listing(images=imgs)
    with pytest.raises(ValidationError):
        _listing(images=[*imgs, ImageRef(image_id=UUID(int=999), slot=ImageSlot.MAIN)])
    attrs = {f"k{i}": "v" for i in range(MAX_LISTING_ATTRIBUTES)}
    _listing(attributes=attrs)
    with pytest.raises(ValidationError):
        _listing(attributes={**attrs, "extra": "v"})


def test_attribute_string_values_max_256() -> None:
    with pytest.raises(ValidationError):
        _listing(attributes={"k": "x" * 257})


def test_attribute_value_types_preserved() -> None:
    obj = _listing(
        attributes={"a": 1, "b": 1.5, "c": True, "d": ["a"], "e": "1"},
    )
    dumped = json.loads(obj.model_dump_json())
    assert dumped["attributes"]["a"] == 1
    assert dumped["attributes"]["b"] == 1.5
    assert dumped["attributes"]["c"] is True
    assert dumped["attributes"]["d"] == ["a"]
    assert dumped["attributes"]["e"] == "1"


def test_attribute_value_rejects_objects_and_nan() -> None:
    with pytest.raises(ValidationError):
        _listing(attributes={"k": {"nested": 1}})
    with pytest.raises(ValidationError):
        _listing(attributes={"k": float("nan")})


def test_attribute_keys_validated() -> None:
    with pytest.raises(ValidationError):
        _listing(attributes={"Bad-Key": "v"})


def test_confidence_score_bounds() -> None:
    _listing(confidence_score=0.0)
    _listing(confidence_score=1.0)
    with pytest.raises(ValidationError):
        _listing(confidence_score=-0.01)
    with pytest.raises(ValidationError):
        _listing(confidence_score=1.01)
    with pytest.raises(ValidationError):
        _listing(confidence_score=float("nan"))


def test_category_is_slug() -> None:
    with pytest.raises(ValidationError):
        _listing(category="Bad-Category")


def test_locale_defaults_en_in() -> None:
    assert _listing().locale == "en-IN"


def test_version_is_distinct_from_schema_version() -> None:
    obj = _listing(version=2)
    assert obj.version == 2
    assert obj.schema_version == "2.1.0"


_CLAIM_ID = "55555555-5555-5555-5555-555555555555"


def _claim(**overrides: object) -> Claim:
    base: dict[str, object] = {
        "id": _CLAIM_ID,
        "text_path": "content.bullets[0]",
        "fact_keys": ["attributes.fabric_type"],
        "status": "supported",
    }
    base.update(overrides)
    return Claim.model_validate(base)


def test_claims_and_attribute_sources_default_empty() -> None:
    obj = _listing()
    assert obj.claims == []
    assert obj.attribute_sources == {}
    dumped = obj.model_dump(mode="json")
    assert dumped["claims"] == []
    assert dumped["attribute_sources"] == {}


def test_claims_and_attribute_sources_round_trip() -> None:
    obj = _listing(
        claims=[_claim(), _claim(text_path="content.title", status="removed")],
        attribute_sources={"color": "attributes.color"},
    )
    again = CanonicalListing.model_validate_json(obj.model_dump_json())
    assert again == obj
    assert again.claims[1].status == "removed"
    assert again.attribute_sources == {"color": "attributes.color"}


@pytest.mark.parametrize("path", ["content.title", "content.bullets[9]", "content.description"])
def test_claim_text_path_names_a_content_field(path: str) -> None:
    assert _claim(text_path=path).text_path == path


@pytest.mark.parametrize(
    "path", ["title", "content.bullets", "attributes.color", "keywords.search_terms", ""]
)
def test_claim_text_path_refuses_other_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        _claim(text_path=path)


def test_claim_status_is_closed() -> None:
    for status in ("supported", "unsupported", "removed"):
        assert _claim(status=status).status == status
    with pytest.raises(ValidationError):
        _claim(status="verified")


def test_listing_from_1_1_0_without_claims_still_parses() -> None:
    raw: dict[str, Any] = {
        "schema_version": "1.1.0",
        "product_id": "11111111-1111-1111-1111-111111111111",
        "marketplace": "amazon",
        "category": "ethnic_wear_kurta_sets",
        "content": {"title": "T", "bullets": ["b"], "description": "d"},
    }
    obj = CanonicalListing.model_validate(raw)
    assert obj.claims == [] and obj.attribute_sources == {}


def test_claims_are_capped() -> None:
    claims = [_claim(id=UUID(int=n)) for n in range(MAX_LISTING_CLAIMS)]
    assert len(_listing(claims=claims).claims) == MAX_LISTING_CLAIMS
    with pytest.raises(ValidationError):
        _listing(claims=[*claims, _claim(id=UUID(int=MAX_LISTING_CLAIMS))])


def test_attribute_sources_keys_are_attribute_keys_and_capped() -> None:
    sources = {f"k{n}": "attributes.color" for n in range(MAX_LISTING_ATTRIBUTES)}
    assert len(_listing(attribute_sources=sources).attribute_sources) == MAX_LISTING_ATTRIBUTES
    with pytest.raises(ValidationError):
        _listing(attribute_sources={**sources, "one_more": "attributes.color"})
    for bad_key in ("Color", "colour-name", "", "k" * 65):
        with pytest.raises(ValidationError):
            _listing(attribute_sources={bad_key: "attributes.color"})


@pytest.mark.parametrize("value", ["", "   ", "a" * 193])
def test_attribute_sources_values_are_short_and_non_blank(value: str) -> None:
    with pytest.raises(ValidationError):
        _listing(attribute_sources={"color": value})


# ---- 2.1.0: keyword reasons (client feedback 11, item 6) ----


def _reason(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "term": "cotton kurta set",
        "reason": "Names the fabric and product type shoppers search for.",
        "source": "product_facts",
        "fact_keys": ["attributes.fabric"],
        "used_in": ["title", "backend"],
    }
    base.update(overrides)
    return base


def test_keyword_reasons_default_empty_and_round_trip() -> None:
    assert _listing().keyword_reasons == []
    obj = _listing(
        keyword_reasons=[_reason(), _reason(term="kurta for women", source="search_intent")]
    )
    again = CanonicalListing.model_validate_json(obj.model_dump_json())
    assert again == obj
    assert again.keyword_reasons[0].fact_keys == ["attributes.fabric"]
    assert again.keyword_reasons[1].source == "search_intent"


def test_keyword_reason_optional_lists_default_empty() -> None:
    reason = KeywordReason.model_validate({"term": "a", "reason": "b", "source": "category"})
    assert reason.fact_keys == [] and reason.used_in == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "model_guess"},
        {"used_in": ["keywords"]},
        {"reason": ""},
        {"reason": "x" * 201},
        {"term": "   "},
        {"term": "t" * 257},
        {"fact_keys": ["attributes.a"] * 9},
        {"used_in": ["title", "bullets", "description", "backend", "title"]},
    ],
)
def test_keyword_reason_refuses_bad_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        KeywordReason.model_validate(_reason(**overrides))


def test_keyword_reasons_are_capped() -> None:
    reasons = [_reason(term=f"term {n}") for n in range(MAX_KEYWORD_REASONS)]
    assert len(_listing(keyword_reasons=reasons).keyword_reasons) == MAX_KEYWORD_REASONS
    with pytest.raises(ValidationError):
        _listing(keyword_reasons=[*reasons, _reason(term="one more")])
