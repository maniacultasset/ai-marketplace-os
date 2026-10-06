"""S14a tester round 2: shapes around the fix-round-1 overlap guard that must stay refused
(optional groups, alternations with an empty or repeating arm, classes that overlap a category)."""

from __future__ import annotations

import pytest
from app.services.rules_engine.text import UnsafeRuleRegexError, compile_rule_regex

REFUSED = [
    r"(?:\d+)?(?:\d+)?(?:\d+)?x",
    r"(\d+)?\d+x",
    r"\d+(?:a|\d)\d+x",
    r"(?:\d|a)?\d*x",
    r"\d*(?:\d{2})?\d*x",
    r"(?:\d{2}){0,1}\d*\d",
    r"\d*-?\d*-?\d*-?\d*x",
    r"[0-9]*\d*x",
    r"(?i)[a-z]*[A-Z]*1",
    "\\w*[\u0966-\u096f]*x",
    "\\s*\u3000*x",
    r"(?:\d*)?\d*x",
    r"(?:(?:\d+)?)?(?:(?:\d+)?)?x",
    r"\d{0,3}(?:,\d{0,3})?\d+x",
    r"(?:a\d*)?\d*x",
    r"\d*(?:a|b|)\d*x",
    r"(?:a|\d*)\d*x",
    r"\D*[^a]*x",
    r"[^x]*[^y]*x",
]


@pytest.mark.parametrize("regex", REFUSED)
def test_regex_guard_recheck_overlap_shapes_refused(regex: str) -> None:
    with pytest.raises(UnsafeRuleRegexError):
        compile_rule_regex(regex, ignore_case=False)
