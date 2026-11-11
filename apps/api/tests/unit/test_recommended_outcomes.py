"""Client feedback item 10: a missing recommended attribute never moves a listing.

The generation outcome (`map_outcome`) and a human check point (`human_move`) read it as clean,
so a re-check after the new template versions never voids an approval; any other warning still
asks for review.
"""

from __future__ import annotations

from app.services.generation_result import map_outcome
from app.services.listings import human_move
from app.services.rules_engine import evaluate
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import AgentBlockStatus
from tests.rules_engine_builders import listing, ruleset, template
from tests.unit.generation_builders import context, result

LISTING = result(context()).listing
OK_BLOCK = AgentBlockStatus(agent="content", status="ok")


def _recommendation_only() -> ErrorReport:
    report = evaluate(
        listing(attributes={}), ruleset(), template=template(recommended_attributes=["fit"])
    )
    assert report.status == "passed" and len(report.errors) == 1
    return report


def _with_warning(report: ErrorReport) -> ErrorReport:
    other = ErrorItem(
        field="content.title",
        error_code=ErrorCode.FIELD_TOO_LONG,
        severity=Severity.WARNING,
        message="m",
        source=ErrorSource.RULES_ENGINE,
        retryable=False,
    )
    return report.model_copy(update={"errors": [*report.errors, other]})


def test_generation_recommendation_alone_is_validated() -> None:
    move = map_outcome("generated", LISTING, _recommendation_only(), [OK_BLOCK])
    assert (move.to, move.event) == ("validated", "generation_clean")


def test_generation_other_warning_still_needs_review() -> None:
    move = map_outcome("generated", LISTING, _with_warning(_recommendation_only()), [OK_BLOCK])
    assert (move.to, move.event) == ("needs_review", "generation_warnings")


def test_generation_pipeline_needs_review_is_kept() -> None:
    move = map_outcome("needs_review", LISTING, _recommendation_only(), [OK_BLOCK])
    assert move.to == "needs_review"


def test_human_move_recommendation_alone_is_validated() -> None:
    assert human_move(_recommendation_only()) == ("validated", None)
    assert human_move(_with_warning(_recommendation_only())) == ("needs_review", None)


def test_human_move_blocking_still_blocks() -> None:
    report = evaluate(
        listing(attributes={}),
        ruleset(),
        template=template(required_attributes=["color"], recommended_attributes=["fit"]),
    )
    assert human_move(report)[0] == "blocked"
