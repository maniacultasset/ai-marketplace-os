"""Client feedback 17 Nov: a jeans photo suggested T-shirts. The suggestion says when nothing
offered fits (`not_listed`) and when the seller's own words name another group (`looks_like`)."""

from __future__ import annotations

from uuid import UUID

import pytest
from app.services.catalog_categories import CATEGORY_GROUPS, category_group
from app.services.category_draft import answer_from_job
from app.services.category_gate import NOT_LISTED, NOT_LISTED_NAME
from app.services.category_honesty import assess, word_groups
from app.workers.category_suggestion import _needs_review, screen
from mkos_schemas.classify import (
    CategoryCandidate,
    CategoryOption,
    ClassifyRequest,
    ClassifyResult,
    ProductText,
)

OFFERED = ("ethnic_wear_kurta_sets", "jeans", "tshirts")


def _request(
    title: str, seller_category: str | None = None, attributes: dict[str, str] | None = None
) -> ClassifyRequest:
    return ClassifyRequest(
        tenant_id=UUID(int=1),
        product_id=UUID(int=2),
        request_id="req-honest",
        text=ProductText(title=title, seller_category=seller_category, attributes=attributes or {}),
        categories=[
            *(CategoryOption(category=slug, name=slug) for slug in OFFERED),
            CategoryOption(category=NOT_LISTED, name=NOT_LISTED_NAME),
        ],
    )


def _result(*ranked: tuple[str, float]) -> ClassifyResult:
    return ClassifyResult(
        status="ok",
        candidates=[
            CategoryCandidate(category=slug, confidence=conf, rationale="Matches the name.")
            for slug, conf in ranked
        ],
        calibration_version="v2",
    )


def test_groups_match_the_shared_contract() -> None:
    assert category_group("tshirts") == "topwear"
    assert category_group("ethnic_wear_kurta_sets") == "ethnic"
    assert category_group("home_decor_lamps") == "home"
    for slug in ("jeans", "trousers", "track_pants", "shorts", "skirts"):
        assert category_group(slug) == "bottomwear"
    assert category_group("sarees") == "other"
    assert set(CATEGORY_GROUPS.values()) == {"topwear", "ethnic", "home", "bottomwear"}


def test_not_listed_is_stripped_and_flagged_when_first() -> None:
    request = _request("Silk saree with blouse piece")
    raw = _result((NOT_LISTED, 0.7), ("ethnic_wear_kurta_sets", 0.2), ("tshirts", 0.1))
    screened = screen(raw, request)
    assert [c.category for c in screened.candidates] == ["ethnic_wear_kurta_sets", "tshirts"]
    honesty = assess(raw, screened, request.text)
    assert honesty.not_listed and honesty.unsure
    assert honesty.looks_like == "ethnic"  # "saree" names the ethnic group
    assert _needs_review(_result(("ethnic_wear_kurta_sets", 0.95)), honesty)


def test_not_listed_lower_down_is_only_stripped() -> None:
    request = _request("Round neck cotton tee")
    raw = _result(("tshirts", 0.9), (NOT_LISTED, 0.1))
    screened = screen(raw, request)
    assert [c.category for c in screened.candidates] == ["tshirts"]
    honesty = assess(raw, screened, request.text)
    assert not honesty.not_listed and honesty.looks_like is None and not honesty.unsure
    assert not _needs_review(screened, honesty)


def test_jeans_words_against_a_topwear_pick() -> None:
    request = _request("Men Slim Fit Blue Jeans")
    raw = _result(("tshirts", 0.9))
    honesty = assess(raw, screen(raw, request), request.text)
    assert honesty.looks_like == "bottomwear" and not honesty.not_listed
    assert _needs_review(raw, honesty)


def test_words_that_agree_with_the_pick_are_fine() -> None:
    request = _request("Men Slim Fit Blue Jeans")
    raw = _result(("jeans", 0.9), ("tshirts", 0.1))
    assert assess(raw, screen(raw, request), request.text).looks_like is None


def test_mixed_words_that_include_the_pick_group_are_fine() -> None:
    request = _request("Cotton kurta with pants", attributes={"neckline": "Shirt collar"})
    raw = _result(("ethnic_wear_kurta_sets", 0.92))
    assert assess(raw, screen(raw, request), request.text).looks_like is None


def test_seller_category_and_attributes_count() -> None:
    request = _request("Classic fit everyday wear", seller_category="Denim")
    raw = _result(("tshirts", 0.85))
    assert assess(raw, screen(raw, request), request.text).looks_like == "bottomwear"
    request = _request("Classic fit everyday wear", attributes={"style": "Cargo"})
    assert assess(raw, screen(raw, request), request.text).looks_like == "bottomwear"


def test_short_sleeve_is_not_shorts() -> None:
    request = _request("Graphic print crew", attributes={"sleeve_length": "Short Sleeve"})
    raw = _result(("tshirts", 0.9))
    assert assess(raw, screen(raw, request), request.text).looks_like is None


def test_a_top_pick_of_no_known_group_is_not_contradicted() -> None:
    request = _request("Cotton crew tee")
    raw = _result(("sarees", 0.9))
    assert assess(raw, raw, request.text).looks_like is None


def test_no_candidates_still_hint_the_group() -> None:
    request = _request("Pleated midi skirt")
    raw = _result()
    assert assess(raw, screen(raw, request), request.text).looks_like == "bottomwear"


@pytest.mark.parametrize(
    ("text", "groups"),
    [
        ("Men's Track Pants", ["bottomwear"]),
        ("track-pant for gym", ["bottomwear"]),
        ("Oversized T-Shirt", ["topwear"]),
        ("Women Kurti", ["ethnic"]),
        ("Desk lamp", []),
        ("Jeanswear", []),  # whole words only
        ("Laptop sleeve", []),
        ("Short kurta", ["ethnic"]),
        ("Denim shorts", ["bottomwear"]),
    ],
)
def test_word_groups(text: str, groups: list[str]) -> None:
    assert word_groups([text]) == groups


def test_draft_answer_carries_the_flags_and_marks_guesses() -> None:
    job = UUID(int=9)
    stored = {
        "status": "suggested",
        "candidates": [{"category": "tshirts", "confidence": 0.95, "rationale": "r"}],
        "not_listed": False,
        "looks_like": "bottomwear",
    }
    body = answer_from_job(job, "succeeded", stored)
    assert body["looks_like"] == "bottomwear" and body["not_listed"] is False
    assert body["candidates"][0]["unsure"] is True


def test_draft_answer_stored_before_the_flags() -> None:
    stored = {
        "status": "suggested",
        "candidates": [{"category": "tshirts", "confidence": 0.95, "rationale": "r"}],
        "looks_like": "nonsense",
    }
    body = answer_from_job(UUID(int=9), "succeeded", stored)
    assert body["not_listed"] is False and body["looks_like"] is None
    assert body["candidates"][0]["unsure"] is False
