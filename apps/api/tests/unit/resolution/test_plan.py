"""SI11 T11 (AC16; P:B1, D16): the pure resolution planner, its lanes, overrides and causes."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.services.resolution.plan import (
    AUTOMATIC,
    IssueView,
    PlanContext,
    answerable,
    lane_for,
    plan,
)
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass

SOURCES = ("schema", "rules_engine", "vision_model", "amazon_adapter", "flipkart_adapter")


def _issue(**over: Any) -> IssueView:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "source": "amazon_adapter",
        "field_path": "attributes.color",
        "error_code": "VALUE_NOT_ALLOWED",
        "error_class": "invalid_value",
        "severity": "error",
        "marketplace_code": "99300",
    }
    return IssueView(**{**base, **over})


def _lane(issue: IssueView, **ctx: Any) -> str:
    (step,) = plan([issue], PlanContext(**ctx))
    return step.lane


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("code", sorted(ErrorCode, key=str))
def test_every_code_and_source_gets_a_lane(code: ErrorCode, source: str) -> None:
    steps = plan([_issue(error_code=code.value, source=source)], PlanContext(ai_allowed=True))
    assert len(steps) == 1 and steps[0].lane


def test_repairable_is_auto_repair() -> None:
    issue = _issue()
    assert _lane(issue, repairable=frozenset({issue.id})) == "auto_repair"


def test_critical_key_with_ai_field_is_seller() -> None:
    issue = _issue(
        source="rules_engine", field_path="attributes.fiber_composition", error_code="FACT_MISMATCH"
    )
    assert (
        _lane(issue, ai_allowed=True, critical_keys=frozenset({"fiber_composition"})) != "ai_field"
    )


def test_image_never_ai_and_unknown_class_is_staff() -> None:
    image = _issue(
        field_path="images[0]", error_code="IMAGE_NON_COMPLIANT", error_class="media_error"
    )
    assert _lane(image, ai_allowed=True) not in AUTOMATIC
    unknown = _issue(
        error_code="MARKETPLACE_UNKNOWN_ERROR", error_class="unknown", field_path="listing"
    )
    assert _lane(unknown) == "staff"


def test_compliance_gate_is_never_automatic() -> None:
    issue = _issue(source="rules_engine", field_path="content.title", error_code="BANNED_TERM",
                   error_class="compliance_error", gate="compliance")  # fmt: skip
    assert _lane(issue, ai_allowed=True) not in AUTOMATIC


def test_recurrence_of_a_fixed_key_is_staff_even_if_repairable() -> None:
    issue = _issue(recurrence_resolution="deterministic_fix")
    assert _lane(issue, repairable=frozenset({issue.id})) == "staff"
    answered = _issue(recurrence_resolution="human_answer")
    assert _lane(answered, repairable=frozenset({answered.id})) == "auto_repair"


def test_a_refused_live_repair_is_staff_even_if_repairable() -> None:
    """Round 1 R1-2: the marketplace refused the value Kuinn already repaired (another code, so
    no recurrence): one automatic guess per attribute, then staff."""
    issue = _issue(marketplace_code="99301")
    ctx = PlanContext(repairable=frozenset({issue.id}), repair_rejected=frozenset({issue.id}))
    (step,) = plan([issue], ctx)
    assert (step.lane, step.action) == ("staff", "repair_rejected")


def test_marketplace_lane() -> None:
    gated = _issue(error_class=ErrorClass.ACCOUNT_PERMISSION.value, field_path="listing")
    assert _lane(gated) == "marketplace"
    assert _lane(_issue(marketplace_code="99320", field_path="listing")) == "marketplace"


@pytest.mark.parametrize(
    ("path", "lane"),
    [("facts.gender", "seller"), ("attributes.colour", "seller"), ("facts.manufacturer", "staff")],
)
def test_missing_fact_answerable_or_staff(path: str, lane: str) -> None:
    issue = _issue(field_path=path, error_code="MISSING_ATTRIBUTE", error_class="missing_data")
    assert _lane(issue) == lane
    if path.startswith("facts."):
        assert answerable(path) is (lane == "seller")


def test_no_ai_when_not_allowed() -> None:
    issues = [_issue(source="rules_engine", field_path="content.description", error_code=c.value,
                     error_class="content_error") for c in ErrorCode]  # fmt: skip
    assert all(s.lane != "ai_field" for s in plan(issues, PlanContext(ai_allowed=False)))


def test_warnings_make_no_step_and_empty_is_empty() -> None:
    assert plan([_issue(severity="warning")], PlanContext()) == ()
    assert plan([], PlanContext()) == ()


@pytest.mark.parametrize(
    ("issue", "cause"),
    [
        (_issue(field_path="attributes.color"), "seller_fact"),
        (_issue(field_path="content.title"), "ai_content"),
        (
            _issue(
                field_path="images[0]", error_code="IMAGE_NON_COMPLIANT", error_class="media_error"
            ),
            "image",
        ),
        (_issue(field_path="listing", error_class="account_permission"), "marketplace"),
        (_issue(field_path="listing"), "mapping"),
        (_issue(source="rules_engine", field_path="listing"), "unknown"),
    ],
)
def test_cause_table(issue: IssueView, cause: str) -> None:
    (step,) = plan([issue], PlanContext())
    assert step.cause == cause


def test_ai_provenance_is_ai_content() -> None:
    issue = _issue(field_path="attributes.neckline")
    (step,) = plan([issue], PlanContext(provenance={"neckline": "ai"}))
    assert step.cause == "ai_content"


def test_lane_for_has_no_listing_context() -> None:
    assert lane_for("MARKETPLACE_UNKNOWN_ERROR", "unknown") == "staff"
    assert lane_for("VALUE_NOT_ALLOWED", "account_permission") == "marketplace"
