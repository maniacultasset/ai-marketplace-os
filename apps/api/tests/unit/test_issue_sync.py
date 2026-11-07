"""S19 T18 (AC 13; DATABASE.md:1331): the issue dedupe key and the classification fallback."""

from __future__ import annotations

import uuid

import pytest
from app.services.issues import classify, dedupe_key, resolution_for
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, FixAction, Gate, RetryClass, Severity
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
    assert classify(_item(), critical_keys=frozenset()) == (
        "invalid_value",
        "user_action",
        None,
    )  # ERROR_CODE_META
    explicit = _item(
        error_class=ErrorClass.CONTENT_ERROR,
        retry_class=RetryClass.USER_ACTION,
        fix_action=FixAction.REFORMAT,
        auto_fixable=True,
    )
    assert classify(explicit, critical_keys=frozenset()) == (
        "content_error",
        "user_action",
        "reformat",
    )


def test_resolution_for_cause() -> None:
    assert resolution_for("edit") == "human_edit"
    assert resolution_for("regenerate") == "regenerated"
    assert resolution_for("revalidate") == "revalidated_clean"
    assert resolution_for("publish") == "revalidated_clean"


# ---- SI4 A6 (AC1): the safety overrides keep a stored row truthful ----

CRITICAL = frozenset({"fiber_composition"})


def _remap(field: str = "attributes.fiber_composition", **over: object) -> ErrorItem:
    return _item(
        field=field,
        severity=Severity.ERROR,
        fix_action=FixAction.REMAP_VALUE,
        auto_fixable=True,
        source=ErrorSource.AMAZON_ADAPTER,
        **over,
    )


def test_classify_critical_key_drops_the_fix_action() -> None:
    assert classify(_remap(), critical_keys=CRITICAL)[2] is None


def test_classify_non_critical_key_keeps_the_fix_action() -> None:
    assert classify(_remap(), critical_keys=frozenset())[2] == "remap_value"
    assert classify(_remap("attributes.fit"), critical_keys=CRITICAL)[2] == "remap_value"


@pytest.mark.parametrize("gate", [Gate.COMPLIANCE, Gate.COMMERCIAL])
def test_classify_gate_drops_the_fix_action(gate: Gate) -> None:
    assert classify(_remap("attributes.fit", gate=gate), critical_keys=frozenset())[2] is None


def test_classify_unknown_class_and_image_drop_the_fix_action() -> None:
    unknown = _remap("attributes.fit", error_class=ErrorClass.UNKNOWN)
    assert classify(unknown, critical_keys=frozenset())[2] is None
    image = _remap("images[0]")
    assert classify(image, critical_keys=frozenset(), source="vision_model")[2] is None


def test_classify_rebuild_variation_is_never_auto_fixable() -> None:
    item = _item(field="variants", error_code=ErrorCode.VARIATION_INVALID)
    assert classify(item, critical_keys=frozenset())[2] is None


def test_classify_needs_the_critical_keys() -> None:
    with pytest.raises(TypeError):
        classify(_remap())  # type: ignore[call-arg]


def test_resolution_for_status_sync() -> None:
    assert resolution_for("status_sync") == "marketplace_cleared"
