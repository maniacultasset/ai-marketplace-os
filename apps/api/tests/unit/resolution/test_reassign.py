"""SI13 T6 (P:A1, D8; AC1, AC2; CP-D9, CP-D10): the pure reassignment signal and its item."""

from __future__ import annotations

import pytest
from app.services.resolution.reassign import (
    REASSIGN_BURST,
    category_item,
    clean_type,
    label_of,
    signal,
)
from mkos_schemas.enums import Severity


def test_type_change() -> None:
    assert signal("PANTS", "KURTA", 0, accepted_before=False, item_open=False) == "type_change"


@pytest.mark.parametrize(
    ("read", "sent"), [("KURTA", "KURTA"), (None, "KURTA"), ("PANTS", None), ("pants", "KURTA")]
)
def test_no_type_change(read: str | None, sent: str | None) -> None:
    assert signal(read, sent, 0, accepted_before=True, item_open=False) is None


def test_burst_boundaries() -> None:
    assert REASSIGN_BURST == 3
    assert signal(None, "KURTA", 2, accepted_before=True, item_open=False) is None
    assert signal(None, "KURTA", 3, accepted_before=True, item_open=False) == "burst"
    # three long-standing rows and no item: not a new burst
    assert signal(None, "KURTA", 3, accepted_before=True, item_open=False, missing_before=3) is None
    # sticky while the item is open
    assert (
        signal(None, "KURTA", 3, accepted_before=True, item_open=True, missing_before=3) == "burst"
    )


def test_burst_needs_an_accepted_sku() -> None:
    assert signal(None, "KURTA", 5, accepted_before=False, item_open=False) is None


@pytest.mark.parametrize("value", ["pants", "P" * 65, "PAN TS", "PANTS\n", "<b>", 3, None])
def test_hostile_type_never_echoed(value: object) -> None:
    assert clean_type(value) is None
    item = category_item("type_change", value, None)  # type: ignore[arg-type]
    assert str(value) not in item.message or value is None


def test_item_shape_and_suggestion() -> None:
    item = category_item("type_change", "PANTS", "pants_trousers")
    assert (item.field, item.error_code, item.severity) == (
        "facts.category",
        "CATEGORY_MISMATCH",
        Severity.ERROR,
    )
    assert item.source == "amazon_adapter" and item.raw_code is None
    assert item.message == (
        "Amazon now files this product as PANTS. Choose the category that matches it, then "
        "regenerate the listing. Suggested: pants trousers."
    )


def test_burst_item_leaves_the_type_out() -> None:
    item = category_item("burst", "PANTS", None)
    assert "PANTS" not in item.message and "Suggested" not in item.message


def test_label_refuses_odd_keys() -> None:
    assert label_of("ethnic_wear_kurta_sets") == "ethnic wear kurta sets"
    assert label_of("<script>") is None
    assert "Suggested" not in category_item("type_change", "PANTS", "Bad Key!").message
