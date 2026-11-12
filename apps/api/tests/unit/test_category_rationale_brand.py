"""Prod Chrome pass 12 Nov (B11): the category suggestion said "brand is Staff, indicating ethnic
wear". A reason cites the product's own signals, never the brand (its name or the word)."""

from __future__ import annotations

from uuid import UUID

import pytest
from app.workers.category_suggestion import product_rationale, screen
from mkos_schemas.classify import (
    CategoryCandidate,
    CategoryOption,
    ClassifyRequest,
    ClassifyResult,
    ProductText,
)


def _request(brand: str | None = "Staff") -> ClassifyRequest:
    return ClassifyRequest(
        tenant_id=UUID(int=1),
        product_id=UUID(int=2),
        request_id="req-b11",
        text=ProductText(title="KTEST QA Cotton Kurta with pants", brand=brand),
        categories=[CategoryOption(category="kurta_sets", name="Kurta sets")],
    )


def _result(rationale: str, refs: list[str]) -> ClassifyResult:
    return ClassifyResult(
        status="ok",
        candidates=[
            CategoryCandidate(
                category="kurta_sets", confidence=0.95, rationale=rationale, evidence_refs=refs
            )
        ],
        calibration_version="v2",
    )


def test_prod_reason_loses_its_brand_clause() -> None:
    out = screen(
        _result(
            "Title says cotton kurta with pants; brand is Staff, indicating ethnic wear.",
            ["text.title", "text.brand"],
        ),
        _request(),
    )
    candidate = out.candidates[0]
    assert candidate.rationale == "Title says cotton kurta with pants."
    assert candidate.evidence_refs == ["text.title"]


def test_reason_of_only_the_brand_falls_back_to_the_evidence() -> None:
    out = screen(_result("Staff makes ethnic wear.", ["text.title", "text.brand"]), _request())
    assert out.candidates[0].rationale == "Matches the product name."


@pytest.mark.parametrize(
    ("text", "brand", "refs", "expected"),
    [
        ("A kurta with matching pants.", "Staff", ["text.title"], "A kurta with matching pants."),
        # a brand word inside another word is not the brand
        (
            "Fits staffing uniforms. A kurta set.",
            "Staff",
            [],
            "Fits staffing uniforms. A kurta set.",
        ),
        ("The brand's range is ethnic.", None, ["images[x]"], "Matches the photos."),
        (
            "STAFF sells sets. Photo shows a kurta with its bottom.",
            "Staff",
            [],
            "Photo shows a kurta with its bottom.",
        ),
        (
            "Brand only.",
            "Staff",
            ["text.title", "attributes.fabric"],
            ("Matches the product name and the product details."),
        ),
    ],
)
def test_product_rationale(text: str, brand: str | None, refs: list[str], expected: str) -> None:
    assert product_rationale(text, brand, refs) == expected
