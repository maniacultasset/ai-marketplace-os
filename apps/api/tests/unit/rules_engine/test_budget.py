"""S14a T13: the engine's time budget on worst-case input (PLAN §6 performance)."""

from __future__ import annotations

import time
from typing import Any

from app.services.rules_engine import RuleRow, evaluate, resolve_ruleset
from mkos_schemas.enums import Marketplace
from tests.rules_engine_builders import CATEGORY, LOADED_AT, TENANT_ID, listing, rule, ruleset
from tests.rules_engine_strategies import POOL

STAR_TERMS = [
    "best seller",
    "bestseller",
    "#1",
    "number 1",
    "100% guaranteed",
    "free shipping",
    "cure",
    "cures",
    "doctor recommended",
    "fda approved",
    "sale",
    "discount",
    "cheapest",
]
WORDS = [
    "soft",
    "cotton",
    "kurta",
    "set",
    "best",
    "secure",
    "cure",
    "#1",
    "number",
    "1",
    "free",
    "shipping",
]


def _text(size: int, seed: int) -> str:
    out: list[str] = []
    i = seed
    while sum(len(w) + 1 for w in out) < size:
        out.append(WORDS[i % len(WORDS)])
        i += 7
    return " ".join(out)[:size].strip() or "x"


def max_listing() -> Any:
    return listing(
        content={
            "title": _text(500, 1),
            "bullets": [_text(1000, i) for i in range(10)],
            "description": _text(5000, 3),
        },
        keywords={
            "search_terms": [_text(256, i) for i in range(50)],
            "hidden_terms": [_text(256, i + 50) for i in range(50)],
        },
        attributes={f"a{i:03d}": _text(256, i) for i in range(126)}
        | {"color": "black", "fabric_type": "cotton"},
    )


def seed_like_rules() -> list[Any]:
    """The S9/T12 seed rows for one RuleSet (rules.seed.json is S9's file; see DEVIATIONS)."""
    return [
        rule({"type": "max_length", "limit": 200}),
        rule({"type": "min_length", "limit": 5}),
        rule(
            {
                "type": "banned_terms",
                "terms": ["best", "guaranteed", "free shipping", "best seller", "guaranteed cure"],
            }
        ),
        rule({"type": "banned_terms", "terms": STAR_TERMS}, field="*", category=None),
        rule({"type": "required_attribute", "attribute": "color"}, field="attributes.color"),
        rule(
            {"type": "required_attribute", "attribute": "fabric_type"},
            field="attributes.fabric_type",
        ),
    ]


def test_engine_worst_case_budget() -> None:
    item = max_listing()
    rows = [RuleRow(r, i + 1) for i, r in enumerate(seed_like_rules() + POOL)]
    start = time.perf_counter()
    rs = resolve_ruleset(
        rows,
        marketplace=Marketplace.AMAZON,
        category=CATEGORY,
        tenant_id=TENANT_ID,
        loaded_at=LOADED_AT,
    )
    rep = evaluate(item, rs)
    assert time.perf_counter() - start < 1.0
    assert rep.status == "failed"


def test_engine_500_rules_budget() -> None:
    item = max_listing()
    rules = [
        rule(
            {"type": "banned_terms", "terms": [f"term{r}x{t}" for t in range(499)] + ["cure"]},
            field="*",
        )
        for r in range(500)
    ]
    rs = ruleset(*rules)
    start = time.perf_counter()
    rep = evaluate(item, rs)
    assert time.perf_counter() - start < 5.0
    assert len(rep.errors) == 500
