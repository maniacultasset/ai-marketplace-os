"""Rule selection, file size and format narrowing."""

from __future__ import annotations

from typing import Any

import pytest
from mkos_schemas.rules import Rule
from PIL import Image
from tests import image_fixtures as fx
from tests.image_fixtures import codes, image_rule, names, run

INTRINSIC = ["format", "corruption", "exif_orientation"]
SMALL = fx.jpeg(64, 64)


@pytest.fixture
def open_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    real = Image.open

    def spy(fp: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(fp)
        return real(fp, *args, **kwargs)

    monkeypatch.setattr(Image, "open", spy)
    return calls


def _failing_resolution(**kw: Any) -> Rule:
    return image_rule("min_resolution", min_px=4096, **kw)


# 1. file size


def test_pillow_checks_file_size_pass() -> None:
    report = run(SMALL, [image_rule("max_bytes", max_bytes=len(SMALL))])
    assert names(report) == ["file_size", *INTRINSIC]
    assert report.pass_ is True


def test_pillow_checks_file_size_fail(open_calls: list[object]) -> None:
    report = run(SMALL, [image_rule("max_bytes", max_bytes=len(SMALL) - 1)])
    assert names(report) == ["file_size"]
    assert codes(report) == ["FILE_TOO_LARGE"]
    assert report.pass_ is False
    assert open_calls == []


def test_file_size_warning_continues() -> None:
    report = run(SMALL, [image_rule("max_bytes", severity="warning", max_bytes=1)])
    assert names(report) == ["file_size", *INTRINSIC]
    assert report.pass_ is True and codes(report) == ["FILE_TOO_LARGE"]


# 2, 3. selection


def _baseline() -> str:
    return run(SMALL).model_dump_json()


@pytest.mark.parametrize(
    "rule",
    [
        _failing_resolution(marketplace="flipkart"),
        _failing_resolution(slot="secondary"),
        _failing_resolution(enabled=False),
    ],
    ids=["other_marketplace", "other_slot", "disabled"],
)
def test_rule_selection_skips(rule: Rule) -> None:
    assert run(SMALL, [rule]).model_dump_json() == _baseline()


def test_rule_selection_non_image_rules_ignored() -> None:
    text_rule = Rule.model_validate(
        {
            "id": "00000000-0000-4000-8000-0000000000aa",
            "marketplace": "amazon",
            "field": "content.title",
            "rule_def": {"type": "max_length", "message": "too long", "limit": 1},
        }
    )
    assert run(SMALL, [text_rule]).model_dump_json() == _baseline()


def test_rule_selection_any_slot() -> None:
    report = run(SMALL, [_failing_resolution(slot="any")])
    assert codes(report) == ["RESOLUTION_LOW"]


def test_rule_selection_packaging_label_any_only() -> None:
    rules = [
        _failing_resolution(slot="main"),
        image_rule("aspect_ratio", slot="any", ratio_w=2, ratio_h=1),
    ]
    report = run(SMALL, rules, slot="packaging_label")
    assert names(report) == [*INTRINSIC, "aspect_ratio"]
    assert codes(report) == ["WRONG_ASPECT"]


# 4. vision-only checks


@pytest.mark.parametrize(
    ("check", "params"),
    [
        ("fill_ratio", {"min_fill": 0.99}),
        ("text_overlay", {"min_fill": 0.5}),
        ("watermark", {"min_fill": 0.5}),
        ("logo", {"min_fill": 0.5}),
    ],
)
def test_vision_only_rules_not_listed(check: str, params: dict[str, Any]) -> None:
    assert names(run(SMALL, [image_rule(check, **params)])) == INTRINSIC


# 5. format narrowing


def test_format_rule_narrows() -> None:
    report = run(fx.tiff(64, 64), [image_rule("format", allowed_formats=["jpeg", "png"])])
    assert names(report) == ["format"]
    assert codes(report) == ["FORMAT_UNSUPPORTED"] and report.pass_ is False


def test_format_rule_aliases() -> None:
    assert run(SMALL, [image_rule("format", allowed_formats=["JPG"])]).pass_ is True
    assert run(fx.tiff(64, 64), [image_rule("format", allowed_formats=["Tif"])]).pass_ is True


def test_format_rule_cannot_widen() -> None:
    report = run(fx.magic("gif"), [image_rule("format", allowed_formats=["gif"])])
    assert names(report) == ["format"] and codes(report) == ["FORMAT_UNSUPPORTED"]


def test_format_rule_every_list_applies() -> None:
    rules = [
        image_rule("format", slot="any", allowed_formats=["jpeg", "png"]),
        image_rule("format", allowed_formats=["png"]),
    ]
    assert codes(run(SMALL, rules)) == ["FORMAT_UNSUPPORTED"]


def test_format_rule_warning_continues() -> None:
    report = run(
        fx.tiff(64, 64), [image_rule("format", severity="warning", allowed_formats=["png"])]
    )
    assert names(report) == INTRINSIC
    assert report.pass_ is True and codes(report) == ["FORMAT_UNSUPPORTED"]
    assert report.checks[0].passed is False


# 6. misconfigured rules raise before any byte is read


@pytest.mark.parametrize(
    ("check", "params"),
    [
        ("min_resolution", {"ratio_w": 1}),
        ("recommended_resolution", {"ratio_w": 1}),
        ("aspect_ratio", {"ratio_w": 1}),
        ("aspect_ratio", {"min_px": 1}),
        ("background", {"min_px": 1}),
        ("max_bytes", {"min_px": 1}),
        ("format", {"min_px": 1}),
    ],
)
def test_misconfigured_rule_raises(check: str, params: dict[str, Any]) -> None:
    rule = image_rule(check, **params)
    with pytest.raises(ValueError, match=f"image rule {rule.id}: {check} needs"):
        run(b"", [rule])


def test_misconfigured_unselected_rule_ignored() -> None:
    assert run(SMALL, [image_rule("background", marketplace="flipkart", min_px=1)]).pass_ is True


# 7. suggestions


def test_rule_suggestion_wins() -> None:
    report = run(SMALL, [_failing_resolution(suggestion="Shoot at 4096 px or more")])
    assert report.issues[0].suggestion == "Shoot at 4096 px or more"
    default = run(SMALL, [_failing_resolution()])
    assert default.issues[0].suggestion == "Upscale or re-export"


def test_most_severe_rule_suggestion() -> None:
    rules = [
        _failing_resolution(severity="warning", suggestion="warning text"),
        _failing_resolution(severity="error", suggestion="error text"),
        _failing_resolution(severity="error", suggestion="second error text"),
    ]
    report = run(SMALL, rules)
    assert [i.suggestion for i in report.issues] == ["error text"]
    assert report.pass_ is False
    assert [c.check for c in report.checks].count("resolution") == 1
