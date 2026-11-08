"""SI5 (PLAN D12, D13): the pure barcode rules shared by the product API and the CSV parser."""

from __future__ import annotations

import pytest
from app.core.identifiers import TYPE_LENGTHS, check, gtin_valid, type_from_length
from hypothesis import given
from hypothesis import strategies as st

FULL_WIDTH = "".join(chr(0xFF10 + int(d)) for d in "4006381333931")
VALID = {
    "ean13": "4006381333931",
    "ean8": "73513537",
    "upc": "036000291452",
    "gtin14": "10036000291459",
}


@pytest.mark.parametrize("value", sorted(VALID.values()))
def test_known_barcodes_pass(value: str) -> None:
    assert gtin_valid(value)


@pytest.mark.parametrize("value", sorted(VALID.values()))
def test_a_wrong_check_digit_fails(value: str) -> None:
    wrong = value[:-1] + str((int(value[-1]) + 1) % 10)
    assert not gtin_valid(wrong)


@pytest.mark.parametrize(
    ("id_type", "value", "ok"),
    [
        ("ean", VALID["ean13"], True),
        ("ean", VALID["ean8"], True),
        ("upc", VALID["upc"], True),
        ("gtin", VALID["gtin14"], True),
        ("gtin", VALID["ean13"], True),
        ("gtin", VALID["upc"], True),
        ("upc", VALID["ean13"], False),  # 13 digits under upc
        ("ean", VALID["upc"], False),  # 12 digits under ean
        ("ean", VALID["gtin14"], False),
        ("isbn", VALID["ean13"], False),
        (None, VALID["ean13"], False),
    ],
)
def test_check_applies_the_type_length(id_type: str | None, value: str, ok: bool) -> None:
    assert (check(id_type, value) is None) is ok


def test_type_lengths_table() -> None:
    assert {
        "upc": frozenset({12}),
        "ean": frozenset({8, 13}),
        "gtin": frozenset({8, 12, 13, 14}),
    } == TYPE_LENGTHS


@pytest.mark.parametrize(
    ("value", "expected"),
    [("73513537", "ean"), ("036000291452", "upc"), ("4006381333931", "ean"),
     ("10036000291459", "gtin"), ("1234567890", None), ("", None)],
)  # fmt: skip
def test_type_from_length(value: str, expected: str | None) -> None:
    assert type_from_length(value) == expected


def test_leading_zeros_are_kept() -> None:
    assert gtin_valid("00012345600012")
    assert check("gtin", "00012345600012") is None


@pytest.mark.parametrize(
    "value",
    [FULL_WIDTH,
     "4006381 333931", " 4006381333931", "", "400638133393a"],
)  # fmt: skip
def test_non_ascii_digits_and_spaces_refused(value: str) -> None:
    assert not gtin_valid(value)
    assert check("ean", value) is not None
    assert type_from_length(value) is None


def test_none_is_refused() -> None:
    assert not gtin_valid(None)  # type: ignore[arg-type]
    assert check("ean", None) is not None


def _check_digit(body: str) -> str:
    digits = [int(d) for d in body.zfill(13)]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits))
    return str((10 - total % 10) % 10)


@given(
    st.sampled_from([8, 12, 13, 14]).flatmap(
        lambda n: st.text(alphabet="0123456789", min_size=n - 1, max_size=n - 1)
    ),
    st.integers(min_value=1, max_value=9),
)
def test_property_check_digit(body: str, shift: int) -> None:
    digit = _check_digit(body)
    assert gtin_valid(body + digit)
    other = str((int(digit) + shift) % 10)
    assert not gtin_valid(body + other)
