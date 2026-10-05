"""ImageReport and related models."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import get_args
from uuid import UUID

import pytest
from mkos_schemas.codes import ImageIssueCode
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import CheckName, ImageIssue, ImageReport
from pydantic import ValidationError

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_IID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _report(**overrides: object) -> ImageReport:
    base = {
        "image_id": _IID,
        "marketplace": Marketplace.AMAZON,
        "slot": ImageSlot.MAIN,
        "pass_": True,
        "processed_at": _TS,
    }
    base.update(overrides)
    return ImageReport.model_validate(base)


def test_image_report_pass_alias() -> None:
    r = _report(pass_=False)
    dumped = r.model_dump()
    assert "pass" in dumped
    assert "pass_" not in dumped
    assert dumped["pass"] is False
    j = r.model_dump_json()
    assert '"pass"' in j
    assert "pass_" not in j
    for mode in ("serialization", "validation"):
        schema = json.dumps(ImageReport.model_json_schema(mode=mode))
        assert '"pass"' in schema or "pass" in schema
        assert "pass_" not in schema


def test_image_report_accepts_pass_and_pass_() -> None:
    assert _report(**{"pass": True}).pass_ is True
    assert _report(pass_=False).pass_ is False


def test_t84_image_issue_uses_code_and_check() -> None:
    issue = ImageIssue(
        code=ImageIssueCode.BACKGROUND_OFF,
        check="background",
        suggestion="Use white background",
    )
    assert issue.code is ImageIssueCode.BACKGROUND_OFF
    assert issue.check == "background"
    assert "type" not in ImageIssue.model_fields


def test_image_check_names_closed() -> None:
    expected = {
        "format",
        "corruption",
        "resolution",
        "aspect_ratio",
        "file_size",
        "exif_orientation",
        "background",
        "fill_ratio",
        "text_overlay",
        "watermark",
        "logo",
        "product_visibility",
    }
    assert set(get_args(CheckName)) == expected


def test_image_report_confidence_bounds_and_nan() -> None:
    _report(confidence=0.0)
    _report(confidence=1.0)
    with pytest.raises(ValidationError):
        _report(confidence=-0.01)
    with pytest.raises(ValidationError):
        _report(confidence=1.01)
    with pytest.raises(ValidationError):
        _report(confidence=math.nan)


def test_image_report_processed_at_requires_timezone() -> None:
    with pytest.raises(ValidationError):
        ImageReport.model_validate(
            {
                "image_id": _IID,
                "marketplace": "amazon",
                "slot": "main",
                "pass_": True,
                "processed_at": datetime(2026, 1, 1, 12, 0, 0),
            }
        )


def test_image_report_slot_vocabulary() -> None:
    _report(slot=ImageSlot.PACKAGING_LABEL)


def test_image_issue_suggestion_max_1000() -> None:
    ImageIssue(
        code=ImageIssueCode.WATERMARK,
        check="watermark",
        suggestion="s" * 1000,
    )
    with pytest.raises(ValidationError):
        ImageIssue(
            code=ImageIssueCode.WATERMARK,
            check="watermark",
            suggestion="t" * 1001,
        )
