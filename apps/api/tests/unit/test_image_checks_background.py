"""Background: corner squares plus per-edge strips with the longest inward-continuing off run
forgiven."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from mkos_schemas.images import ImageReport
from PIL import Image, ImageDraw, ImageFilter
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
    """White canvas, dark product touching the top and bottom edges 600 px wide, with a narrower
    waist between (not a straight pillarbox: its side outline is not a full-height line)."""
    img = Image.new("RGB", (w, w), fx.WHITE)
    foot = w // 25
    img.paste(fx.DARK, (w // 5, 0, w * 4 // 5, foot))
    img.paste(fx.DARK, (w // 5, w - foot, w * 4 // 5, w))
    img.paste(fx.DARK, (w * 7 // 20, foot, w * 13 // 20, w - foot))
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


def test_background_run_inside_band_only_fails() -> None:
    """An off run that stops at the band's inner edge is not a product continuing inward."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    img.paste((128, 128, 128), (300, 0, 400, 20))
    assert passes(_png(img)) is False


def test_background_disc_touching_edge_passes() -> None:
    """A round product touching the bottom edge: most of its contact is partial band depth, but
    it continues inward at every position, so the run is forgiven."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    ImageDraw.Draw(img).ellipse((200, 200, 800, 999), fill=fx.DARK)
    assert passes(_png(img)) is True


# 6b. frames and stripes: an off run is forgiven only where it continues inward

GREY = (128, 128, 128)


def test_grey_frame_with_white_corners_fails() -> None:
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    for box in ((20, 0, 980, 20), (20, 980, 980, 1000), (0, 20, 20, 980), (980, 20, 1000, 980)):
        img.paste(GREY, box)
    assert codes(bg(_png(img))) == ["BACKGROUND_OFF"]


def test_grey_stripe_along_bottom_edge_fails() -> None:
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    img.paste(GREY, (20, 990, 980, 1000))
    assert codes(bg(_png(img))) == ["BACKGROUND_OFF"]


def test_thick_grey_frame_with_white_corners_fails() -> None:
    """A frame deeper than the band continues inward, so the run-length cap catches it."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    for box in ((20, 0, 980, 40), (20, 960, 980, 1000), (0, 20, 40, 980), (960, 20, 1000, 980)):
        img.paste(GREY, box)
    assert codes(bg(_png(img))) == ["BACKGROUND_OFF"]


@pytest.mark.parametrize(("run_len", "expected"), [(863, True), (864, False)])
def test_background_forgiven_run_cap(run_len: int, expected: bool) -> None:
    """Top edge strip spans x 20-980 (960 positions); a run is forgiven only below 90 % (864)."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    img.paste(fx.DARK, (20, 0, 20 + run_len, 500))
    assert passes(_png(img)) is expected


# 6c. a grey background with rounded or cut white corners: each edge's run continues inward and
# stays under the run cap, so the cross-edge rule (at most two edges with a forgiven run over
# WIDE_RUN_PERCENT % of the edge) catches it


def _rounded_grey(radius: int) -> Image.Image:
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    ImageDraw.Draw(img).rounded_rectangle((0, 0, 999, 999), radius=radius, fill=GREY)
    return img


@pytest.mark.parametrize("radius", [100, 150, 200, 250])
def test_rounded_grey_background_fails(radius: int) -> None:
    assert codes(bg(_png(_rounded_grey(radius)))) == ["BACKGROUND_OFF"]


@pytest.mark.parametrize("cut", [80, 150])
def test_octagon_grey_background_fails(cut: int) -> None:
    c = cut
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    pts = [
        (c, 0),
        (999 - c, 0),
        (999, c),
        (999, 999 - c),
        (999 - c, 999),
        (c, 999),
        (0, 999 - c),
        (0, c),
    ]
    ImageDraw.Draw(img).polygon(pts, fill=GREY)
    assert codes(bg(_png(img))) == ["BACKGROUND_OFF"]


@pytest.mark.parametrize(
    ("size", "radius"),
    [((2000, 1000), 150), ((2000, 1000), 300), ((1000, 2000), 300), ((1000, 750), 200)],
)
def test_rounded_grey_background_non_square_fails(size: tuple[int, int], radius: int) -> None:
    """Non-square canvases: the short edges' bands are deeper (2 % of the long side), so their
    inward line is in the grey card too and every edge counts as wide."""
    w, h = size
    img = Image.new("RGB", size, fx.WHITE)
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=GREY)
    assert codes(bg(_png(img))) == ["BACKGROUND_OFF"]


@pytest.mark.parametrize("radius", [300, 350])
def test_rounded_grey_background_large_radius_fails(radius: int) -> None:
    """The inward line meets the arc deeper than the outer edge, so a radius of 35 % of the side
    still leaves every edge's forgiven run over half the edge."""
    assert codes(bg(_png(_rounded_grey(radius)))) == ["BACKGROUND_OFF"]


def test_background_inscribed_disc_passes() -> None:
    """A disc touching all four edges covers about 28 % of each edge strip, under the 50 % cut."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    ImageDraw.Draw(img).ellipse((0, 0, 999, 999), fill=fx.DARK)
    assert passes(_png(img)) is True


def test_background_full_bleed_three_edges_fails() -> None:
    """Documented trade-off (D3, T110): a product touching three edges over more than half their
    length cannot be told from a grey card, so it fails closed."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    img.paste(fx.DARK, (200, 200, 800, 1000))
    img.paste(fx.DARK, (0, 200, 1000, 800))
    assert codes(bg(_png(img))) == ["BACKGROUND_OFF"]


