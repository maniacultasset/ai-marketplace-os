"""S14a T4: items and report shaping (AC 1, 6, 9; D10g, D13, D14)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from app.services.rules_engine import report as report_mod
from app.services.rules_engine.report import (
    TEXT_MAX,
    Draft,
    build_report,
    make_item,
    path_rank,
    render_text,
)
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorClass, Marketplace, RetryClass, Severity
from mkos_schemas.paths import FIELD_PATH_PATTERNS
from tests.rules_engine_builders import LISTING_ID, TENANT_ID, rule

AMAZON = Marketplace.AMAZON


def _rule(**over: Any) -> Any:
    rule_def = over.pop("rule_def", {"type": "max_length", "limit": 5})
    return rule(rule_def, **over)


def _draft(
    field: str = "content.title",
    code: ErrorCode = ErrorCode.FIELD_TOO_LONG,
    *,
    rule_: Any = "auto",
    severity: Severity = Severity.ERROR,
    value: object = "v",
    placeholders: dict[str, str] | None = None,
) -> Draft:
    if rule_ == "auto":
        rule_ = _rule()
    return Draft(field, code, value, placeholders or {}, rule_, severity)


def _report(drafts: list[Draft]) -> Any:
    return build_report(drafts, listing_id=LISTING_ID, marketplace=AMAZON)


def test_report_shape_empty_passes() -> None:
    rep = _report([])
    assert (rep.status, rep.error_code, rep.retryable, rep.errors) == ("passed", None, False, [])


def test_report_shape_warnings_only_passed() -> None:
    rep = _report([_draft(severity=Severity.WARNING)])
    assert rep.status == "passed"
    assert rep.error_code is None
    assert rep.retryable is False
    assert len(rep.errors) == 1


def test_report_shape_failed_retryable_all_ai_correct() -> None:
    rep = _report([_draft(), _draft("content.description")])
    assert rep.status == "failed"
    assert rep.error_code is ErrorCode.VALIDATION_FAILED
    assert rep.retryable is True


def test_report_shape_one_user_action_not_retryable() -> None:
    missing = _draft(
        "attributes.color",
        ErrorCode.MISSING_ATTRIBUTE,
        rule_=_rule(rule_def={"type": "required_attribute", "attribute": "color"}),
    )
    rep = _report([_draft(), missing])
    assert rep.status == "failed"
    assert rep.retryable is False


def test_report_shape_dedupe_rule_beats_template() -> None:
    req = _rule(rule_def={"type": "required_attribute", "attribute": "color"})
    rule_item = _draft("attributes.color", ErrorCode.MISSING_ATTRIBUTE, rule_=req)
    template_item = _draft("attributes.color", ErrorCode.MISSING_ATTRIBUTE, rule_=None)
    rep = _report([template_item, rule_item, rule_item])
    assert [e.rule_id for e in rep.errors] == [req.id]


@pytest.mark.parametrize("case", ["tenant_error", "platform_warning"])
def test_template_item_not_suppressed_by_tenant_or_warning_rule(case: str) -> None:
    banned = {"type": "banned_terms", "terms": ["x"], "error_code": "VALUE_NOT_ALLOWED"}
    if case == "tenant_error":
        r = _rule(rule_def=banned, tenant_id=TENANT_ID, field="attributes.fabric_type")
        severity = Severity.ERROR
    else:
        r = _rule(rule_def=banned, field="attributes.fabric_type", severity="warning")
        severity = Severity.WARNING
    rule_item = _draft(
        "attributes.fabric_type", ErrorCode.VALUE_NOT_ALLOWED, rule_=r, severity=severity
    )
    template_item = _draft("attributes.fabric_type", ErrorCode.VALUE_NOT_ALLOWED, rule_=None)
    rep = _report([rule_item, template_item])
    assert sorted(str(e.rule_id) for e in rep.errors) == sorted([str(r.id), "None"])
    assert rep.status == "failed"


def test_report_shape_sort_order() -> None:
    r = _rule()
    drafts = [
        _draft("content.bullets[10]", rule_=r),
        _draft("content.bullets[2]", rule_=r),
        _draft("content.title", rule_=r),
    ]
    rep = _report(drafts)
    assert [e.field for e in rep.errors] == [
        "content.title",
        "content.bullets[2]",
        "content.bullets[10]",
    ]


SAMPLE_PATHS = [
    "listing",
    "request",
    "file.header",
    "file.row",
    "content.title",
    "content.bullets",
    "content.bullets[3]",
    "content.description",
    "keywords.search_terms",
    "keywords.search_terms[1]",
    "keywords.hidden_terms",
    "keywords.hidden_terms[0]",
    "attributes",
    "attributes.color",
    "images[0]",
    "facts.mrp",
    "variants",
    "variants[a]",
    "variants[a].facts.mrp",
    "brand.name",
]


def test_report_shape_sort_rank_every_legal_path() -> None:
    import re

    for pattern in FIELD_PATH_PATTERNS:
        assert any(re.fullmatch(pattern, p) for p in SAMPLE_PATHS), pattern
    ranks = [path_rank(p) for p in SAMPLE_PATHS]
    assert len(set(ranks)) == len(ranks)  # total: no two paths tie
    assert [r[0] for r in ranks if r[0] != 99] == [r[0] for r in ranks]
    ordered = sorted(SAMPLE_PATHS, key=path_rank)
    assert ordered[:3] == ["content.title", "content.bullets", "content.bullets[3]"]
    assert ordered.index("attributes") < ordered.index("attributes.color")
    assert ordered[-1] == "file.row"
    assert path_rank("zzz") == (99, -1, "", "zzz")
    r = _rule()
    rep = _report([_draft(p, rule_=r) for p in reversed(SAMPLE_PATHS)])
    assert [e.field for e in rep.errors] == ordered


def _many(n: int, **kw: Any) -> list[Draft]:
    return [_draft(f"keywords.search_terms[{i % 50}]", rule_=_rule(**kw)) for i in range(n)]


def test_report_shape_cap_500_deterministic() -> None:
    drafts = _many(600)
    a = _report(drafts)
    b = _report(list(reversed(drafts)))
    assert len(a.errors) == 500
    assert a.model_dump_json() == b.model_dump_json()


def test_report_shape_cap_keeps_blocking() -> None:
    warnings = _many(600, tenant_id=TENANT_ID, severity="warning", rule_def=_banned())
    error = _draft(
        "attributes.color",
        ErrorCode.MISSING_ATTRIBUTE,
        rule_=_rule(rule_def={"type": "required_attribute", "attribute": "color"}),
    )
    rep = _report([*warnings, error])
    assert rep.status == "failed"
    assert rep.errors[0].field == "attributes.color"
    assert len(rep.errors) == 500


def _banned() -> dict[str, Any]:
    return {"type": "banned_terms", "terms": ["x"]}


def test_report_shape_cap_platform_before_tenant() -> None:
    tenant = _many(600, tenant_id=TENANT_ID, rule_def=_banned())
    platform = _draft(
        "attributes.color",
        ErrorCode.MISSING_ATTRIBUTE,
        rule_=_rule(rule_def={"type": "required_attribute", "attribute": "color"}),
    )
    rep = _report([*tenant, platform])
    assert rep.errors[0].field == "attributes.color"
    assert sum(e.field == "attributes.color" for e in rep.errors) == 1


def test_report_shape_cap_status_from_full_set() -> None:
    errors = _many(509)
    user_action = _draft(
        "images[5]",
        ErrorCode.MISSING_ATTRIBUTE,
        rule_=_rule(rule_def={"type": "required_attribute", "attribute": "color"}),
    )
    rep = _report([*errors, user_action])
    assert len(rep.errors) == 500
    assert all(e.retryable for e in rep.errors)
    assert rep.retryable is False


def test_report_shape_listing_id_copied() -> None:
    assert _report([]).listing_id == LISTING_ID
    assert build_report([], listing_id=None, marketplace=AMAZON).listing_id is None


def test_classification_override_wins() -> None:
    r = _rule(
        rule_def={
            "type": "max_length",
            "limit": 5,
            "error_class": "compliance_error",
            "retry_class": "user_action",
        }
    )
    item = make_item(_draft(rule_=r), marketplace=AMAZON)
    assert item.error_class is ErrorClass.COMPLIANCE_ERROR
    assert item.retry_class is RetryClass.USER_ACTION
    assert item.retryable is False


ENGINE_CODES = [
    ErrorCode.FIELD_TOO_LONG,
    ErrorCode.FIELD_TOO_SHORT,
    ErrorCode.BANNED_TERM,
    ErrorCode.MISSING_ATTRIBUTE,
    ErrorCode.VALUE_NOT_ALLOWED,
    ErrorCode.CROSS_FIELD_MISMATCH,
    ErrorCode.UNSUPPORTED_CLAIM,
    ErrorCode.PATTERN_MISMATCH,
]


@pytest.mark.parametrize("code", ENGINE_CODES)
def test_classification_meta_default(code: ErrorCode) -> None:
    item = make_item(_draft(code=code), marketplace=AMAZON)
    meta = ERROR_CODE_META[code]
    assert item.error_class == meta.error_class
    assert item.retry_class is meta.retry_class
    assert item.retryable is (meta.retry_class in {RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT})
    assert item.source.value == "rules_engine"
    assert item.marketplace is AMAZON


def test_message_placeholder_inapplicable_empty() -> None:
    r = _rule(rule_def={"type": "max_length", "limit": 5, "message": "x{term}y {limit}"})
    item = make_item(_draft(rule_=r, placeholders={"limit": "5"}), marketplace=AMAZON)
    assert item.message == "xy 5"


def test_message_long_value_capped_1000() -> None:
    msg = "{value}" * 7 + "z" * 900
    r = _rule(rule_def={"type": "max_length", "limit": 5, "message": msg})
    item = make_item(_draft(rule_=r, placeholders={"value": "v" * 5000}), marketplace=AMAZON)
    assert len(item.message) == TEXT_MAX
    assert item.message.startswith("v" * 200 + "v")


def test_message_blank_falls_back() -> None:
    r = _rule(rule_def={"type": "max_length", "limit": 5, "message": "{term}"})
    item = make_item(_draft(rule_=r), marketplace=AMAZON)
    assert item.message == "{term}"


def test_value_submitted_secret_redacted() -> None:
    item = make_item(_draft(value='{"api_key": "abc"}'), marketplace=AMAZON)
    assert item.value_submitted == "<redacted>"
    item = make_item(_draft(value=["a", "b"]), marketplace=AMAZON)
    assert item.value_submitted == "a b"
    item = make_item(_draft(value=None), marketplace=AMAZON)
    assert item.value_submitted is None


def _suggest(text: str, values: dict[str, str] | None = None) -> str | None:
    r = _rule(rule_def={"type": "max_length", "limit": 5, "suggestion": text})
    return make_item(
        _draft(rule_=r, placeholders=values or {"value": "V"}), marketplace=AMAZON
    ).suggestion


def test_suggestion_format_spec_not_expanded() -> None:
    out = _suggest("{value:>999999999}")
    assert out == "{value:>999999999}"
    assert out is not None and len(out) <= TEXT_MAX


def test_suggestion_unknown_placeholder_literal() -> None:
    assert _suggest("Use {brand} {value}") == "Use {brand} V"


@pytest.mark.parametrize("text", ["{", "}", "{value"])
def test_suggestion_unbalanced_brace(text: str) -> None:
    assert _suggest(text) == text


def test_suggestion_attribute_access_literal() -> None:
    assert _suggest("{value.__class__.__mro__}") == "{value.__class__.__mro__}"
    assert _suggest("{value[0]}") == "{value[0]}"
    assert _suggest("{value!r}") == "{value!r}"
    assert _suggest("{value!r:>5}") == "{value!r:>5}"
    assert _suggest("{{value}}") == "{value}"


def test_render_text_used_for_message_and_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def spy(template: str, values: Any) -> str:
        calls.append(template)
        return "rendered"

    monkeypatch.setattr(report_mod, "render_text", spy)
    r = _rule(rule_def={"type": "max_length", "limit": 5, "message": "m", "suggestion": "s"})
    item = make_item(_draft(rule_=r), marketplace=AMAZON)
    assert calls == ["m", "s"]
    assert (item.message, item.suggestion) == ("rendered", "rendered")


def test_render_text_template_messages() -> None:
    item = make_item(
        _draft(
            "attributes.color",
            ErrorCode.MISSING_ATTRIBUTE,
            rule_=None,
            placeholders={"attribute": "color"},
        ),
        marketplace=AMAZON,
    )
    assert item.message == "Required attribute color is missing"
    assert item.rule_id is None
    assert item.suggestion is None
    assert render_text("{value}", {"value": "  "}) == "{value}"


def test_rule_id_sorted_none_last() -> None:
    a = _rule(id=UUID(int=2))
    b = _rule(id=UUID(int=1))
    drafts = [
        _draft("attributes.color", ErrorCode.VALUE_NOT_ALLOWED, rule_=None),
        _draft("attributes.color", ErrorCode.VALUE_NOT_ALLOWED, rule_=a, severity=Severity.ERROR),
        _draft("attributes.color", ErrorCode.VALUE_NOT_ALLOWED, rule_=b),
    ]
    rep = _report(drafts)
    assert [e.rule_id for e in rep.errors] == [b.id, a.id]
