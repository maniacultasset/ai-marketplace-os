"""SI13 round 2 re-check (tester): pins the round 1 fixes against the attack inputs of the
findings R1-1 (brand forms), R1-2/R1-3 (diagnosis guard) and R1-6 (export lanes on the seed)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.adapters.amazon.precheck import brand_relation
from app.services.resolution.diagnosis import DiagnosisOut, Invalid, check, redact
from app.services.resolution.export import rows

SKU = "KURTA-NAVY-M"
GOOD = {
    "plain_cause": "Amazon needs a different product type for this item.",
    "lane_class": "seller",
    "error_code": "CATEGORY_MISMATCH",
    "error_class": None,
    "field_hint": None,
    "suggested_fix": "Choose the category that matches the product, then send it.",
}
SEED = Path(__file__).parents[2] / "app" / "db" / "seed_data" / "error_map.amazon.json"


@pytest.mark.parametrize(
    ("ours", "theirs", "want"),
    [
        ("Mama Earth", "Mama-Earth", "same"),
        ("Zebra", "ZEBRA ®", "same"),
        ("Zebra™", "Zebra Co", "similar"),
        ("Haldiram's", "HALDIRAM’S", "same"),  # noqa: RUF001
        ("Zebra", "Acme", "other"),
        ("Zebra", "Zebrafish", "other"),
    ],
)
def test_brand_forms(ours: str, theirs: str, want: str) -> None:
    assert brand_relation(ours, theirs) == want


@pytest.mark.parametrize(
    "cause",
    [
        "Open //evil.example/fix to repair this item.",
        "Run javascript:alert(1) to repair this item.",
        "Go to evil-example.com/fix to repair the item.",
        "Go to ｈｔｔｐｓ：／／evil now.",  # noqa: RUF001
        "Amazon refused KURTA​-NAVY-M for this item.",
        "Amazon refused the value ⁦x⁩ for this item.",
    ],
)
def test_the_guard_refuses_round_one_attacks(cause: str) -> None:
    out = DiagnosisOut.model_validate({**GOOD, "plain_cause": cause})
    found = check(out, field_index=frozenset(), critical_keys=frozenset(), secrets=[SKU])
    assert isinstance(found, Invalid), cause


@pytest.mark.parametrize(
    "message",
    [
        "SKU KURTA​-NAVY-M is invalid",
        "SKU ＫＵＲＴＡ-NAVY-M is invalid",  # noqa: RUF001
        "SKU kurta-navy-m is invalid",
        "SKU KURTA-⁦NAVY-M is invalid",
    ],
)
def test_redaction_survives_hidden_and_folded_forms(message: str) -> None:
    text = redact(message, [(SKU, "{sku}")])
    assert text == "SKU {sku} is invalid", text


def _seed_rows() -> list[Any]:
    data = json.loads(SEED.read_text())
    out = []
    for r in data if isinstance(data, list) else data["rows"]:
        base = {"version": 1, "enabled": True, "policy_ref": None, "marketplace": "amazon"}
        out.append(SimpleNamespace(**{**base, **r}))
    return out


@pytest.mark.parametrize(
    ("code", "want"),
    [
        ("100980", "Seller"),
        ("4005015", "Seller"),
        ("4000003", "Seller (Kuinn team once live)"),
        ("101158", "Seller (Kuinn team once live)"),
    ],
)
def test_export_labels_the_si13_seller_codes(code: str, want: str) -> None:
    labels = {r.code: r.who for r in rows(_seed_rows())}
    assert labels[code] == want