def test_background_two_wide_edges_pass() -> None:
    """Two opposite edges with an aligned forgiven run over half their length pass when the
    outline at the run boundary is not a straight full line (a product, not a padded photo)."""
    assert passes(_png(_touching())) is True
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    bowtie = [
        (0, 200), (60, 200), (500, 400), (940, 200), (999, 200),
        (999, 800), (940, 800), (500, 600), (60, 800), (0, 800),
    ]  # fmt: skip
    ImageDraw.Draw(img).polygon(bowtie, fill=fx.DARK)
    assert passes(_png(img)) is True


@pytest.mark.parametrize("photo", [600, 750, 850])
@pytest.mark.parametrize("box", ["letterbox", "pillarbox"])
def test_background_padded_photo_fails(box: str, photo: int) -> None:
    """Round-3 F5: a grey photo padded to 1:1 with white bars. Two opposite edges forgive aligned
    wide runs, and the line just inside each run boundary is straight and full: a padded photo."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    a = (1000 - photo) // 2
    img.paste(GREY, (0, a, 1000, a + photo) if box == "letterbox" else (a, 0, a + photo, 1000))
    assert codes(bg(_png(img))) == ["BACKGROUND_OFF"]


@pytest.mark.parametrize(("step", "expected"), [(9, False), (10, True)])
def test_background_padded_photo_alignment(step: int, expected: bool) -> None:
    """Opposite runs line up when start and end differ by at most max(BAND_MIN_PX, 1 % of the
    edge) = 9 of 960 positions; past that the boundary line is not read (two wide edges pass)."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    img.paste(GREY, (0, 200, 500, 800))
    img.paste(GREY, (500, 200 + step, 1000, 800 + step))
    assert passes(_png(img)) is expected


def test_background_wide_octagon_card_fails() -> None:
    """A 2:1 grey card with corners cut 30 % of its height: the top and bottom runs are wide and
    aligned, and the cut's boundary column is off over 90 % of the height."""
    w, h, c = 2000, 1000, 300
    img = Image.new("RGB", (w, h), fx.WHITE)
    pts = [
        (c, 0), (w - 1 - c, 0), (w - 1, c), (w - 1, h - 1 - c),
        (w - 1 - c, h - 1), (c, h - 1), (0, h - 1 - c), (0, c),
    ]  # fmt: skip
    ImageDraw.Draw(img).polygon(pts, fill=GREY)
    assert codes(bg(_png(img))) == ["BACKGROUND_OFF"]


@pytest.mark.parametrize("radius", [400, 450])
def test_background_rounded_card_large_radius_accepted_limit(radius: int) -> None:
    """Accepted limit (D3, T110 (3)): a square grey card with corner radius >= 40 % of the side
    leaves every edge run under half the edge, so it reads as an inscribed product and passes;
    the vision step confirms the background."""
    assert passes(_png(_rounded_grey(radius))) is True


@pytest.mark.parametrize(("arm", "expected"), [(480, True), (481, False)])
def test_background_wide_run_cut(arm: int, expected: bool) -> None:
    """A plus touching all four edges; each edge strip has 960 positions, so a run counts as wide
    only above 50 % (480)."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    img.paste(fx.DARK, (260, 0, 260 + arm, 1000))
    img.paste(fx.DARK, (0, 260, 1000, 260 + arm))
    assert passes(_png(img)) is expected


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


def test_background_rows_are_anded_across_packs() -> None:
    """Pins TEAM-QUESTIONS T110 (2): background rows are not pack alternatives (only aspect ratio
    is), so a second row cannot express "white or light"; every selected row must pass."""
    rules = [
        image_rule("background", marketplace="flipkart", rgb=(255, 255, 255)),
        image_rule("background", marketplace="flipkart", pack="light_bg", rgb=(240, 240, 240)),
    ]
    for border in (fx.WHITE, (240, 240, 240)):
        report = run(fx.png(1000, 1000, border=border), rules, marketplace="flipkart")
        assert codes(report) == ["BACKGROUND_OFF"]


@pytest.mark.parametrize("variant", ["jpeg", "blurred", "off_centre", "dashed_boundary"])
def test_background_padded_photo_variants_fail(variant: str) -> None:
    """A padded photo still fails after JPEG artefacts, a soft boundary, unequal bars, or a few
    white dashes on the boundary row (the line test needs only 90 % off)."""
    img = Image.new("RGB", (1000, 1000), fx.WHITE)
    top = 50 if variant == "off_centre" else 200
    img.paste(GREY, (0, top, 1000, top + 600))
    if variant == "blurred":
        img = img.filter(ImageFilter.GaussianBlur(2))
    if variant == "dashed_boundary":
        draw = ImageDraw.Draw(img)
        for x in range(0, 1000, 40):
            draw.rectangle((x, 200, x + 5, 201), fill=fx.WHITE)
    data = fx.encode(img, "JPEG") if variant == "jpeg" else _png(img)
    assert codes(bg(data)) == ["BACKGROUND_OFF"]
