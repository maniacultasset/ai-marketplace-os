"""SI4 A1 (AC1): the fix policy routes every error code, and the safety overrides win."""

from __future__ import annotations

import uuid

import pytest
from app.services.auto_fix.policy import (
    POLICY,
    TRIM_MAX_SHARE,
    Route,
    Routing,
    ask_kind,
    fact_key,
    route,
)
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, FixAction, Gate, Severity
from mkos_schemas.errors import ErrorItem

CRITICAL = frozenset({"fiber_composition", "country_of_origin"})


def _item(**over: object) -> ErrorItem:
    base: dict[str, object] = {
        "field": "content.title",
        "error_code": ErrorCode.FIELD_TOO_LONG,
        "severity": Severity.ERROR,
        "message": "m",
        "source": ErrorSource.RULES_ENGINE,
        "retryable": False,
    }
    return ErrorItem.model_validate({**base, **over})


def _route(item: ErrorItem, critical: frozenset[str] = CRITICAL) -> Routing:
    return route(item, source="rules_engine", critical_keys=critical)


# ---- the table covers every code ----


def test_policy_covers_every_error_code() -> None:
    assert set(POLICY) == set(ErrorCode)


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_code_has_a_row(code: ErrorCode) -> None:
    row = POLICY[code]
    assert isinstance(row.route, Route)
    if row.route is Route.DETERMINISTIC:
        assert row.fix_action is not None
    else:
        assert row.fix_action is None
    if row.task_kind is not None:
        assert row.route is Route.ASK


def test_trim_share_is_a_decimal() -> None:
    assert str(TRIM_MAX_SHARE) == "0.15"


# ---- happy paths ----


@pytest.mark.parametrize(
    ("code", "field"),
    [
        (ErrorCode.FIELD_TOO_LONG, "content.title"),
        (ErrorCode.BYTE_BUDGET_EXCEEDED, "keywords.hidden_terms"),
        (ErrorCode.KEYWORD_DUPLICATE, "keywords.search_terms"),
    ],
)
def test_reformat_rows(code: ErrorCode, field: str) -> None:
    got = _route(_item(error_code=code, field=field))
    assert got.route is Route.DETERMINISTIC
    assert got.fix_action is FixAction.REFORMAT
    assert ask_kind(got) is None


def test_value_not_allowed_remaps_with_a_question_fallback() -> None:
    got = _route(_item(error_code=ErrorCode.VALUE_NOT_ALLOWED, field="attributes.fit"))
    assert (got.route, got.fix_action) == (Route.DETERMINISTIC, FixAction.REMAP_VALUE)
    assert got.fallback_task_kind == "missing_fact"


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.FIELD_TOO_SHORT,
        ErrorCode.BANNED_TERM,
        ErrorCode.PATTERN_MISMATCH,
        ErrorCode.UNSUPPORTED_CLAIM,
    ],
)
def test_ai_field_rows(code: ErrorCode) -> None:
    got = _route(_item(error_code=code))
    assert got.route is Route.AI_FIELD
    assert ask_kind(got) is None


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.MISSING_ATTRIBUTE,
        ErrorCode.CRITICAL_FACT_MISSING,
        ErrorCode.REQUIRED_FIELD_MISSING,
        ErrorCode.PRICE_ABOVE_MRP,
        ErrorCode.FACT_NEEDS_CONFIRMATION,
        ErrorCode.FACT_MISMATCH,
    ],
)
def test_missing_fact_rows(code: ErrorCode) -> None:
    got = _route(_item(error_code=code, field="attributes.color"))
    assert (got.route, got.task_kind) == (Route.ASK, "missing_fact")
    assert ask_kind(got) == "missing_fact"


def test_image_row() -> None:
    got = _route(_item(error_code=ErrorCode.IMAGE_NON_COMPLIANT, field="images[0]"))
    assert (got.route, got.task_kind) == (Route.ASK, "image_replacement")


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.RATE_LIMITED,
        ErrorCode.MARKETPLACE_UNAVAILABLE,
        ErrorCode.SUBMISSION_NOT_APPLIED,
        ErrorCode.MARKETPLACE_UNCONFIRMED,
        ErrorCode.TIMEOUT,
    ],
)
def test_retry_rows(code: ErrorCode) -> None:
    assert _route(_item(error_code=code, field="listing")).route is Route.RETRY


def test_unknown_marketplace_error_row() -> None:
    got = _route(_item(error_code=ErrorCode.MARKETPLACE_UNKNOWN_ERROR, field="listing"))
    assert (got.route, got.task_kind) == (Route.ASK, "marketplace_error_review")


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.UNAUTHENTICATED,
        ErrorCode.IDEMPOTENCY_KEY_REUSED,
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.INTERNAL_ERROR,
        ErrorCode.JOB_TIMEOUT,
        ErrorCode.BUDGET_EXCEEDED,
    ],
)
def test_ops_rows(code: ErrorCode) -> None:
    got = _route(_item(error_code=code, field="request"))
    assert got.route is Route.OPS
    assert ask_kind(got) is None


