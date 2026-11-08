"""SI10 T18 (AC16): live photo accuracy from the existing `product.fact_updated` audit rows (no
new write): per key, how often a seller confirmed, rejected or changed a photo-read value."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from app.services.photo_accuracy import KeyAccuracy, classify_photo_reviews, summarise


def _photo(value: str, confidence: float = 0.9) -> dict[str, Any]:
    return {
        "value": value,
        "normalized": value,
        "source": "ai",
        "captured_via": "agent",
        "confidence": confidence,
        "evidence_ref": "image:7b0d",
        "updated_at": "2026-11-07T10:00:00Z",
    }


def _seller(value: str) -> dict[str, Any]:
    return {"value": value, "normalized": value, "source": "seller", "captured_via": "form"}


def _row(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, str]:
    return json.dumps({"attributes": before}), json.dumps({"attributes": after})


def test_confirm_reject_change() -> None:
    before = {"color": _photo("red"), "sleeve": _photo("long"), "neck": _photo("round")}
    after = {
        "color": {**_photo("red"), "confirmed_by": "u1", "confirmed_at": "2026-11-07T11:00:00Z"},
        "sleeve": None,
        "neck": _seller("v-neck"),
    }
    assert classify_photo_reviews(*_row(before, after)) == {
        "color": "confirmed",
        "sleeve": "rejected",
        "neck": "changed",
    }


def test_non_photo_and_missing_befores_skipped() -> None:
    before = {"fabric": _seller("cotton"), "color": None}
    after = {"fabric": _seller("silk"), "color": _seller("blue")}
    assert classify_photo_reviews(*_row(before, after)) == {}


def test_key_absent_from_after_skipped() -> None:
    assert classify_photo_reviews(*_row({"color": _photo("red")}, {})) == {}


def test_confirm_needs_the_same_value() -> None:
    after = {"color": {**_photo("blue"), "confirmed_at": "2026-11-07T11:00:00Z"}}
    assert classify_photo_reviews(*_row({"color": _photo("red")}, after)) == {"color": "changed"}


def test_malformed_rows_skipped() -> None:
    assert classify_photo_reviews("{not json", "{}") == {}
    assert classify_photo_reviews(json.dumps([1, 2]), json.dumps({"attributes": {}})) == {}
    assert classify_photo_reviews(json.dumps({"attributes": "x"}), json.dumps({})) == {}
    assert classify_photo_reviews(None, None) == {}


def test_unicode_keys() -> None:
    before = {"रंग": _photo("लाल")}
    assert classify_photo_reviews(*_row(before, {"रंग": None})) == {"रंग": "rejected"}


def test_summarise_rates_and_confidence() -> None:
    rows = [
        _row(
            {"color": _photo("red", 0.9)},
            {"color": {**_photo("red"), "confirmed_at": "t"}},
        ),
        _row({"color": _photo("red", 0.7)}, {"color": None}),
        _row({"color": _photo("red", 0.8)}, {"color": _seller("blue")}),
        _row({"sleeve": _photo("long", 0.6)}, {"sleeve": {**_photo("long"), "confirmed_at": "t"}}),
        ("broken", "rows"),
    ]
    assert summarise(rows) == [
        KeyAccuracy("color", 1, 1, 1, Decimal("0.33"), Decimal("0.80")),
        KeyAccuracy("sleeve", 1, 0, 0, Decimal("1.00"), Decimal("0.60")),
    ]


def test_summarise_empty() -> None:
    assert summarise([]) == []
