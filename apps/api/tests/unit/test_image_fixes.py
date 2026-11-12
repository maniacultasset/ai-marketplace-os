"""SI7 T6: the `fixes` an image offers (AC15)."""

from __future__ import annotations

from typing import Any

from app.services.image_fixes import fix_unavailable_for, fixes_for

NEAR_WHITE = {"uniform": True, "near_white": True, "colour": [245, 245, 245]}
GREY = {"uniform": True, "near_white": False, "colour": [200, 200, 200]}


def _report(*failed: str, vision_failed: str | None = None) -> dict[str, Any]:
    names = ("file_size", "format", "corruption", "resolution", "aspect_ratio", "background")
    checks = [{"check": n, "method": "deterministic", "passed": n not in failed} for n in names]
    if vision_failed:
        checks.append({"check": vision_failed, "method": "vision", "passed": False})
    return {"checks": checks}


def _reports(*failed: str, **kw: Any) -> dict[str, Any]:
    return {"amazon": _report(*failed, **kw), "flipkart": _report()}


def test_aspect_only_is_a_fix() -> None:
    assert fixes_for("main", _reports("aspect_ratio"), GREY, edit_enabled=True) == ["fix"]
    assert fixes_for("main", _reports("resolution"), None, edit_enabled=False) == ["fix"]


def test_grey_background_needs_the_ai() -> None:
    reports = _reports("background", "aspect_ratio")
    assert fixes_for("main", reports, GREY, edit_enabled=True) == ["white_background"]
    assert fixes_for("main", reports, GREY, edit_enabled=False) == []


def test_near_white_background_is_a_fix() -> None:
    assert fixes_for("main", _reports("background"), NEAR_WHITE, edit_enabled=True) == ["fix"]


def test_passing_main_and_other_slots_offer_nothing() -> None:
    assert fixes_for("main", _reports(), GREY, edit_enabled=True) == []
    assert fixes_for("main", _reports(vision_failed="text_overlay"), GREY, edit_enabled=True) == []
    assert fixes_for("secondary", _reports("background"), GREY, edit_enabled=True) == []
    assert fixes_for("size_chart", _reports("aspect_ratio"), GREY, edit_enabled=True) == []


def test_corruption_offers_nothing() -> None:
    assert fixes_for("main", _reports("corruption"), GREY, edit_enabled=True) == []
    assert fixes_for("main", _reports("format", "background"), GREY, edit_enabled=True) == []


def test_pre_si7_row_without_a_border_profile() -> None:
    reports = _reports("background")
    assert fixes_for("main", reports, None, edit_enabled=True) == ["white_background"]
    assert fixes_for("main", None, None, edit_enabled=True) == []


def test_flipkart_failures_count_too() -> None:
    reports = {"amazon": _report(), "flipkart": _report("aspect_ratio")}
    assert fixes_for("main", reports, GREY, edit_enabled=True) == ["fix"]


def _with_qa(reports: dict[str, Any], *qa_failed: str) -> dict[str, Any]:
    """The SI14 photo QA checks (warnings only) as the prod report stores them: deterministic."""
    for report in reports.values():
        report["checks"].extend(
            {"check": n, "method": "deterministic", "passed": n not in qa_failed}
            for n in ("fill_ratio", "text_overlay", "watermark")
        )
    return reports


def test_photo_qa_warnings_do_not_hide_the_white_background() -> None:
    # B2 (chrome pass 1): a grey-background Front with "Text on image" and "Low fill" warnings
    reports = _with_qa(_reports("background"), "fill_ratio", "text_overlay")
    assert fixes_for("main", reports, GREY, edit_enabled=True) == ["white_background"]
    assert fixes_for("main", reports, NEAR_WHITE, edit_enabled=True) == ["fix"]
    qa_only = _with_qa(_reports(), "fill_ratio", "watermark")
    assert fixes_for("main", qa_only, GREY, edit_enabled=True) == []
    framing = _with_qa(_reports("aspect_ratio"), "fill_ratio")
    assert fixes_for("main", framing, GREY, edit_enabled=True) == ["fix"]


def test_why_no_fix_is_named() -> None:
    grey = _reports("background")
    assert fix_unavailable_for("main", grey, GREY, edit_enabled=False) == "ai_off"
    assert fix_unavailable_for("main", grey, GREY, edit_enabled=True) is None
    broken = _reports("format", "background")
    assert fix_unavailable_for("main", broken, GREY, edit_enabled=True) == "not_fixable"
    assert fix_unavailable_for("main", _reports(), GREY, edit_enabled=False) is None
    assert fix_unavailable_for("secondary", grey, GREY, edit_enabled=False) is None
    assert fix_unavailable_for("main", None, None, edit_enabled=False) is None
    qa = _with_qa(_reports("background"), "text_overlay")
    assert fix_unavailable_for("main", qa, GREY, edit_enabled=True) is None
