"""S14a tester round 1: attacks on the Rules Engine that it must survive (AC 2, 9, 11, 15)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from app.services.rules_engine import evaluate
from app.services.rules_engine.report import render_text
from app.services.rules_engine.text import UnsafeRuleRegexError, compile_rule_regex
from tests.rules_engine_builders import listing, rule, ruleset


def _banned(terms: list[str], field: str = "*", **kw: Any) -> Any:
    sev = {"severity": kw.pop("severity")} if "severity" in kw else {}
    return rule(
        {"type": "banned_terms", "terms": terms, "message": "Banned: {term}", **kw},
        field=field,
        **sev,
    )


@pytest.mark.parametrize(
    "regex",
    [r"(\d|\d)*x", r"(?:a|ab)*c", r"\d*a?\d*a?\d*x", r"(\d*)(\d*)x", r"(?i)[a-z]*[A-Z]*x"],
)
def test_attack_regex_guard_refuses_nested_and_split_repeats(regex: str) -> None:
    with pytest.raises(UnsafeRuleRegexError):
        compile_rule_regex(regex, ignore_case=False)


def test_attack_fullwidth_and_em_space_still_flagged() -> None:
    item = listing(content={"title": "\uff22\uff25\uff33\uff34\u2003kurta \uff03\uff11"})
    errs = evaluate(item, ruleset(_banned(["best", "#1"]))).errors
    assert [e.message for e in errs] == ["Banned: best, #1"]


def test_attack_word_term_needs_boundary_after_combining_mark() -> None:
    # "best" followed by a combining acute is another word; no false positive.
    item = listing(content={"title": "best\u0301 kurta"})
    assert evaluate(item, ruleset(_banned(["best"]))).errors == []


def test_attack_500_cap_keeps_errors_before_warnings_and_is_order_free() -> None:
    bullets = [f"best bullet {i}" for i in range(10)]
    item = listing(content={"title": "best kurta", "bullets": bullets})
    warns = [_banned(["best"], severity="warning") for _ in range(60)]
    error = _banned(["best"], field="content.title")
    rules = [*warns, error]
    first = evaluate(item, ruleset(*rules))
    rules = rules[len(rules) // 2 :] + rules[: len(rules) // 2][::-1]
    second = evaluate(item, ruleset(*rules))
    assert len(first.errors) == 500
    assert first.model_dump_json() == second.model_dump_json()
    assert first.status == "failed"
    assert first.errors[0].severity.value == "error"
    assert first.errors[0].rule_id == error.id


def test_attack_warnings_only_report_passes() -> None:
    item = listing(content={"title": "best kurta"})
    report = evaluate(item, ruleset(_banned(["best"], severity="warning")))
    assert report.status == "passed"
    assert report.error_code is None
    assert len(report.errors) == 1


def test_attack_max_size_description_banned_and_pattern_fast() -> None:
    desc = ("\U0001f600 \ufdfa best " * 600)[:5000]
    item = listing(content={"description": desc})
    pattern = rule(
        {"type": "pattern", "name": "no_urls", "regex": r"https?://.*", "must": "not_match"},
        field="content.description",
    )
    start = time.perf_counter()
    report = evaluate(item, ruleset(_banned(["best"], field="content.description"), pattern))
    assert time.perf_counter() - start < 1.0
    assert [e.error_code.value for e in report.errors] == ["BANNED_TERM"]


def test_attack_hostile_message_template_stays_literal() -> None:
    # The contract refuses these (rules.py:238); the renderer must still never evaluate them.
    hostile = "{value!r:>99999} {value.__class__} {limit[0]} {{value}} {term"
    out = render_text(hostile, {"value": "v" * 5000, "limit": "5"})
    assert out == hostile
    assert render_text("{value}", {"value": "v" * 5000}) == "v" * 200
