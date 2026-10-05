"""Background: corner squares plus per-edge strips with the longest off run forgiven."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from mkos_schemas.images import ImageReport
from PIL import Image
from tests import image_fixtures as fx
from tests.image_fixtures import codes, image_rule, run

WHITE_RULE = [image_rule("background", rgb=(255, 255, 255))]


def bg(data: bytes, rules: list[Any] | None = None) -> ImageReport:
    return run(data, WHITE_RULE if rules is None else rules)


def passes(data: bytes, rules: list[Any] | None = None) -> bool:
    report = bg(data, rules)
    assert [c.check for c in report.checks][-1] == "background"
    return report.pass_


def _png(img: Image.Image) -> bytes:
    return fx.encode(img, "PNG")


def _touching(w: int = 1000) -> Image.Image:
    """White canvas, dark product 600 px wide from the top edge to the bottom edge."""
    img = Image.new("RGB", (w, w), fx.WHITE)
    img.paste(fx.DARK, (200, 0, 800, w))
    return img


# 1, 2. basic, near-white, artifacts, transparency, tolerance


def test_pillow_checks_background_pass() -> None:
    assert passes(fx.png(400, 400)) is True


def test_pillow_checks_background_fail() -> None:
    report = bg(fx.png(400, 400, border=(40, 40, 40)))
    assert codes(report) == ["BACKGROUND_OFF"] and report.pass_ is False
    assert report.issues[0].suggestion == "Background removal / replacement"


def test_background_near_white_fails() -> None:
    assert passes(fx.png(400, 400, border=(248, 250, 249))) is False


def test_background_jpeg_white_passes() -> None:
    assert passes(fx.jpeg(800, 800, quality=85)) is True


def test_background_transparent_png_fails() -> None:
    assert passes(fx.png(400, 400, mode="RGBA", alpha=0)) is False
    assert passes(fx.png(400, 400, mode="RGBA", alpha=255)) is True


def test_background_tolerance_edge() -> None:
    assert passes(fx.png(400, 400, border=(250, 250, 250))) is True
    assert passes(fx.png(400, 400, border=(249, 255, 255))) is False


# 3. modes


MODE_BUILDERS: dict[str, Callable[[tuple[int, int, int]], bytes]] = {
    "L": lambda border: fx.png(300, 300, mode="L", border=border),
    "LA": lambda border: fx.png(300, 300, mode="LA", border=border),
    "P": lambda border: fx.png(300, 300, mode="P", border=border),
    "P+tRNS": lambda border: fx.png(300, 300, mode="P", border=border, transparent_product=True),
    "CMYK": lambda border: fx.tiff(300, 300, mode="CMYK", border=border),
    "1": lambda border: fx.png(300, 300, mode="1", border=border, product=(0, 0, 0)),
    "I;16": lambda border: fx.png(300, 300, mode="I;16", border=border),
}


@pytest.mark.parametrize("mode", list(MODE_BUILDERS))
def test_background_modes(mode: str) -> None:
    build = MODE_BUILDERS[mode]
    assert passes(build(fx.WHITE)) is True
    black = (0, 0, 0)
    report = bg(build(black))
    assert codes(report) == ["BACKGROUND_OFF"]


def test_background_mid_grey_16_bit_fails() -> None:
    img = Image.new("I;16", (300, 300), 32768)
    assert passes(_png(img)) is False


# 4. thresholds and tiny images


@pytest.mark.parametrize(("rows", "expected"), [(4, True), (6, False)])
def test_background_threshold_second_run(rows: int, expected: bool) -> None:
    """Top strip: 960 positions, product run 600 removed, 360 x 20 = 7200 px remain; an 18 px
    wide patch `rows` deep is 1 % (4 rows) or 1.5 % (6 rows) of them."""
    img = _touching()
    img.paste((128, 128, 128), (100, 0, 118, rows))
    assert passes(_png(img)) is expected


@pytest.mark.parametrize(("off", "expected"), [(4, True), (6, False)])
def test_background_threshold_corner(off: int, expected: bool) -> None:
    """A corner square is 20 x 20 = 400 px: 4 px off is 1 %, 6 px is 1.5 %."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    img.paste(fx.DARK, (0, 0, off, 1))
    assert passes(_png(img)) is expected


def test_background_tiny_image() -> None:
    assert passes(_png(Image.new("RGB", (3, 3), fx.WHITE))) is True
    dotted = Image.new("RGB", (3, 3), fx.WHITE)
    dotted.putpixel((1, 1), fx.DARK)
    assert passes(_png(dotted)) is False


# 5. target colour from the rule


def test_background_rgb_from_rule() -> None:
    grey_rule = [image_rule("background", rgb=(240, 240, 240))]
    assert passes(fx.png(400, 400, border=(240, 240, 240)), grey_rule) is True
    assert passes(fx.png(400, 400), grey_rule) is False


def test_background_warning_rule() -> None:
    rule = [image_rule("background", severity="warning", rgb=(255, 255, 255))]
    report = bg(fx.png(400, 400, border=(0, 0, 0)), rule)
    assert report.pass_ is True and codes(report) == ["BACKGROUND_OFF"]


# 6. AC7 and the documented limits


def test_background_product_touching_edge_passes() -> None:
    assert passes(_png(_touching())) is True


def test_background_product_plus_grey_patch_fails() -> None:
    img = _touching()
    img.paste((128, 128, 128), (60, 0, 90, 20))
    assert passes(_png(img)) is False


def test_background_product_covering_corners_fails() -> None:
    """Documented limit: a product filling all four corner squares fails even with clean edges."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    for x, y in ((0, 0), (980, 0), (0, 980), (980, 980)):
        img.paste(fx.DARK, (x, y, x + 20, y + 20))
    assert passes(_png(img)) is False


def test_background_single_edge_run_forgiven() -> None:
    """Documented limit: one off run per edge is forgiven when the corners are clean; the
    vision step confirms the background."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    img.paste((128, 128, 128), (300, 0, 400, 20))
    assert passes(_png(img)) is True


# 7. only the band is converted


@pytest.mark.parametrize("mode", list(MODE_BUILDERS))
def test_background_converts_band_only(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    builders: dict[str, Callable[[], bytes]] = {
        "L": lambda: fx.png(1000, 1000, mode="L"),
        "LA": lambda: fx.png(1000, 1000, mode="LA"),
        "P": lambda: fx.png(1000, 1000, mode="P"),
        "P+tRNS": lambda: fx.png(1000, 1000, mode="P", transparent_product=True),
        "CMYK": lambda: fx.tiff(1000, 1000, mode="CMYK"),
        "1": lambda: fx.png(1000, 1000, mode="1"),
        "I;16": lambda: fx.png(1000, 1000, mode="I;16"),
    }
    data = builders[mode]()
    sizes: list[tuple[int, int]] = []
    real = Image.Image.convert

    def spy(self: Image.Image, *args: Any, **kwargs: Any) -> Image.Image:
        sizes.append(self.size)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", spy)
    assert passes(data) is True
    assert sizes and (1000, 1000) not in sizes
