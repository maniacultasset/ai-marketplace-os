"""SI8 T11 (AC12): the Amazon bullet and description rules carry the limits of the stored
LISTING definitions (the hand-written fixtures until DL-A stores the real ones)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

CASES = (("ethnic_wear_kurta_sets", "KURTA"), ("tshirts", "SHIRT"))


def _seed(repo_root: Path) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(
        (repo_root / "packages/schemas/fixtures/rules.seed.json").read_text(encoding="utf-8")
    )
    return loaded


def _ptd(repo_root: Path, product_type: str) -> dict[str, Any]:
    path = repo_root / f"apps/api/tests/fixtures/amazon_ptd/{product_type}.listing.schema.json"
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _limit(rules: list[dict[str, Any]], field: str, kind: str) -> int:
    (rule,) = [r for r in rules if r["field"] == field and r["rule_def"]["type"] == kind]
    assert rule["severity"] == "error" and rule["rule_def"]["error_code"] == "FIELD_TOO_LONG"
    return int(rule["rule_def"]["limit"])


@pytest.mark.parametrize(("category", "product_type"), CASES)
def test_text_rule_limits_equal_the_listing_definition(
    repo_root: Path, category: str, product_type: str
) -> None:
    (ruleset,) = [
        s for s in _seed(repo_root) if s["marketplace"] == "amazon" and s["category"] == category
    ]
    rules = ruleset["rules"]
    props = _ptd(repo_root, product_type)["properties"]
    bullet = props["bullet_point"]
    description = props["product_description"]
    assert _limit(rules, "content.bullets", "max_items") == bullet["maxItems"]
    assert (
        _limit(rules, "content.bullets[*]", "max_length")
        == bullet["items"]["properties"]["value"]["maxLength"]
    )
    assert (
        _limit(rules, "content.description", "max_length")
        == description["items"]["properties"]["value"]["maxLength"]
    )
