"""Client feedback item 10: a missing recommended attribute never moves a listing.

The generation outcome (`map_outcome`) and a human check point (`human_move`) read it as clean,
so a re-check after the new template versions never voids an approval; any other warning still
asks for review. The issue sync never opens a row (so never a question) for one, a validation
run never counts one as a warning, and the pipeline's Rules Engine port never carries one into
the correction loop, so the model never fills one in.
"""

from __future__ import annotations

import uuid

from app.services.generation_result import map_outcome
from app.services.issues import issue_items
from app.services.listings import human_move
from app.services.rules_engine import evaluate, is_recommendation, warning_count
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import AgentBlockStatus
from tests.rules_engine_builders import listing, rule, ruleset, template
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


def test_issue_sync_skips_a_recommendation() -> None:
    report = _with_warning(_recommendation_only())
    items = issue_items(report, "rules_engine")
    assert [item.field for item in items.values()] == ["content.title"]
    assert not any(is_recommendation(item) for item in items.values())


def test_issue_sync_scope_and_recommendation_together() -> None:
    report = _with_warning(_recommendation_only())
    assert issue_items(report, "rules_engine", scope_fields=["attributes.fit"]) == {}


def test_warning_count_leaves_out_recommendations() -> None:
    assert warning_count(_recommendation_only()) == 0
    assert warning_count(_with_warning(_recommendation_only())) == 1
    blocked = evaluate(
        listing(attributes={}),
        ruleset(),
        template=template(required_attributes=["color"], recommended_attributes=["fit"]),
    )
    assert (len(blocked.blocking), warning_count(blocked)) == (1, 0)


async def test_pipeline_port_never_carries_a_recommendation() -> None:
    from app.core.config import Settings
    from app.services.pipeline_deps import BackendPipelineDeps, _ModelConfig
    from app.services.rules_loader import LoadedRules
    from mkos_schemas.enums import Marketplace

    warn = rule(
        {"type": "required_attribute", "attribute": "neckline", "message": "neckline helps"},
        field="attributes.neckline",
        severity="warning",
    )
    tpl = template(required_attributes=["color"], recommended_attributes=["fit", "pattern"])
    cfg = Settings(_env_file=None, app_env="local", database_url="postgresql+psycopg://x@h/db")
    deps = BackendPipelineDeps(
        tenant_id=uuid.UUID("00000000-0000-0000-0000-0000000000cc"),
        job_id=uuid.uuid4(),
        user_id=None,
        marketplace=Marketplace.AMAZON,
        rules=LoadedRules(ruleset=ruleset(warn), template=tpl),
        product=None,
        image_reports={},
        embedder=None,
        models=_ModelConfig.from_settings(cfg),
        critical_keys=frozenset(),
        rejections={},
    )
    report = await deps.validate_rules(listing(attributes={}))
    assert not any(is_recommendation(item) for item in report.errors)
    assert {(e.field, e.severity) for e in report.errors} == {
        ("attributes.color", Severity.ERROR),
        ("attributes.neckline", Severity.WARNING),
    }
