"""SI11 T6 (P:A2, D5; CP-D4): the dry run's issue key, `|preview` after everything else."""

from __future__ import annotations

import uuid

from app.services.issues import PREVIEW_SUFFIX, dedupe_key
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem


def _item(**over: object) -> ErrorItem:
    base: dict[str, object] = {
        "field": "attributes.color",
        "error_code": ErrorCode.VALUE_NOT_ALLOWED,
        "severity": Severity.ERROR,
        "message": "m",
        "source": ErrorSource.AMAZON_ADAPTER,
        "retryable": False,
        "raw_code": "99300",
    }
    return ErrorItem.model_validate({**base, **over})


def test_preview_key_keeps_the_prefix() -> None:
    live = dedupe_key("amazon_adapter", _item())
    preview = dedupe_key("amazon_adapter", _item(), channel="preview")
    assert live == "amazon_adapter|attributes.color|VALUE_NOT_ALLOWED|99300"
    assert preview == live + "|preview"
    assert preview.startswith("amazon_adapter|attributes.color|VALUE_NOT_ALLOWED|")
    assert PREVIEW_SUFFIX == "|preview"


def test_preview_key_with_an_item_ends_with_preview() -> None:
    item_id = uuid.uuid4()
    key = dedupe_key("amazon_adapter", _item(), item_id, channel="preview")
    assert key.endswith(f"|item:{item_id}|preview")


def test_live_is_the_default() -> None:
    assert not dedupe_key("amazon_adapter", _item()).endswith("|preview")


def test_long_field_key_stays_within_400() -> None:
    field = "attributes." + "k" * 64
    key = dedupe_key(
        "amazon_adapter", _item(field=field, raw_code="9" * 64), uuid.uuid4(), channel="preview"
    )
    assert len(key) <= 400
