"""S14a fix round 1 (R1-1, R1-2, R1-4): the overlap guard also refuses stacked overlapping
bounded and optional repeats, which backtrack polynomially on stdlib re (PLAN D6 option B)."""

from __future__ import annotations

import time

import pytest
from app.services.rules_engine import evaluate
from app.services.rules_engine.text import UnsafeRuleRegexError, compile_rule_regex
from tests.rules_engine_builders import listing, rule, ruleset
from tests.timing import budget

BOUNDED_REFUSED = [
    r"\d{1,50}\d{1,50}\d{1,50}\d{1,50}x",  # tester repro: 32.8 s over 2000 digits
    r"\d{0,100}\d{0,100}\d{0,100}x",  # tester repro: 8.0 s over 3000 digits
    r"\d{0,1000}\d{0,1000}\d{0,1000}x",  # code review: 122 s over 1000 digits
    r".{0,500}.{0,500}.{0,500}x",  # code review: 61 s over 1500 chars
    r"\d{0,60}\d{0,60}\d{0,60}\d{0,60}x",  # security review
    r"\d{1,3000}\d{1,3000}x",
    r"\d+\d{1,3000}x",
    r"\d{1,3000}\d+x",
    r"\d{0,8}\d{0,8}\d{0,8}\d{0,8}\d{0,8}\d{0,8}x",  # narrow but stacked: over 5 s
    r"\d{2,4}\d{2,4}",
    "\\d?" * 20 + "x",  # optional atoms stacked: over 5 s
    "\\d*" + "\\d?" * 10 + "x",
    r"\d*\d?x",
    r"(?:ab)?(?:ab)?(?:ab)?x",
    r"\d*a{0,3}\d*",
]
BOUNDED_SAFE = [
    r"\d{2,4}-\d{2,4}",
    r"\d{3}",
    r"\d{3}\d{3}",
    r"^\d+(\.\d+)?$",
    r"https?://.*",
    r"-?\d+",
    r"^\+?\d+$",
    r"\d*a{1,3}\d*",
    r"^[A-Z]{2}\d{6,}$",
]
ADVERSARIAL = ["1" * 5000, "1" * 2000 + "!", "a" * 5000, "ab" * 2500]


@pytest.mark.parametrize("regex", BOUNDED_REFUSED)
def test_regex_overlapping_bounded_repeats_refused(regex: str) -> None:
    with pytest.raises(UnsafeRuleRegexError):
        compile_rule_regex(regex, ignore_case=False)


@pytest.mark.parametrize("regex", BOUNDED_SAFE)
def test_regex_disjoint_bounded_repeats_allowed_and_fast(regex: str) -> None:
    pattern = compile_rule_regex(regex, ignore_case=False)
    for text in ADVERSARIAL:
        start = time.perf_counter()
        pattern.search(text)
        assert time.perf_counter() - start < budget(
            0.5
        )  # backtracking blow-ups take seconds; headroom for load


def test_regex_bounded_pattern_rule_fails_closed_fast() -> None:
    # Tester repro: evaluate() over a 1200-digit description took several seconds.
    r = rule(
        {"type": "pattern", "name": "sku", "regex": BOUNDED_REFUSED[0], "must": "not_match"},
        field="content.description",
    )
    item = listing(content={"description": "1" * 1200})
    start = time.perf_counter()
    with pytest.raises(UnsafeRuleRegexError):
        evaluate(item, ruleset(r))
    assert time.perf_counter() - start < budget(1.0)
