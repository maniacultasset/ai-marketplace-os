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


# ---- T22: text on the image and watermarks ----


def _qa_rules(severity: str = "warning") -> list[Rule]:
    return [
        fx.image_rule("text_overlay", severity=severity, min_px=500),
        fx.image_rule("watermark", severity=severity, min_px=500),
    ]


MARKED = {
    "text": (fx.with_text(fx.qa_product()), "TEXT_DETECTED"),
    "tiled_mark": (fx.tiled_mark(fx.qa_product()), "WATERMARK"),
    "tiled_mark_20": (fx.tiled_mark(fx.qa_product(), 0.2), "WATERMARK"),
    "corner_logo": (fx.corner_logo(fx.qa_product()), "WATERMARK"),
}


@pytest.mark.parametrize("name", sorted(MARKED))
def test_marked_fixtures_warn(name: str) -> None:
    data, code = MARKED[name]
    report = run_qa(data, _qa_rules())
    assert code in fx.codes(report), fx.codes(report)
    assert report.pass_ is True


@pytest.mark.parametrize("name", sorted(fx.clean_products()))
def test_clean_fixtures_give_no_warning(name: str) -> None:
    report = run_qa(fx.clean_products()[name], _qa_rules())
    assert fx.codes(report) == [], name
    assert {"text_overlay", "watermark"} <= set(fx.names(report))


def test_switch_off_emits_no_text_or_watermark_check() -> None:
    report = run_qa(MARKED["text"][0], _qa_rules(), photo_qa=False)
    assert {"text_overlay", "watermark"} & set(fx.names(report)) == set()


def test_an_error_text_rule_still_passes() -> None:
    report = run_qa(MARKED["text"][0], _qa_rules("error"))
    assert "TEXT_DETECTED" in fx.codes(report) and report.pass_ is True


def test_below_min_px_the_heuristics_do_not_run() -> None:
    small = fx.with_text(fx.qa_product(400))
    report = run_qa(small, _qa_rules())
    assert {"text_overlay", "watermark"} & set(fx.names(report)) == set()


def test_both_heuristics_take_under_200_ms() -> None:
    import io
    import statistics
    import time

    from app.services import image_qa
    from PIL import Image

    for data in (MARKED["text"][0], MARKED["tiled_mark"][0], fx.clean_products()["checked"]):
        img = Image.open(io.BytesIO(data))
        img.load()
        times = []
        for _ in range(5):
            start = time.perf_counter()
            image_qa.text_overlay(img)
            image_qa.watermark(img)
            times.append(time.perf_counter() - start)
        assert statistics.median(times) < 0.2


@pytest.mark.parametrize("size", [(1, 65535), (1, 20000), (20000, 1)])
def test_hostile_shapes_do_not_crash(size: tuple[int, int]) -> None:
    import time

    data = fx.png(*size)
    start = time.perf_counter()
    report = run_qa(data, [*_qa_rules(), _fill_rule()])
    assert time.perf_counter() - start < 2.0
    assert report.image_id == fx.IMAGE_ID
