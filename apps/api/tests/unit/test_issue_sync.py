"""S19 T18 (AC 13; DATABASE.md:1331): the issue dedupe key and the classification fallback."""

from __future__ import annotations

import uuid

from app.services.issues import classify, dedupe_key, resolution_for
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, FixAction, RetryClass, Severity
from mkos_schemas.errors import ErrorItem


def _item(**over: object) -> ErrorItem:
    base: dict[str, object] = {
        "field": "content.title",
        "error_code": ErrorCode.VALUE_NOT_ALLOWED,
        "severity": Severity.WARNING,
        "message": "m",
        "source": ErrorSource.RULES_ENGINE,
        "retryable": False,
    }
    return ErrorItem.model_validate({**base, **over})


def test_dedupe_key_vector() -> None:
    rule = uuid.UUID("00000000-0000-4000-8000-000000000001")
    assert dedupe_key("rules_engine", _item(rule_id=rule)) == (
        f"rules_engine|content.title|VALUE_NOT_ALLOWED|{rule}"
    )
    assert dedupe_key("amazon_adapter", _item(raw_code="90220")) == (
        "amazon_adapter|content.title|VALUE_NOT_ALLOWED|90220"
    )
    assert dedupe_key("schema", _item()) == "schema|content.title|VALUE_NOT_ALLOWED|"


def test_dedupe_key_item_suffix() -> None:
    item_id = uuid.uuid4()
    assert dedupe_key("amazon_adapter", _item(), item_id).endswith(f"|item:{item_id}")


def test_dedupe_key_template_term() -> None:
    template = _item(field="attributes.color", error_code=ErrorCode.MISSING_ATTRIBUTE)
    assert dedupe_key("rules_engine", template) == (
        "rules_engine|attributes.color|MISSING_ATTRIBUTE|template:color"
    )
    rule = _item(
        field="attributes.color", error_code=ErrorCode.MISSING_ATTRIBUTE, rule_id=uuid.uuid4()
    )
    assert dedupe_key("rules_engine", rule) != dedupe_key("rules_engine", template)
    # only the rules engine's rule-less attribute items are template checks
    assert dedupe_key("schema", template).endswith("|")


def test_meta_fallback() -> None:
    assert classify(_item()) == ("invalid_value", "user_action", None)  # ERROR_CODE_META
    explicit = _item(
        error_class=ErrorClass.CONTENT_ERROR,
        retry_class=RetryClass.USER_ACTION,
        fix_action=FixAction.REFORMAT,
        auto_fixable=True,
    )
    assert classify(explicit) == ("content_error", "user_action", "reformat")


def test_resolution_for_cause() -> None:
    assert resolution_for("edit") == "human_edit"
    assert resolution_for("regenerate") == "regenerated"
    assert resolution_for("revalidate") == "revalidated_clean"
    assert resolution_for("publish") == "revalidated_clean"
