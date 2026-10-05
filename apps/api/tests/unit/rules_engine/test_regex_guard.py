"""S14a AC 15: the engine refuses polynomial-backtracking rule regexes (PLAN D6 option B)."""

from __future__ import annotations

import time

import pytest
from app.services.rules_engine import text as text_mod
from app.services.rules_engine.text import UnsafeRuleRegexError, compile_rule_regex
from mkos_schemas.rules import check_rule_regex

AC15_REFUSED = [r"\s*\s*x", r"\d*\d*\d*\d*x", r".*.*.*.*x", r"\s*\s*\s*\s*\s*\s*!", r".*foo.*"]
SAFE = [r"^\d+(\.\d+)?$", r"\d+\.\d+", r"https?://.*"]
ADVERSARIAL = ["7" * 5000, "http" * 1250, " " * 5000]


@pytest.mark.parametrize("regex", AC15_REFUSED)
def test_regex_polynomial_pattern_budget_refused(regex: str) -> None:
    with pytest.raises(UnsafeRuleRegexError):
        compile_rule_regex(regex, ignore_case=False)


@pytest.mark.parametrize("regex", SAFE)
def test_regex_polynomial_pattern_budget(regex: str) -> None:
    pattern = compile_rule_regex(regex, ignore_case=False)
    for text in ADVERSARIAL:
        start = time.perf_counter()
        pattern.search(text)
        assert time.perf_counter() - start < 0.1


def test_regex_polynomial_gap_documented() -> None:
    # The S2 contract gate still accepts this; when it is tightened this test fails and the
    # KNOWN-ISSUES row goes.
    assert check_rule_regex(r"\s*\s*x") == r"\s*\s*x"


@pytest.mark.parametrize(
    ("regex", "safe"),
    [
        (r"\d+\.\d+", True),
        (r"^\d+(\.\d+)?$", True),
        (r"https?://.*", True),
        (r"a*b*", True),
        (r"[a-z]*[0-9]*", True),
        (r"^[A-Z]{2}\d{6,}$", True),
        (r"[^,]*,[^,]*", True),
        (r"\s*\s*x", False),
        (r"\d*\d*\d*\d*x", False),
        (r".*.*.*.*x", False),
        (r"\s*\s*\s*\s*\s*\s*!", False),
        (r".*foo.*", False),
        (r"\w*\d*", False),
        (r"(?i)a*A*", False),
        (r"[^a]*b*", False),
        (r"(?s).*\n.*", False),
        (r".*\n.*", True),
    ],
)
def test_regex_overlap_table(regex: str, safe: bool) -> None:
    if safe:
        compile_rule_regex(regex, ignore_case=False)
    else:
        with pytest.raises(UnsafeRuleRegexError):
            compile_rule_regex(regex, ignore_case=False)


def test_regex_ignore_case_argument_widens_overlap() -> None:
    compile_rule_regex(r"a*A*", ignore_case=False)
    with pytest.raises(UnsafeRuleRegexError):
        compile_rule_regex(r"a*A*", ignore_case=True)


@pytest.mark.parametrize(
    ("regex", "safe"),
    [
        (r"(?:x|\d*)\d*", False),
        (r"(?:x|y)\d*", True),
        (r"\d*a?\d*", False),
        (r"\d*(?:a)\d*", True),
        (r"\d*(?:a|\d)\d*", False),
        (r"\d*a{1,3}\d*", True),
        (r"\d*a{0,3}\d*", False),
        (r"\d+\b\d+", False),
        (r"(?:ab)*b*", False),
        (r"(?i:a)*A*", False),
    ],
)
def test_regex_overlap_branch_and_optional_group(regex: str, safe: bool) -> None:
    if safe:
        compile_rule_regex(regex, ignore_case=False)
    else:
        with pytest.raises(UnsafeRuleRegexError):
            compile_rule_regex(regex, ignore_case=False)


def test_regex_s2_gate_and_syntax_errors_refused() -> None:
    for regex in ["(a+)+", "(", "(?=a)b"]:
        with pytest.raises(UnsafeRuleRegexError):
            compile_rule_regex(regex, ignore_case=False)


def test_regex_overlap_guard_must_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard-must-fail proof: with the guard disabled, every refused pattern compiles (only a
    # short input is ever searched).
    monkeypatch.setattr(text_mod, "_overlapping_repeat", lambda *_a, **_k: None)
    for regex in AC15_REFUSED:
        compile_rule_regex(regex, ignore_case=False).search("x" * 20)
