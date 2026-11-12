"""Client feedback 11 Nov (items 3 and 5): the pure rules behind the category picker, the
suggestion before save and the SKU help."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.core.config import Settings
from app.repositories.jobs import MAX_PAYLOAD_BYTES
from app.repositories.templates import CurrentMapping
from app.services.catalog_categories import build_categories, category_label, type_name
from app.services.category_draft import (
    DRAFT_KEY,
    PAYLOAD_BUDGET,
    Draft,
    answer_from_job,
    draft_digest,
    draft_entity_id,
    draft_text,
    encoded_bytes,
    fit_text,
)
from app.services.sku_help import brand_prefix, category_code, format_sku

# ---- SKU help ----


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("Acme Naturals", "ACME"),
        ("acme", "ACME"),
        ("AB Cotton", "ABCO"),
        ("Zara", "ZARA"),
        ("Kuinn Test Brand", "KUIN"),
        ("Café Noir", "CAFE"),
        ("R & D Wear", "RDWE"),
        ("7 Threads", "7THR"),
        ("कुइन", "SKU"),
        ("", "SKU"),
        (None, "SKU"),
        ("   ", "SKU"),
    ],
)
def test_brand_prefix(name: str | None, prefix: str) -> None:
    assert brand_prefix(name) == prefix


@pytest.mark.parametrize(
    ("category", "code"),
    [
        ("ethnic_wear_kurta_sets", "KUR"),
        ("tshirts", "TSH"),
        ("home_decor_lamps", "LMP"),
        ("mens_formal_shirts", "SHI"),
        ("bags", "BAG"),
        (None, "GEN"),
        ("", "GEN"),
    ],
)
def test_category_code(category: str | None, code: str) -> None:
    assert category_code(category) == code


def test_format_sku_pads_and_fits_the_sku_pattern() -> None:
    import re

    assert format_sku("ACME", "KUR", 7) == "ACME-KUR-0007"
    assert format_sku("ACME", "KUR", 12345) == "ACME-KUR-12345"
    longest = format_sku("WXYZ", "ABC", 99_999_999)
    assert re.fullmatch(r"^[^\]\s]{1,64}$", longest)
    # the variant SKUs made from it still fit
    assert len(f"{longest}-NAVYBLUE-XXL") <= 64


# ---- the category picker ----


def _row(marketplace: str, category: str, product_type: str, version: int = 1) -> CurrentMapping:
    return CurrentMapping(marketplace, category, version, product_type, None)


def _settings(*, lamps: bool = False) -> Settings:
    return Settings.model_construct(category_lamps_enabled=lamps, category_suggest_enabled=False)


def test_build_groups_by_category_sorted_by_label() -> None:
    rows = [
        _row("amazon", "tshirts", "SHIRT", 2),
        _row("flipkart", "tshirts", "t_shirt", 2),
        _row("flipkart", "ethnic_wear_kurta_sets", "kurta", 3),
        _row("amazon", "ethnic_wear_kurta_sets", "KURTA", 3),
        _row("amazon", "home_decor_lamps", "LAMP"),
        _row("fake", "tshirts", "FAKE"),
        _row("amazon", "uncategorized", "X"),
        _row("amazon", "no_type", ""),
    ]
    built = build_categories(rows, _settings())
    assert [(c.category, c.label, c.enabled) for c in built] == [
        ("ethnic_wear_kurta_sets", "Kurta sets", True),
        ("home_decor_lamps", "Lamps", False),
        ("tshirts", "T-shirts", True),
    ]
    kurta = built[0].marketplaces
    assert [(m.marketplace, m.product_type, m.product_type_name) for m in kurta] == [
        ("amazon", "KURTA", "Kurta"),
        ("flipkart", "kurta", "Kurta"),
    ]
    assert [m.product_type_name for m in built[2].marketplaces] == ["Shirt", "T-shirt"]
    assert built[2].marketplaces[0].template_version == 2


def test_lamps_switch_enables_lamps() -> None:
    built = build_categories([_row("amazon", "home_decor_lamps", "LAMP")], _settings(lamps=True))
    assert built[0].enabled is True


def test_unknown_names_read_as_words() -> None:
    assert category_label("mens_formal_shirts") == "Mens formal shirts"
    assert type_name("amazon", "SAREE") == "Saree"
    assert type_name("flipkart", "kids_dress") == "Kids dress"


# ---- the suggestion before save ----


def test_draft_text_caps_and_drops_empty_values() -> None:
    attributes = {f"k{i:02d}": "v" for i in range(50)} | {"color": "  ", "fabric": " Cotton "}
    text = draft_text(
        Draft(title="  Tee  ", description="d" * 3000, brand=" ", attributes=attributes)
    )
    assert text["title"] == "Tee"
    assert text["description"] == "d" * 2000
    assert text["brand"] is None and text["seller_category"] is None
    assert len(text["attributes"]) == 40
    assert "color" not in text["attributes"] and text["attributes"]["fabric"] == "Cotton"


def test_draft_text_blank_title_is_untitled() -> None:
    assert draft_text(Draft(title="   "))["title"] == "untitled"


def test_fit_text_keeps_a_normal_draft_unchanged() -> None:
    text = draft_text(Draft(title="Tee", description="Soft", attributes={"color": "Navy"}))
    assert fit_text(text) == text


@pytest.mark.parametrize("char", ["\U0001f600", "\u0915", "a"])
def test_fit_text_worst_case_fits_the_job_payload(char: str) -> None:
    # the widest draft the request allows, all in one character (an emoji costs 12 bytes)
    attributes = {f"k{i:02d}": char * 200 for i in range(60)}
    text = draft_text(
        Draft(title=char * 500, description=char * 5000, brand=char * 200, attributes=attributes)
    )
    fitted = fit_text(text)
    payload = {DRAFT_KEY: {"text": fitted, "digest": "0" * 64}}
    assert encoded_bytes(fitted) <= PAYLOAD_BUDGET
    assert encoded_bytes(payload) <= MAX_PAYLOAD_BYTES
    assert fitted["title"] == text["title"] and fitted["brand"] == text["brand"]
    # attributes go from the last key back; the first ones stay as they were
    kept = sorted(fitted["attributes"])
    assert kept == sorted(text["attributes"])[: len(kept)]
    if char == "a":
        assert fitted == text


def test_fit_text_cuts_the_description_last() -> None:
    text: dict[str, Any] = {
        "title": "t",
        "description": "\U0001f600" * 2000,
        "brand": None,
        "attributes": {},
    }
    fitted = fit_text(text, budget=12_000)
    assert encoded_bytes(fitted) <= 12_000
    assert 0 < len(fitted["description"]) < 2000


def test_draft_digest_and_entity_follow_the_text_and_the_offer() -> None:
    text = draft_text(Draft(title="Tee"))
    same = draft_digest(text, ["shirts", "tshirts"])
    assert same == draft_digest(dict(text), ["shirts", "tshirts"])
    assert same != draft_digest(text, ["shirts"])
    assert same != draft_digest(draft_text(Draft(title="Tee 2")), ["shirts", "tshirts"])
    assert draft_entity_id(same) == draft_entity_id(same) != draft_entity_id("0" * 64)


JOB = uuid.uuid4()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_answer_live_is_queued(status: str) -> None:
    assert answer_from_job(JOB, status, None) == {
        "suggestion_id": str(JOB),
        "status": "queued",
        "candidates": [],
    }


@pytest.mark.parametrize("status", ["failed", "dead_letter", "cancelled"])
def test_answer_ended_badly_is_failed(status: str) -> None:
    assert answer_from_job(JOB, status, {"status": "suggested"})["status"] == "failed"


def test_answer_suggested_labels_and_marks_unsure() -> None:
    result = {
        "status": "suggested",
        "candidates": [
            {"category": "tshirts", "confidence": 0.9, "rationale": "A tee."},
            {"category": "ethnic_wear_kurta_sets", "confidence": 0.4, "rationale": "Maybe."},
            {"confidence": 0.3},
        ],
    }
    body = answer_from_job(JOB, "succeeded", result)
    assert body["status"] == "suggested"
    assert body["candidates"] == [
        {
            "category": "tshirts",
            "label": "T-shirts",
            "confidence": 0.9,
            "rationale": "A tee.",
            "unsure": False,
        },
        {
            "category": "ethnic_wear_kurta_sets",
            "label": "Kurta sets",
            "confidence": 0.4,
            "rationale": "Maybe.",
            "unsure": True,
        },
    ]


@pytest.mark.parametrize(
    ("result", "status"),
    [
        ({"status": "unavailable"}, "unavailable"),
        ({"status": "failed"}, "failed"),
        ({"status": "weird"}, "failed"),
        (None, "failed"),
    ],
)
def test_answer_other_results(result: dict[str, str] | None, status: str) -> None:
    body = answer_from_job(JOB, "succeeded", result)
    assert body["status"] == status and body["candidates"] == []


# ---- the batch SKU check (item 3: the variants grid and the bulk upload) ----


def test_batch_check_body_takes_1_to_1000_valid_skus() -> None:
    from app.api.v1.schemas.skus import SkuBatchCheckIn
    from pydantic import ValidationError

    assert SkuBatchCheckIn.model_validate({"skus": ["A-1", "A-1"]}).skus == ["A-1", "A-1"]
    assert len(SkuBatchCheckIn.model_validate({"skus": ["A-1"] * 1000}).skus) == 1000
    for bad in ([], ["A-1"] * 1001, ["has space"], ["a]b"], ["x" * 65], ["A\x00B"]):
        with pytest.raises(ValidationError):
            SkuBatchCheckIn.model_validate({"skus": bad})


async def test_batch_check_refuses_more_than_the_cap_before_any_query() -> None:
    from app.services.sku_help import MAX_BATCH_SKUS, check_skus

    principal: Any = object()  # never read: the cap is checked before the unit of work opens
    with pytest.raises(ValueError, match="at most"):
        await check_skus(principal, [f"S-{n}" for n in range(MAX_BATCH_SKUS + 1)])
