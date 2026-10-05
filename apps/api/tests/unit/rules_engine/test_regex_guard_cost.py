"""S14a fix round 2 (R2-1, R2-2, R2-3): the overlap guard's own cost stays bounded. Nested
optional groups are walked once per (node, open set) instead of twice per level (2**depth), atom
overlap is a mask intersection instead of pairwise probe matching, the guard fails closed on a
step budget and an open-set cap, and a refusal is memoised like a compiled regex.
Moved from docs/plans/s14a/repro/test_repro_tester_r2.py."""

from __future__ import annotations

import contextlib
import time

import pytest
from app.services.rules_engine import evaluate
from app.services.rules_engine import text as text_mod
from app.services.rules_engine.text import (
    UnsafeRuleRegexError,
    compile_rule_regex,
    compiled_rule_regex,
)
from tests.rules_engine_builders import listing, rule, ruleset


def _nested_optional(depth: int, group: str = "(") -> str:
    return group * depth + "a" + ")?" * depth


def _elapsed_compile(regex: str) -> float:
    start = time.perf_counter()
    with contextlib.suppress(UnsafeRuleRegexError):
        compile_rule_regex(regex, ignore_case=False)
    return time.perf_counter() - start


def _cjk_optional_term(i: int, atoms: int) -> str:
    return "".join(chr(0x4E00 + (i * atoms + j) % 20000) + "?" for j in range(atoms))


@pytest.mark.parametrize("group", ["(", "(?:"])
@pytest.mark.parametrize("depth", [18, 22])
def test_guard_linear_on_nested_optional_groups(depth: int, group: str) -> None:
    # tester and code review: 2.3 s at depth 18, 36 s at depth 22 before the fix
    assert _elapsed_compile(_nested_optional(depth, group)) < 0.05


@pytest.mark.parametrize(
    ("depth", "group"),
    [(60, "("), (85, "("), (51, "(?:")],  # the deepest under 256 chars
)
def test_guard_deep_nested_optional_under_50ms(depth: int, group: str) -> None:
    regex = _nested_optional(depth, group)
    assert len(regex) <= 256
    assert _elapsed_compile(regex) < 0.05
    compile_rule_regex(regex, ignore_case=False)  # still accepted: nothing overlaps


def test_pattern_rule_with_nested_optional_does_not_stall_evaluate() -> None:
    r = rule(
        {"type": "pattern", "name": "n", "regex": _nested_optional(22), "must": "not_match"},
        field="content.description",
    )
    start = time.perf_counter()
    evaluate(listing(content={"description": "hello"}), ruleset(r))
    assert time.perf_counter() - start < 1.0  # 2.4 s at depth 18 before the fix


def test_regex_banned_terms_guard_cost_bounded() -> None:
    # 500 terms of 60 distinct optional CJK atoms: accepted, and the guard is not quadratic.
    terms = [_cjk_optional_term(i, 60) for i in range(500)]
    r = rule(
        {"type": "banned_terms", "terms": terms, "match": "regex"}, field="content.description"
    )
    start = time.perf_counter()
    evaluate(listing(content={"description": "hello"}), ruleset(r))
    assert time.perf_counter() - start < 5.0  # about 60 s for 128-atom terms before the fix


def test_wide_open_set_refused_fast_and_refusal_memoised() -> None:
    term = _cjk_optional_term(7, 128)  # 128 open atoms, above the cap: fail closed
    assert _elapsed_compile(term) < 0.05  # 0.109 s per term before the fix
    with pytest.raises(UnsafeRuleRegexError, match="too complex"):
        compiled_rule_regex(term, ignore_case=False)
    start = time.perf_counter()
    for _ in range(200):
        with pytest.raises(UnsafeRuleRegexError, match="too complex"):
            compiled_rule_regex(term, ignore_case=False)
    assert time.perf_counter() - start < 0.05  # refusals come from the cache


def test_guard_step_budget_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    regex = _nested_optional(10)
    compile_rule_regex(regex, ignore_case=False)
    monkeypatch.setattr(text_mod, "_GUARD_STEP_BUDGET", 5)
    with pytest.raises(UnsafeRuleRegexError, match="too complex"):
        compile_rule_regex(regex, ignore_case=False)


def test_guard_open_set_cap_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    regex = "a?b?c?d?x"
    compile_rule_regex(regex, ignore_case=False)
    monkeypatch.setattr(text_mod, "_MAX_OPEN_ATOMS", 2)
    with pytest.raises(UnsafeRuleRegexError, match="too complex"):
        compile_rule_regex(regex, ignore_case=False)


def test_mask_overlap_uses_non_base_probes() -> None:
    # Overlap on a character outside the base probes must still be seen: the CJK literal is
    # inside the open class range only through the atom's own probe.
    with pytest.raises(UnsafeRuleRegexError):
        compile_rule_regex("[一-鿿]*中*x", ignore_case=False)
    compile_rule_regex("[一-丏]*中*x", ignore_case=False)
