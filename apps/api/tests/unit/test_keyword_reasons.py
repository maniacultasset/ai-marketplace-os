"""Client feedback 11 (bullet 6): keyword reasons follow the keywords through a merge, the locked
paths and a seller edit, and `used_in` always describes the stored copy. Pure, no database."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.services.keyword_reasons import keyword_covered, refresh_keyword_reasons, used_in
from app.services.resolution.merge import merge_targets
from app.services.sections import keep_locked
from mkos_schemas.listing import MAX_KEYWORD_REASONS, CanonicalListing

LISTING_ID, PRODUCT_ID = str(uuid.uuid4()), str(uuid.uuid4())
TITLE = "Acme Women's Indigo Cotton Kurta Set with Three-Quarter Sleeves"


def _reason(term: str, reason: str = "Names the fabric.", **extra: Any) -> dict[str, Any]:
    return {"term": term, "reason": reason, "source": "product_facts", "used_in": [], **extra}


def _listing(
    *,
    title: str = TITLE,
    bullets: tuple[str, ...] = ("Cotton fabric: soft for everyday wear",),
    description: str = "A kurta set for work.",
    terms: tuple[str, ...] = ("cotton kurta set", "indigo kurta"),
    hidden: tuple[str, ...] = ("kurtha set",),
    reasons: list[dict[str, Any]] | None = None,
) -> CanonicalListing:
    return CanonicalListing.model_validate(
        {
            "listing_id": LISTING_ID,
            "product_id": PRODUCT_ID,
            "marketplace": "amazon",
            "category": "ethnic_wear_kurta_sets",
            "content": {"title": title, "bullets": list(bullets), "description": description},
            "keywords": {"search_terms": list(terms), "hidden_terms": list(hidden)},
            "keyword_reasons": reasons if reasons is not None else [],
        }
    )


# The pipeline's own cases (mkos-pipeline tests/test_copy_checks.py): the matcher must agree.
@pytest.mark.parametrize(
    ("term", "text", "covered"),
    [
        ("kurta set for women", "Women's kurta sets", True),
        ("three quarter sleeve kurta", TITLE, True),
        ("anarkali kurta", TITLE, False),
        ("for the", TITLE, False),
        ("tshirt", "Black Cotton T-Shirt", True),
        ("cotton kurta", "cottonkurta", False),
    ],
)
def test_keyword_covered_matches_the_pipeline(term: str, text: str, covered: bool) -> None:
    assert keyword_covered(term, text) is covered


def test_used_in_names_the_copy_and_always_the_backend() -> None:
    listing = _listing(description="This cotton kurta set suits office days.")
    assert used_in("cotton kurta set", listing) == ["title", "description", "backend"]
    assert used_in("cotton fabric", listing) == ["bullets", "backend"]
    assert used_in("anarkali kurta", listing) == ["backend"]


def test_refresh_keeps_only_listed_terms_and_recomputes_used_in() -> None:
    listing = _listing(
        reasons=[
            _reason("cotton kurta set", used_in=["backend"]),
            _reason("dropped term"),
            _reason("kurtha set", "A common spelling.", source="search_intent"),
        ]
    )
    out = refresh_keyword_reasons(listing)
    assert [(r.term, r.used_in) for r in out.keyword_reasons] == [
        ("cotton kurta set", ["title", "backend"]),
        ("kurtha set", ["backend"]),
    ]
    assert out.keyword_reasons[1].source == "search_intent"
    assert refresh_keyword_reasons(out) is out


def test_refresh_fills_from_the_sources_in_order() -> None:
    listing = _listing(terms=("cotton kurta set", "indigo kurta"))
    first = _listing(reasons=[_reason("indigo kurta", "First.")])
    second = _listing(
        reasons=[_reason("indigo kurta", "Second."), _reason("cotton kurta set", "Only.")]
    )
    out = refresh_keyword_reasons(listing, (first, second))
    assert [(r.term, r.reason) for r in out.keyword_reasons] == [
        ("cotton kurta set", "Only."),
        ("indigo kurta", "First."),
    ]


def test_refresh_never_exceeds_the_contract_cap() -> None:
    terms = tuple(f"term {n}" for n in range(50))
    hidden = tuple(f"hidden {n}" for n in range(10))
    listing = _listing(terms=terms, hidden=hidden)
    first = _listing(terms=terms, hidden=(), reasons=[_reason(t) for t in terms])
    second = _listing(terms=hidden, hidden=(), reasons=[_reason(t) for t in hidden])
    out = refresh_keyword_reasons(listing, (first, second))
    assert len(out.keyword_reasons) == MAX_KEYWORD_REASONS
    assert out.keyword_reasons[-1].term == "term 49"
    CanonicalListing.model_validate(out.model_dump())


def test_a_copy_target_keeps_the_base_reasons_with_fresh_used_in() -> None:
    base = _listing(reasons=[_reason("cotton kurta set", "Base reason.")])
    result = _listing(
        title="Acme Indigo Kurta",
        terms=("other term",),
        reasons=[_reason("cotton kurta set", "Result reason."), _reason("other term")],
    )
    merged = merge_targets(base, result, ["content.title"])
    assert merged.content.title == "Acme Indigo Kurta"
    assert merged.keywords.search_terms == ["cotton kurta set", "indigo kurta"]
    (reason,) = merged.keyword_reasons
    assert reason.reason == "Base reason." and reason.used_in == ["backend"]


def test_a_keyword_target_takes_the_result_reasons_first() -> None:
    base = _listing(
        reasons=[_reason("cotton kurta set", "Base reason."), _reason("indigo kurta", "Old.")]
    )
    result = _listing(
        terms=("cotton kurta set", "kurta for women"),
        reasons=[_reason("cotton kurta set", "Result reason."), _reason("kurta for women", "New.")],
    )
    merged = merge_targets(base, result, ["keywords.search_terms"])
    assert [(r.term, r.reason) for r in merged.keyword_reasons] == [
        ("cotton kurta set", "Result reason."),
        ("kurta for women", "New."),
    ]


def test_locked_keywords_bring_their_own_reasons_back() -> None:
    previous = _listing(terms=("seller term",), reasons=[_reason("seller term", "Seller's.")])
    fresh = _listing(terms=("model term",), reasons=[_reason("model term", "Model's.")])
    kept = keep_locked(fresh, previous, ["keywords.search_terms"])
    assert kept.keywords.search_terms == ["seller term"]
    assert [(r.term, r.reason) for r in kept.keyword_reasons] == [("seller term", "Seller's.")]
