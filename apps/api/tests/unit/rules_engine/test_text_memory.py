"""S14a fix round 1 (R1-3): no process-wide cache holds listing text. Work on a text is
memoised per ``evaluate`` call only, so a long-running worker's memory stays flat."""

from __future__ import annotations

from typing import Any

from app.services.rules_engine import evaluate
from app.services.rules_engine import text as text_mod
from app.services.rules_engine.text import TextMemo, find_terms
from tests.rules_engine_builders import listing, rule, ruleset


def _cached_entries() -> int:
    total = 0
    for value in vars(text_mod).values():
        info: Any = getattr(value, "cache_info", None)
        if callable(info):
            total += info().currsize
    return total


def test_text_caches_do_not_grow_with_distinct_texts() -> None:
    terms = ["best seller", "guaranteed", "cure"]
    for case_sensitive in (False, True):  # warm the term-keyed matcher caches
        find_terms("warm", terms, match="word", case_sensitive=case_sensitive)
    before = _cached_entries()
    for i in range(40):
        text = f"listing {i} " + "cotton kurta " * 400
        for case_sensitive in (False, True):
            find_terms(text, terms, match="word", case_sensitive=case_sensitive)
            text_mod.normalise(text, case_sensitive=case_sensitive)
    assert _cached_entries() == before


def test_text_memo_shares_work_within_one_call() -> None:
    memo = TextMemo()
    text = "Best Seller cotton kurta"
    first = find_terms(text, ["best seller"], match="word", case_sensitive=False, memo=memo)
    second = find_terms(text, ["best seller"], match="word", case_sensitive=False, memo=memo)
    assert first == second == ("best seller",)
    assert len(memo.norms) == 1
    assert len(memo.hits) == 1
    assert memo.gram_sets


def test_evaluate_with_text_rules_leaves_caches_flat() -> None:
    r = rule({"type": "banned_terms", "terms": ["best seller"]}, field="*")
    evaluate(listing(content={"description": "warm up"}), ruleset(r))
    before = _cached_entries()
    for i in range(20):
        evaluate(listing(content={"description": f"best seller {i} " * 200}), ruleset(r))
    assert _cached_entries() == before
