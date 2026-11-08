"""SI5 T10 (PLAN §2.4): `product_identifier.plan`, the pure change set of the two body fields
against each level and each stored status."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from app.core.errors import InputError
from app.services.product_identifier import IdentifierChange, plan
from mkos_schemas.codes import ErrorCode

EAN = ("ean", "4006381333931")
UPC = ("upc", "036000291452")
STORED: dict[str, tuple[Any, Any, Any]] = {
    "none": (None, None, None),
    "provided": ("ean", "4006381333931", "provided"),
    "exempt": (None, None, "exempt"),
    "not_applicable": (None, None, "not_applicable"),
}


def _row(state: str) -> Any:
    t, v, s = STORED[state]
    return SimpleNamespace(identifier_type=t, identifier_value=v, identifier_status=s)


def _plan(level: str, state: str, **body: Any) -> IdentifierChange | None:
    return plan(
        level,
        _row(state),
        identifier=body.get("product_identifier"),
        exemption=body.get("identifier_exemption"),
        sent=set(body),
    )


@pytest.mark.parametrize("level", ["variant", "standalone"])
@pytest.mark.parametrize("state", sorted(STORED))
def test_set_a_barcode(level: str, state: str) -> None:
    got = _plan(level, state, product_identifier=UPC)
    assert got == IdentifierChange("upc", "036000291452", "provided")
    same = _plan(level, state, product_identifier=EAN)
    assert same is None if state == "provided" else same == IdentifierChange(*EAN, "provided")


@pytest.mark.parametrize("level", ["variant", "standalone"])
@pytest.mark.parametrize(
    ("state", "expected"),
    [("provided", IdentifierChange(None, None, None)), ("none", None), ("exempt", None),
     ("not_applicable", None)],
)  # fmt: skip
def test_null_clears_only_a_provided_barcode(level: str, state: str, expected: Any) -> None:
    assert _plan(level, state, product_identifier=None) == expected


@pytest.mark.parametrize("level", ["variant", "standalone"])
@pytest.mark.parametrize(
    ("state", "flag", "expected"),
    [
        ("none", True, IdentifierChange(None, None, "exempt")),
        ("provided", True, IdentifierChange(None, None, "exempt")),
        ("exempt", True, None),
        ("not_applicable", True, IdentifierChange(None, None, "exempt")),
        ("exempt", False, IdentifierChange(None, None, None)),
        ("provided", False, None),
        ("none", False, None),
    ],
)
def test_exemption(level: str, state: str, flag: bool, expected: Any) -> None:
    assert _plan(level, state, identifier_exemption=flag) == expected


def test_null_barcode_with_exemption_is_exempt() -> None:
    got = _plan("variant", "provided", product_identifier=None, identifier_exemption=True)
    assert got == IdentifierChange(None, None, "exempt")


def test_barcode_with_exemption_false_sets_the_barcode() -> None:
    got = _plan("variant", "exempt", product_identifier=EAN, identifier_exemption=False)
    assert got == IdentifierChange(*EAN, "provided")


def test_both_refused() -> None:
    with pytest.raises(InputError) as caught:
        _plan("standalone", "none", product_identifier=EAN, identifier_exemption=True)
    assert caught.value.field == "identifier_exemption"


def test_nothing_sent_is_no_change() -> None:
    for state in STORED:
        assert _plan("variant", state) is None


def test_style_refuses_a_barcode_and_plans_nothing_else() -> None:
    with pytest.raises(InputError) as caught:
        _plan("style", "none", product_identifier=EAN)
    assert caught.value.code is ErrorCode.FACT_LEVEL_INVALID
    assert caught.value.field == "product_identifier"
    assert _plan("style", "none", product_identifier=None) is None
    assert _plan("style", "none", identifier_exemption=True) is None


@pytest.mark.parametrize(
    "bad", [("upc", "4006381333931"), ("ean", "4006381333932"), ("ean", "036000291452")]
)
def test_bad_barcodes_are_422_at_the_value(bad: tuple[str, str]) -> None:
    with pytest.raises(InputError) as caught:
        _plan("variant", "none", product_identifier=bad)
    assert (caught.value.code, caught.value.field) == (
        ErrorCode.VALIDATION_FAILED,
        "product_identifier.value",
    )


def test_a_new_row() -> None:
    got = plan("standalone", None, identifier=EAN, exemption=None, sent={"product_identifier"})
    assert got == IdentifierChange(*EAN, "provided")
