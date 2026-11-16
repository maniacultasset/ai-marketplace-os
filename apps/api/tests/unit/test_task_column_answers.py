"""G1: `tasks_answer` (and the answer for many) also takes the HSN code and the country of origin,
checked as the product PATCH and the CSV import check them."""

from __future__ import annotations

from typing import Any

import pytest
from app.core.errors import InputError
from app.services.tasks import FACT_TARGET, _column_value
from mkos_schemas.codes import ErrorCode


@pytest.mark.parametrize(
    ("target", "column"),
    [
        ("facts.gender", "gender"),
        ("facts.brand_name", "brand_name"),
        ("facts.hsn_code", "hsn_code"),
        ("facts.country_of_origin", "country_of_origin"),
    ],
)
def test_the_answerable_columns(target: str, column: str) -> None:
    found = FACT_TARGET.match(target)
    assert found is not None and found.group(2) == column


@pytest.mark.parametrize("target", ["facts.manufacturer", "facts.identifier", "facts.mrp"])
def test_other_columns_stay_unanswerable(target: str) -> None:
    assert FACT_TARGET.match(target) is None


@pytest.mark.parametrize(("raw", "stored"), [("6204", "6204"), (" 620442 ", "620442"),
                                             ("62044200", "62044200")])  # fmt: skip
async def test_hsn_answer(raw: str, stored: str) -> None:
    assert await _column_value(None, "hsn_code", raw) == stored


@pytest.mark.parametrize("raw", ["620", "62044", "6204A", "", 6204])
async def test_hsn_answer_refused(raw: Any) -> None:
    with pytest.raises(InputError) as caught:
        await _column_value(None, "hsn_code", raw)
    assert (caught.value.code, caught.value.field) == (ErrorCode.VALIDATION_FAILED, "value")


@pytest.mark.parametrize(("raw", "stored"), [("IN", "IN"), ("in", "IN"), (" bd ", "BD")])
async def test_country_answer(raw: str, stored: str) -> None:
    assert await _column_value(None, "country_of_origin", raw) == stored


@pytest.mark.parametrize("raw", ["India", "XX", "IND", "I", "ÍN"])
async def test_country_answer_refused(raw: str) -> None:
    with pytest.raises(InputError) as caught:
        await _column_value(None, "country_of_origin", raw)
    assert (caught.value.code, caught.value.field) == (ErrorCode.VALIDATION_FAILED, "value")


FLIPKART_COUNTRY: dict[str, Any] = {
    "kind": "enum",
    "allowed_values": ["India", "Bangladesh", "China"],
}


@pytest.mark.parametrize(("raw", "stored"), [("India", "IN"), ("india", "IN"), ("IN", "IN"),
                                             ("Bangladesh", "BD")])  # fmt: skip
async def test_a_country_the_spec_names_is_stored_as_its_code(raw: str, stored: str) -> None:
    assert await _column_value(None, "country_of_origin", raw, FLIPKART_COUNTRY) == stored


@pytest.mark.parametrize("raw", ["Nepal", "Indi"])
async def test_a_country_name_outside_the_spec_is_refused(raw: str) -> None:
    with pytest.raises(InputError) as caught:
        await _column_value(None, "country_of_origin", raw, FLIPKART_COUNTRY)
    assert (caught.value.code, caught.value.field) == (ErrorCode.VALIDATION_FAILED, "value")
