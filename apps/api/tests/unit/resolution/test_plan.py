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
    seller_answers,
)
from hypothesis import given, settings
from hypothesis import strategies as st
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


@pytest.mark.parametrize("path", ["keywords.search_terms", "content.title", "content.bullets[0]"])
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("code", sorted(ErrorCode, key=str))
def test_a_content_path_never_takes_a_lane_without_a_writer(
    code: ErrorCode, source: str, path: str
) -> None:
    """Fix round 3 (R3-2..R3-4): until the content auto-fix (T15) and the AI rewrite (T16) are
    built, a content or keyword error never lands in `auto_content` or `ai_field` (nothing writes
    for them), nor in `seller` (the seller's inbox asks only about facts and images): it goes
    to a staff content review."""
    steps = plan([_issue(field_path=path, error_code=code.value, source=source)], PlanContext())
    assert all(s.lane not in ("auto_content", "ai_field", "seller") for s in steps), steps
    assert lane_for(code.value, "content_error") != "auto_content"


def test_a_keyword_length_error_is_a_staff_content_review() -> None:
    issue = _issue(field_path="keywords.search_terms", error_code="FIELD_TOO_LONG",
                   error_class="invalid_value")  # fmt: skip
    (step,) = plan([issue], PlanContext())
    assert (step.lane, step.action) == ("staff", "content_review")


_PATHS = st.one_of(
    st.sampled_from(
        [
            "listing",
            "brand.name",
            "request",
            "variants[SKU-1]",
            "variants[SKU-1].facts.gender",
            "variants[SKU-1].facts.mrp",
            "facts.gender",
            "facts.brand_name",
            "facts.mrp",
            "facts.manufacturer",
            "facts.attributes.color",
            "attributes.color",
            "images[0]",
            "images[12]",
            "images",
            "content.title",
            "keywords.search_terms",
        ]
    ),
    st.from_regex(
        r"(attributes|facts|facts\.attributes|brand|request)\.[a-z_]{1,12}", fullmatch=True
    ),
)


@settings(max_examples=400, deadline=None)
@given(
    path=_PATHS,
    code=st.sampled_from(sorted(ErrorCode, key=str)),
    source=st.sampled_from(SOURCES),
    klass=st.sampled_from(sorted(ErrorClass, key=str)),
)
def test_a_seller_step_is_always_one_the_inbox_can_ask(
    path: str, code: ErrorCode, source: str, klass: ErrorClass
) -> None:
    """Round 4 (R4-1): `seller` only on an image or an answerable fact key; anything else is a
    staff step (`mapping` on `listing`), never a question the inbox cannot open."""
    issue = _issue(field_path=path, error_code=code.value, source=source, error_class=klass.value)
    for step in plan([issue], PlanContext(ai_allowed=True)):
        if step.lane == "seller":
            assert seller_answers(path), (path, code)
        if step.lane == "staff" and step.action in ("mapping", "unanswerable_fact"):
            assert not seller_answers(path)


@pytest.mark.parametrize(
    ("path", "action"),
    [("listing", "mapping"), ("brand.name", "unanswerable_fact"), ("request", "unanswerable_fact")],
)
def test_a_path_with_no_question_is_staff(path: str, action: str) -> None:
    issue = _issue(field_path=path, error_code="MISSING_ATTRIBUTE", error_class="missing_data")
    (step,) = plan([issue], PlanContext())
    assert (step.lane, step.action) == ("staff", action)