# ---- the safety overrides (AC1) ----


_OVERRIDES = [
    pytest.param({"field": "attributes.fiber_composition"}, "critical_key", id="critical-key"),
    pytest.param({"gate": Gate.COMPLIANCE}, "gate:compliance", id="gate-compliance"),
    pytest.param({"gate": Gate.COMMERCIAL}, "gate:commercial", id="gate-commercial"),
    pytest.param({"error_class": ErrorClass.UNKNOWN}, "class_unknown", id="class-unknown"),
    pytest.param({"field": "images[0]"}, "image", id="image-path"),
]


@pytest.mark.parametrize(("over", "reason"), _OVERRIDES)
def test_override_beats_a_deterministic_route(over: dict[str, object], reason: str) -> None:
    item = _item(
        **{
            "error_code": ErrorCode.VALUE_NOT_ALLOWED,
            "field": "attributes.fit",
            **over,
        }
    )
    got = _route(item)
    assert got.route is Route.ASK
    assert got.fix_action is None
    assert got.reason == reason


@pytest.mark.parametrize(("over", "reason"), _OVERRIDES)
def test_override_beats_an_ai_field_route(over: dict[str, object], reason: str) -> None:
    item = _item(**{"error_code": ErrorCode.BANNED_TERM, **over})
    if "field" not in over:
        item = item.model_copy(update={"field": "content.title"})
    got = _route(item)
    assert got.route is Route.ASK
    assert got.reason == reason


def test_image_code_override_on_a_text_path() -> None:
    item = _item(
        error_code=ErrorCode.IMAGE_NON_COMPLIANT,
        field="content.title",
        fix_action=FixAction.REFORMAT,
        auto_fixable=True,
    )
    got = _route(item)
    assert (got.route, got.task_kind, got.reason) == (Route.ASK, "image_replacement", "image")


def test_rebuild_variation_is_never_deterministic() -> None:
    item = _item(
        error_code=ErrorCode.VARIATION_INVALID,
        field="variants",
        fix_action=FixAction.REBUILD_VARIATION,
        auto_fixable=True,
    )
    got = _route(item)
    assert got.route is Route.ASK
    assert got.fix_action is None
    assert got.reason == "rebuild_variation"


def test_retry_rows_stay_retry_under_an_override() -> None:
    item = _item(error_code=ErrorCode.RATE_LIMITED, field="listing", gate=Gate.COMMERCIAL)
    assert _route(item).route is Route.RETRY


# ---- precedence ----


def test_item_fix_action_beats_the_table() -> None:
    item = _item(
        error_code=ErrorCode.BANNED_TERM,
        field="attributes.fit",
        fix_action=FixAction.REMAP_VALUE,
        auto_fixable=True,
    )
    got = _route(item)
    assert (got.route, got.fix_action, got.reason) == (
        Route.DETERMINISTIC,
        FixAction.REMAP_VALUE,
        "item",
    )


def test_item_fix_action_on_a_critical_key_is_asked() -> None:
    item = _item(
        error_code=ErrorCode.BANNED_TERM,
        field="attributes.fiber_composition",
        fix_action=FixAction.REMAP_VALUE,
        auto_fixable=True,
    )
    got = _route(item)
    assert (got.route, got.reason) == (Route.ASK, "critical_key")
    assert ask_kind(got) == "missing_fact"


def test_table_reason() -> None:
    assert _route(_item()).reason == "table"


# ---- ask_kind ----


def test_ask_kind_fallback_on_a_non_critical_key() -> None:
    item = _item(error_code=ErrorCode.VALUE_NOT_ALLOWED, field="attributes.fit")
    assert ask_kind(_route(item)) == "missing_fact"


def test_ask_kind_on_a_critical_key() -> None:
    item = _item(error_code=ErrorCode.VALUE_NOT_ALLOWED, field="attributes.fiber_composition")
    got = _route(item)
    assert got.route is Route.ASK
    assert ask_kind(got) == "missing_fact"


def test_ask_kind_none_for_reformat_and_ai_field() -> None:
    assert ask_kind(_route(_item())) is None
    assert ask_kind(_route(_item(error_code=ErrorCode.BANNED_TERM))) is None


# ---- fact_key ----


@pytest.mark.parametrize(
    ("field", "key"),
    [
        ("attributes.fiber_composition", "fiber_composition"),
        ("facts.fiber_composition", "fiber_composition"),
        ("variants[SKU-1].facts.size", "size"),
        ("attributes.", None),
        ("", None),
        ("content.title", None),
        ("attributes.colouré", None),
        ("attributes." + "a" * 65, None),
        ("images[0]", None),
        ("attributes", None),
    ],
)
def test_fact_key(field: str, key: str | None) -> None:
    assert fact_key(field) == key


def test_route_takes_no_default_critical_keys() -> None:
    with pytest.raises(TypeError):
        route(_item(), source="rules_engine")  # type: ignore[call-arg]


def test_route_ignores_a_foreign_rule_id() -> None:
    item = _item(rule_id=uuid.uuid4())
    assert _route(item).route is Route.DETERMINISTIC
