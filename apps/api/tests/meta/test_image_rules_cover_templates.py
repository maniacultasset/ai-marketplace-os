"""SI8 T11 (#8; AC12): every (marketplace, category) with a seeded template has at least one main
image rule in the rules seed, for that marketplace, with the category or a NULL category (a NULL
row applies to every category it is not shadowed in)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _templates() -> set[tuple[str, str]]:
    from app.db.seed_ops import platform_template_loads
    from app.db.seed_templates import flipkart_seed_templates, seed_templates

    found = {
        (s.template.marketplace.value, s.template.category)
        for s in (*seed_templates(), *flipkart_seed_templates())
    }
    found |= {
        (t.template.marketplace.value, t.template.category) for t in platform_template_loads()
    }
    return found


def _uncovered(seed: list[dict[str, Any]], templates: set[tuple[str, str]]) -> set[tuple[str, str]]:
    mains = {
        (ruleset["marketplace"], rule["category"])
        for ruleset in seed
        for rule in ruleset["rules"]
        if rule["enabled"]
        and rule["rule_def"]["type"] == "image_requirement"
        and rule["rule_def"].get("slot") == "main"
    }
    return {
        (market, category)
        for market, category in templates
        if (market, category) not in mains and (market, None) not in mains
    }


def _seed(repo_root: Path) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(
        (repo_root / "packages/schemas/fixtures/rules.seed.json").read_text(encoding="utf-8")
    )
    return loaded


def test_every_templated_category_has_a_main_image_rule(repo_root: Path) -> None:
    templates = _templates()
    assert ("amazon", "tshirts") in templates and ("flipkart", "tshirts") in templates
    assert _uncovered(_seed(repo_root), templates) == set()


def test_the_check_fails_without_the_null_category_rows(repo_root: Path) -> None:
    seed = _seed(repo_root)
    for ruleset in seed:
        ruleset["rules"] = [r for r in ruleset["rules"] if r["category"] is not None]
    assert ("amazon", "tshirts") in _uncovered(seed, _templates())
