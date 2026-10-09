"""S14a tester round 3 re-check of R2-1/R2-2/R2-3: the guard stays cheap on nested optional
shapes the round 2 tests did not cover (alternation inside, case-folding, stacked levels), and
ordinary rule regexes are still accepted under the step budget and open-set cap."""

from __future__ import annotations

import time

import pytest
from app.services.rules_engine.text import UnsafeRuleRegexError, compile_rule_regex
from tests.timing import budget


def _elapsed(regex: str, *, ignore_case: bool = False) -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        compile_rule_regex(regex, ignore_case=ignore_case)
        accepted = True
    except UnsafeRuleRegexError:
        accepted = False
    return accepted, time.perf_counter() - start


@pytest.mark.parametrize("ignore_case", [False, True])
@pytest.mark.parametrize(
    "regex",
    [
        "(" * 40 + "a|b" + ")?" * 40,
        "(?:" * 30 + "ab|cd" + ")?" * 30,
        "(" * 25 + "a?b" + ")?" * 25,
        "(" * 35 + "a" + "){0,1}" * 35,
    ],
)
def test_nested_optional_variants_fast(regex: str, ignore_case: bool) -> None:
    assert len(regex) <= 256
    _, elapsed = _elapsed(regex, ignore_case=ignore_case)
    assert elapsed < budget(0.05)  # machine-scaled (tests/timing.py)


@pytest.mark.parametrize(
    "regex",
    [
        r"[\w.+-]+@[\w-]+\.[\w.]+",
        r"https?://[^\s/]+(?:/[^\s]*)?",
        r"(?:free|cheap|best|guaranteed|miracle|cure)?\s*(?:offer|deal)",
        "".join(c + "?" for c in "abcdefghijklmnopqrstuvwxyz") + "!",
        "(?:" + "|".join(f"word{i}" for i in range(30)) + ")",
    ],
)
def test_ordinary_regexes_still_accepted(regex: str) -> None:
    accepted, elapsed = _elapsed(regex)
    assert accepted
    assert elapsed < budget(0.05)  # machine-scaled (tests/timing.py)


def test_nested_optional_with_overlap_still_refused() -> None:
    # The memoised walk must not lose the refusal: an open \d* survives the optional nest.
    with pytest.raises(UnsafeRuleRegexError):
        compile_rule_regex(r"\d*" + "(" * 20 + r"\d" + ")?" * 20 + "x", ignore_case=False)
