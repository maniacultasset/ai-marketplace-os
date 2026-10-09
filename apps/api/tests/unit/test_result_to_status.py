"""S18 T14 (AC 5; CP1; DATABASE.md:827): the backend's re-check and the terminal-state mapping."""

from __future__ import annotations

from typing import Any

import pytest
from app.services.generation_result import map_outcome, recheck
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import AgentBlockStatus
from tests.unit.generation_builders import CLAIM, context, result


def _report(*severities: Severity) -> ErrorReport:
    items = [
        ErrorItem(
            field="content.title",
            error_code=ErrorCode.FIELD_TOO_LONG,
            severity=s,
            message="m",
            source=ErrorSource.RULES_ENGINE,
            retryable=False,
        )
        for s in severities
    ]
    status = "failed" if Severity.ERROR in severities else "passed"
    return ErrorReport(status=status, errors=items)


CLEAN = _report()
ERROR = _report(Severity.ERROR)
WARNING = _report(Severity.WARNING)
OK_BLOCK = AgentBlockStatus(agent="content", status="ok")
FAILED_BLOCK = AgentBlockStatus(agent="image", status="failed")
LISTING = result(context()).listing

TABLE = [
    (
        "generated_clean",
        "generated",
        LISTING,
        CLEAN,
        [OK_BLOCK],
        ("validated", "generation_clean", None),
    ),
    (
        "generated_error",
        "generated",
        LISTING,
        ERROR,
        [OK_BLOCK],
        ("blocked", "corrections_exhausted", "corrections_exhausted"),
    ),
    (
        "generated_warning",
        "generated",
        LISTING,
        WARNING,
        [OK_BLOCK],
        ("needs_review", "generation_warnings", None),
    ),
    (
        "failed_block",
        "generated",
        LISTING,
        CLEAN,
        [FAILED_BLOCK],
        ("needs_review", "generation_warnings", None),
    ),
    (
        "needs_review",
        "needs_review",
        LISTING,
        CLEAN,
        [],
        ("needs_review", "generation_warnings", None),
    ),
    ("blocked_clean", "blocked", LISTING, CLEAN, [], ("needs_review", "generation_warnings", None)),
    (
        "blocked_error",
        "blocked",
        LISTING,
        ERROR,
        [],
        ("blocked", "corrections_exhausted", "corrections_exhausted"),
    ),
    ("failed", "failed", LISTING, CLEAN, [], ("failed", "generation_failed", "generation_failed")),
    (
        "null_listing",
        "generated",
        None,
        None,
        [],
        ("failed", "generation_failed", "generation_failed"),
    ),
]


@pytest.mark.parametrize(
    ("terminal", "listing", "report", "blocks", "expected"),
    [row[1:] for row in TABLE],
    ids=[row[0] for row in TABLE],
)
def test_result_to_status(
    terminal: str, listing: Any, report: Any, blocks: Any, expected: tuple[str, str, Any]
) -> None:
    move = map_outcome(terminal, listing, report, blocks)
    assert (move.to, move.event, move.reason) == expected
    assert move.saves_version is (expected[0] != "failed")


@settings(max_examples=100, deadline=None)
@given(
    terminal=st.sampled_from(["generated", "needs_review", "blocked", "failed", "other"]),
    has_listing=st.booleans(),
    severities=st.lists(st.sampled_from(list(Severity)), max_size=3),
    blocks=st.lists(st.sampled_from([OK_BLOCK, FAILED_BLOCK]), max_size=3),
)
def test_map_never_approved_or_publishing(
    terminal: str, has_listing: bool, severities: list[Severity], blocks: list[Any]
) -> None:
    move = map_outcome(terminal, LISTING if has_listing else None, _report(*severities), blocks)
    assert move.to in {"validated", "needs_review", "blocked", "failed"}
    assert move.to not in {"approved", "publishing", "published"}


def test_recheck_runs_the_engine() -> None:
    from app.services.rules_loader import LoadedRules
    from mkos_schemas.rules import Rule, RuleSet

    ctx = context()
    long = result(ctx, long_title=True).listing
    assert long is not None
    rule = Rule.model_validate(
        {
            "id": "00000000-0000-4000-8000-00000000f001",
            "marketplace": "amazon",
            "category": ctx.category,
            "field": "content.title",
            "rule_def": {
                "type": "max_length",
                "limit": 200,
                "message": "Title is {value} characters; the limit is {limit}.",
                "policy_ref": {"source": "amazon_policy", "title": "Titles"},
            },
            "version": 1,
            "enabled": True,
            "severity": "error",
        }
    )
    rules = LoadedRules(
        ruleset=RuleSet(marketplace="amazon", category=ctx.category, rules=[rule], loaded_at=CLAIM),
        template=None,
    )
    report = recheck(long, rules, None)
    assert [e.error_code for e in report.blocking] == [ErrorCode.FIELD_TOO_LONG]


def test_recheck_engine_refusal_fails_closed() -> None:
    from app.services.rules_loader import LoadedRules
    from mkos_schemas.rules import RuleSet

    listing = result(context()).listing
    assert listing is not None
    other_scope = LoadedRules(
        ruleset=RuleSet(marketplace="flipkart", category="other_cat", loaded_at=CLAIM),
        template=None,
    )
    report = recheck(listing, other_scope, None)
    assert not report.passed and report.error_code is ErrorCode.INTERNAL_ERROR
