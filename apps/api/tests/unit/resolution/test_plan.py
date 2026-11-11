"""SI11 T11 (AC16; P:B1, D16): the pure resolution planner, its lanes, overrides and causes."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.services.auto_fix.policy import fact_key
from app.services.issue_questions import _image_index
from app.services.resolution.plan import (
    ADAPTER_SOURCES,
    AUTOMATIC,
    SELLER_ACTIONS,
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
    """Fix round 3 (R3-2..R3-4), kept by T15: a content or keyword error the content fix cannot
    change (no `content_fixable`) never lands in `auto_content` or `ai_field` (nothing writes
    for them), nor in a `seller` question (the seller's inbox asks only about facts and images):
    a marketplace's refusal goes to a staff content review; Kuinn's own check of the text is the
    seller's `edit_content` action (QA 2026-11-09: no staff task stands behind it)."""
    steps = plan([_issue(field_path=path, error_code=code.value, source=source)], PlanContext())
    assert all(s.lane not in ("auto_content", "ai_field") for s in steps), steps
    for s in steps:
        if s.lane == "seller":
            assert s.action == "edit_content" and source not in ADAPTER_SOURCES, s
    assert lane_for(code.value, "content_error") != "auto_content"


def test_a_rules_engine_title_too_long_is_the_sellers_edit() -> None:
    """QA: a seller's own 231-character title read "Kuinn team: our team is checking this"."""
    issue = _issue(source="rules_engine", field_path="content.title",
                   error_code="FIELD_TOO_LONG", error_class="invalid_value")  # fmt: skip
    (step,) = plan([issue], PlanContext())
    assert (step.lane, step.action) == ("seller", "edit_content")
    refused = _issue(field_path="content.title", error_code="FIELD_TOO_LONG",
                     error_class="invalid_value")  # fmt: skip
    (step,) = plan([refused], PlanContext())
    assert (step.lane, step.action) == ("staff", "content_review")


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
        if step.action == "edit_content":
            # QA 2026-11-09: Kuinn's own check of the seller's text; an edit, no question
            assert source not in ADAPTER_SOURCES and path.startswith(("content.", "keywords.")), (
                path,
                code,
            )
            continue
        if step.lane == "seller" and step.action in SELLER_ACTIONS:
            # SI13 (CP-D8): an action on the listing, no inbox question, on its own path only
            assert (code, step.action) in (
                (ErrorCode.CATEGORY_MISMATCH, "recategorize"),
                (ErrorCode.IDENTIFIER_DUPLICATE, "edit_identifier"),
                (ErrorCode.IDENTIFIER_INVALID, "edit_identifier"),
                (ErrorCode.IDENTIFIER_MISSING, "edit_identifier"),
            ), (path, code)
            assert fact_key(path) is not None, path
            continue
        if step.lane == "seller":
            assert seller_answers(path), (path, code)
        if step.lane == "staff" and step.action in ("mapping", "unanswerable_fact"):
            assert not seller_answers(path)


@settings(max_examples=400, deadline=None)
@given(
    path=st.one_of(_PATHS, st.text(max_size=40)),
    code=st.sampled_from(sorted(ErrorCode, key=str)),
    source=st.sampled_from(SOURCES),
    klass=st.sampled_from(sorted(ErrorClass, key=str)),
    ai_allowed=st.booleans(),
)
def test_no_seller_step_without_a_fact_key_or_an_image_index(
    path: str, code: ErrorCode, source: str, klass: ErrorClass, ai_allowed: bool
) -> None:
    """The seller inbox opens a question only from a fact key or an image index; a path with
    neither is never planned `seller` (it would open nothing and stall)."""
    if fact_key(path) is not None or _image_index(path) is not None:
        return
    issue = _issue(field_path=path, error_code=code.value, source=source, error_class=klass.value)
    for step in plan([issue], PlanContext(ai_allowed=ai_allowed)):
        # `edit_content` opens no question (SELLER_ACTIONS): the seller edits the listing
        assert step.lane != "seller" or step.action == "edit_content", (path, code, source, klass)


@pytest.mark.parametrize(
    ("path", "action"),
    [("listing", "mapping"), ("brand.name", "unanswerable_fact"), ("request", "unanswerable_fact")],
)
def test_a_path_with_no_question_is_staff(path: str, action: str) -> None:
    issue = _issue(field_path=path, error_code="MISSING_ATTRIBUTE", error_class="missing_data")
    (step,) = plan([issue], PlanContext())
    assert (step.lane, step.action) == ("staff", action)


# ---- SI11 T15: the content fix lane ----


@pytest.mark.parametrize("path", ["content.title", "content.bullets[1]", "keywords.search_terms"])
def test_a_fixable_content_refusal_takes_auto_content(path: str) -> None:
    issue = _issue(field_path=path, error_code="FIELD_TOO_LONG", error_class="invalid_value")
    (step,) = plan([issue], PlanContext(content_fixable=frozenset({issue.id})))
    assert (step.lane, step.action, step.cause) == ("auto_content", "content_fix", "ai_content")


def test_content_fixable_never_moves_a_fact_or_an_unfixable_row() -> None:
    fact = _issue(field_path="attributes.color", error_code="FIELD_TOO_LONG")
    other = _issue(field_path="content.title", error_code="FIELD_TOO_LONG")
    steps = plan([fact, other], PlanContext(content_fixable=frozenset({fact.id})))
    assert [s.lane for s in steps] == ["seller", "staff"]


# ---- SI13 T7: category, burst hold, SKU and identifier rules ----


def _cat(**over: Any) -> IssueView:
    return _issue(
        field_path="facts.category",
        error_code="CATEGORY_MISMATCH",
        error_class="category_mismatch",
        marketplace_code=None,
        **over,
    )


def _missing(attribute: str) -> IssueView:
    return _issue(
        field_path=f"attributes.{attribute}",
        error_code="MISSING_ATTRIBUTE",
        error_class="missing_data",
        marketplace_code="90220",
    )


def test_category_mismatch_recategorizable_is_seller() -> None:
    (step,) = plan([_cat()], PlanContext(recategorizable=True))
    assert (step.lane, step.action, step.cause) == ("seller", "recategorize", "mapping")


def test_category_mismatch_otherwise_is_staff() -> None:
    (step,) = plan([_cat()], PlanContext())
    assert (step.lane, step.action) == ("staff", "recategorize_review")


def test_lane_for_category_without_context_is_staff() -> None:
    assert lane_for("CATEGORY_MISMATCH", "category_mismatch") == "staff"


def test_burst_hold_one_step_only() -> None:
    issues = [_cat(), *(_missing(a) for a in ("fabric_type", "neck_style", "sleeve_type"))]
    steps = plan(issues, PlanContext(recategorizable=True))
    assert [(s.lane, s.action) for s in steps] == [("seller", "recategorize")]
    held = plan(issues[1:], PlanContext())
    assert len(held) == 3


def test_a_warning_category_row_holds_nothing() -> None:
    issues = [_cat(severity="warning"), _missing("fabric_type")]
    assert len(plan(issues, PlanContext())) == 1


@pytest.mark.parametrize(
    "over",
    [
        {"error_code": "SKU_CONFLICT", "error_class": "identifier_error"},
        {"error_code": "SELLER_SKU_CONFLICT", "error_class": "identifier_error"},
        {"marketplace_code": "4005013"},
        {"marketplace_code": "101168"},
    ],
)
def test_sku_and_locked_codes_go_to_staff(over: dict[str, Any]) -> None:
    issue = _issue(**{"field_path": "listing", **over})
    for ctx in (PlanContext(), PlanContext(repairable=frozenset({issue.id}), ai_allowed=True)):
        (step,) = plan([issue], ctx)
        assert (step.lane, step.action) == ("staff", "sku_conflict")


@pytest.mark.parametrize("path", ["facts.identifier", "variants[A-1].facts.identifier"])
def test_other_brand_barcode_is_seller_edit(path: str) -> None:
    issue = _issue(
        field_path=path,
        error_code="IDENTIFIER_DUPLICATE",
        error_class="identifier_error",
        marketplace_code="catalog:other_brand",
    )
    (step,) = plan([issue], PlanContext())
    assert (step.lane, step.action) == ("seller", "edit_identifier")


@pytest.mark.parametrize("path", ["facts.identifier", "variants[A-1].facts.identifier"])
@pytest.mark.parametrize(
    ("code", "marketplace_code"),
    [("IDENTIFIER_INVALID", "90188"), ("IDENTIFIER_MISSING", "8550")],
)
def test_amazon_barcode_or_exemption_refusal_is_seller_edit(
    path: str, code: str, marketplace_code: str
) -> None:
    """Item 13: a barcode Amazon calls invalid, or a create Amazon refuses for want of a barcode
    or an approved GTIN exemption, sends the seller back to the barcode choice."""
    issue = _issue(
        field_path=path,
        error_code=code,
        error_class="identifier_error",
        marketplace_code=marketplace_code,
    )
    (step,) = plan([issue], PlanContext())
    assert (step.lane, step.action) == ("seller", "edit_identifier")


@pytest.mark.parametrize("source", ["rules_engine", "schema"])
def test_local_identifier_findings_stay_off_the_seller_action(source: str) -> None:
    """Only a marketplace's own refusal opens the seller action; Kuinn's own checks keep their
    lanes."""
    issue = _issue(
        source=source,
        field_path="facts.identifier",
        error_code="IDENTIFIER_INVALID",
        error_class="identifier_error",
    )
    for step in plan([issue], PlanContext()):
        assert step.action != "edit_identifier"


@pytest.mark.parametrize("path", ["variants[A-1]", "listing", "facts.identifier_type"])
def test_identifier_duplicate_elsewhere_is_not_seller(path: str) -> None:
    issue = _issue(
        field_path=path, error_code="IDENTIFIER_DUPLICATE", error_class="identifier_error"
    )
    for step in plan([issue], PlanContext()):
        assert step.lane != "seller" or step.action != "edit_identifier"
        assert step.lane != "seller" or seller_answers(path)
