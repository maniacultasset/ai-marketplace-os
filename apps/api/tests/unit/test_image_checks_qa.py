"""SI14 T21-T22 (AC9): the deterministic photo QA checks (fill ratio, text on the image,
watermark): evaluated only while `photo_qa` is on, always warnings, never blocking."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import ImageReport
from mkos_schemas.rules import Rule
from tests import image_fixtures as fx


def run_qa(data: bytes, rules: Sequence[Rule], *, photo_qa: bool = True) -> ImageReport:
    from app.services.image_checks import check_image

    return check_image(
        data,
        image_id=fx.IMAGE_ID,
        marketplace=Marketplace.AMAZON,
        slot=ImageSlot.MAIN,
        rules=rules,
        now=fx.NOW,
        photo_qa=photo_qa,
    )


def _fill_rule(severity: str = "warning") -> Rule:
    return fx.image_rule("fill_ratio", severity=severity, min_fill=0.85)


SMALL_PRODUCT = fx.jpeg(1200, 1200, fill=0.55)  # about 30 % of the area
FULL_FRAME = fx.jpeg(1200, 1200, fill=0.97)


@pytest.mark.parametrize(
    "data",
    [fx.jpeg(1200, 1200), fx.jpeg(800, 1200, fill=0.3), SMALL_PRODUCT, FULL_FRAME],
)
def test_switch_off_reports_are_unchanged(data: bytes) -> None:
    off = run_qa(data, fx.amazon_rules(), photo_qa=False)
    assert "fill_ratio" not in fx.names(off)
    assert off == fx.run(data, fx.amazon_rules())  # exactly today's report


def test_small_product_warns_and_still_passes() -> None:
    report = run_qa(SMALL_PRODUCT, [_fill_rule()])
    assert fx.codes(report) == ["FILL_RATIO_LOW"]
    assert report.issues[0].suggestion == "Zoom or crop so the product fills the frame"
    assert report.pass_ is True


def test_full_frame_passes() -> None:
    report = run_qa(FULL_FRAME, [_fill_rule()])
    assert fx.codes(report) == [] and "fill_ratio" in fx.names(report)


def test_all_white_is_fill_zero_and_warns() -> None:
    report = run_qa(fx.jpeg(600, 600, fill=0.0), [_fill_rule()])
    assert fx.codes(report) == ["FILL_RATIO_LOW"] and report.pass_ is True


def test_one_pixel_image_does_not_crash() -> None:
    report = run_qa(fx.png(1, 1), [_fill_rule()])
    assert "fill_ratio" in fx.names(report)


def test_an_error_rule_is_clamped_to_a_warning() -> None:
    report = run_qa(SMALL_PRODUCT, [_fill_rule(severity="error")])
    assert fx.codes(report) == ["FILL_RATIO_LOW"] and report.pass_ is True


def test_no_fill_rule_emits_no_check() -> None:
    report = run_qa(SMALL_PRODUCT, [fx.image_rule("min_resolution", min_px=500)])
    assert "fill_ratio" not in fx.names(report)


def test_the_amazon_seed_shape_with_qa_on() -> None:
    report = run_qa(SMALL_PRODUCT, fx.amazon_rules())
    assert "FILL_RATIO_LOW" in fx.codes(report)
    assert report.pass_ is True  # the seed's fill rule is a warning; nothing else fails
