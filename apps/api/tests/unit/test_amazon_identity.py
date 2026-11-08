"""SI8 T3 (AC1-AC4, AC9; CP-D2, CP-D12, CP-D15): Amazon brand, audience, model and part number."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from app.adapters.amazon import constants as c
from app.adapters.amazon import mapping
from app.adapters.base import fact_paths, issue_text, party_secrets
from mkos_schemas.codes import ErrorCode
from tests.adapters.amazon_support import (
    SELLER_FACTS,
    amazon_family,
    amazon_item,
    listing,
    template,
)

MARKET = mapping.DEFAULT_MARKETPLACE_ID


def _with(sent: Any, **facts: Any) -> Any:
    one = dataclasses.replace(SELLER_FACTS, **facts)
    return dataclasses.replace(sent, unit_facts={u.product_id: one for u in sent.units()})


def _items(sent: Any) -> list[Any]:
    return mapping.build(sent)[0]


def _codes(errors: list[Any]) -> list[tuple[str, str]]:
    return [(e.field, e.error_code.value) for e in errors]


def _lamp() -> Any:
    return amazon_item(template=template("home_decor_lamps"))


def test_brand_on_every_role_with_language_tag() -> None:
    for sent in (amazon_item(), amazon_family()):
        for item in _items(sent):
            assert item.payload["attributes"]["brand"] == [
                {"value": "Test Brand", "marketplace_id": MARKET, "language_tag": c.LANGUAGE_TAG}
            ]
            assert "facts.brand_name" in item.submitted


@pytest.mark.parametrize("gender", sorted(c.AUDIENCE))
def test_kurta_carries_the_mapped_audience(gender: str) -> None:
    (item,) = _items(_with(amazon_item(), gender=gender))
    attrs = item.payload["attributes"]
    want = c.AUDIENCE[gender]
    for name in c.AUDIENCE_ATTRIBUTES:
        (entry,) = attrs[name]
        assert entry["value"] == want[name]
        assert ("language_tag" in entry) is (name in c.TEXT_IDENTITY)
    assert "facts.gender" in item.submitted


def test_shirt_carries_the_audience() -> None:
    sent = _with(amazon_item(template=template("tshirts")), gender="boys")
    (item,) = _items(sent)
    attrs = item.payload["attributes"]
    assert [attrs[n][0]["value"] for n in c.AUDIENCE_ATTRIBUTES] == ["male", "Boys", "Kid"]


def test_lamp_has_no_audience_and_no_gender_error() -> None:
    sent = _with(_lamp(), gender=None, style_number="LMP-1")
    items, errors = mapping.build(sent)
    (item,) = items
    attrs = item.payload["attributes"]
    assert not set(c.AUDIENCE_ATTRIBUTES) & set(attrs)
    assert "facts.gender" not in {e.field for e in errors}
    assert attrs["brand"][0]["value"] == "Test Brand"
    assert attrs["model_number"][0]["value"] == "LMP-1"
    assert attrs["part_number"][0]["value"] == "LMP-1"


def test_no_brand_is_one_error_per_family_and_blocks() -> None:
    sent = _with(amazon_family(), brand_name=None)
    assert len(sent.units()) == 5
    errors = mapping.check(sent)
    assert _codes(errors).count(("facts.brand_name", "MISSING_ATTRIBUTE")) == 1
    assert mapping.report_for(errors, sent).status == "failed"
    for item in _items(sent):
        assert "brand" not in item.payload["attributes"]


def test_whitespace_brand_counts_as_missing() -> None:
    errors = mapping.check(_with(amazon_item(), brand_name="   "))
    assert ("facts.brand_name", "MISSING_ATTRIBUTE") in _codes(errors)


def test_brand_is_stripped() -> None:
    (item,) = _items(_with(amazon_item(), brand_name="  Kuinn "))
    assert item.payload["attributes"]["brand"][0]["value"] == "Kuinn"


def test_no_gender_on_kurta_is_missing() -> None:
    sent = _with(amazon_family(), gender=None)
    errors = mapping.check(sent)
    assert _codes(errors).count(("facts.gender", "MISSING_ATTRIBUTE")) == 1
    (problem,) = [e for e in errors if e.field == "facts.gender"]
    assert "PATCH /api/v1/products/{product_id}" in problem.message
    for item in _items(sent):
        assert not set(c.AUDIENCE_ATTRIBUTES) & set(item.payload["attributes"])


def test_unknown_gender_is_value_not_allowed_with_suggestions() -> None:
    errors = mapping.check(_with(amazon_item(), gender="kids"))
    (problem,) = [e for e in errors if e.field == "facts.gender"]
    assert problem.error_code is ErrorCode.VALUE_NOT_ALLOWED
    assert problem.value_submitted == "kids"
    assert problem.suggestion is not None and problem.suggestion.startswith("Allowed values")


def test_style_number_is_model_and_part_number_on_every_item() -> None:
    sent = _with(amazon_family(), style_number="KS-24")
    for item in _items(sent):
        attrs = item.payload["attributes"]
        assert attrs["model_number"] == [{"value": "KS-24", "marketplace_id": MARKET}]
        assert attrs["part_number"] == [{"value": "KS-24", "marketplace_id": MARKET}]
        assert "facts.style_number" in item.submitted


@pytest.mark.parametrize("number", [None, "", "   "])
def test_no_style_number_falls_back_to_the_style_sku(number: str | None) -> None:
    sent = _with(amazon_family(), style_number=number)
    for item in _items(sent):
        attrs = item.payload["attributes"]
        assert attrs["model_number"][0]["value"] == sent.seller_sku
        assert attrs["part_number"][0]["value"] == sent.seller_sku
        assert "facts.style_number" in item.submitted  # CP-D12


def test_reverse_index_rows_for_the_identity_names() -> None:
    index = mapping.reverse_index(template().template)
    assert index["brand"] == ("facts.brand_name",)
    for name in ("target_gender", "department", "age_range_description"):
        assert index[name] == ("facts.gender",)
    assert index["model_number"] == ("facts.style_number",)
    assert index["part_number"] == ("facts.style_number",)


def test_fact_paths_cover_every_submitted_fact_path() -> None:
    for sent in (amazon_item(), amazon_family(), _lamp()):
        for item in _items(sent):
            submitted = {p for p in item.submitted if p.startswith("facts.")}
            assert submitted <= set(fact_paths(item.unit))
            assert submitted <= set(item.paths)


def test_brand_issue_text_never_carries_the_manufacturer() -> None:
    sent = amazon_item()
    index = mapping.reverse_index(template().template)
    raw = "brand Test Mills Pvt Ltd at 1 Mill Road, Surat is not registered"
    shown = issue_text(raw, ["brand"], index, party_secrets(sent))
    assert "Test Mills" not in shown and "Mill Road" not in shown


def test_unmapped_template_sends_no_identity() -> None:
    sent = amazon_item(listing(), template=None)
    (item,) = _items(sent)
    assert "brand" not in item.payload["attributes"]
