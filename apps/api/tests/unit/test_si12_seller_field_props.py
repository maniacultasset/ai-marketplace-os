"""SI12 tester properties (AC9): the API seller-field types and the shared import validators
agree on every input, so a value the CSV import refuses is refused on the API too, and the
reverse."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from app.api.v1.schemas.products import CountryCode, Dimension, Grams, HsnCode, PackageIn
from app.services.intake_values import (
    COUNTRY_PATTERN,
    HSN_PATTERN,
    MAX_DIMENSION,
    MAX_GRAMS,
    check_dimension,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

HSN = TypeAdapter(HsnCode)
COUNTRY = TypeAdapter(CountryCode)
DIMENSION = TypeAdapter(Dimension)
GRAMS = TypeAdapter(Grams)


def _accepts(adapter: TypeAdapter[Any], value: Any) -> Any:
    try:
        return adapter.validate_python(value)
    except ValidationError:
        return None


_digits = st.text(alphabet="0123456789", min_size=0, max_size=10)


@settings(max_examples=300)
@given(st.one_of(_digits, st.text(max_size=10)))
def test_hsn_agrees_with_the_import_pattern(value: str) -> None:
    accepted = _accepts(HSN, value)
    assert (accepted is not None) == bool(HSN_PATTERN.fullmatch(value))
    if accepted is not None:
        assert accepted == value


@settings(max_examples=300)
@given(
    st.one_of(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=0, max_size=3
        ),
        st.text(max_size=3),
    )
)
def test_country_agrees_with_the_import_pattern(value: str) -> None:
    accepted = _accepts(COUNTRY, value)
    expected = value.isascii() and bool(COUNTRY_PATTERN.fullmatch(value))
    assert (accepted is not None) == expected, value
    if accepted is not None:
        assert accepted == value.upper() and accepted.isupper()


@settings(max_examples=300)
@given(
    st.one_of(
        st.decimals(min_value=Decimal("-10"), max_value=Decimal("200000"), allow_nan=False),
        st.decimals(min_value=Decimal("0"), max_value=Decimal("100"), places=3),
        st.sampled_from(
            [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("1E+2")]
        ),
    )
)
def test_dimension_agrees_with_the_shared_check(value: Decimal) -> None:
    accepted = _accepts(DIMENSION, value)
    try:
        check_dimension(value)
        expected = True
    except ValueError:
        expected = False
    assert (accepted is not None) == expected, value
    if accepted is not None:
        assert Decimal(0) < accepted <= MAX_DIMENSION


@settings(max_examples=200)
@given(st.integers(min_value=-5, max_value=MAX_GRAMS + 5))
def test_grams_bounds(value: int) -> None:
    assert (_accepts(GRAMS, value) is not None) == (1 <= value <= MAX_GRAMS)


@pytest.mark.parametrize("value", [True, False, 1.0, "1"])
def test_grams_is_a_strict_int(value: Any) -> None:
    assert _accepts(GRAMS, value) is None


_maybe_dim = st.one_of(st.none(), st.just(Decimal("1.5")))
_keys = st.sets(st.sampled_from(["length_cm", "breadth_cm", "height_cm", "weight_g"]))


@settings(max_examples=200)
@given(_keys, st.data())
def test_package_dims_all_or_none(keys: set[str], data: st.DataObject) -> None:
    body: dict[str, Any] = {}
    for key in keys:
        body[key] = (
            data.draw(st.one_of(st.none(), st.just(5)))
            if key == "weight_g"
            else data.draw(_maybe_dim)
        )
    dims = [k for k in ("length_cm", "breadth_cm", "height_cm") if k in body]
    nulls = [k for k in dims if body[k] is None]
    valid = bool(body) and (not dims or (len(dims) == 3 and len(nulls) in (0, 3)))
    try:
        PackageIn.model_validate(body)
        accepted = True
    except ValidationError:
        accepted = False
    assert accepted == valid, body
