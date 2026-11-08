"""SI7 T5: fix-only framing (AC7). Synthetic images only; every output is judged by the same
`check_image` the uploads get."""

from __future__ import annotations

from io import BytesIO

import pytest
from app.services import image_fix
from app.services.image_fix import (
    FixRefused,
    border_profile,
    fix,
    foreground_bbox,
    frame_square,
    whiten_border,
)
from PIL import Image, ImageDraw
from tests.image_fixtures import amazon_rules, canvas, encode, run

PRODUCT = (40, 70, 160)


def _fill(img: Image.Image) -> float:
    bbox = foreground_bbox(img)
    assert bbox is not None
    long_side = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    return long_side / img.width


def _band_is_white(img: Image.Image) -> bool:
    w, h = img.size
    d = max(2, w // 50)
    for box in ((0, 0, w, d), (0, h - d, w, h), (0, 0, d, h), (w - d, 0, w, h)):
        if img.crop(box).getextrema() != ((255, 255), (255, 255), (255, 255)):
            return False
    return True


def _passes_amazon_main(img: Image.Image) -> bool:
    report = run(encode(img, "JPEG", quality=92), amazon_rules())
    failed = [c.check for c in report.checks if not c.passed and c.method == "deterministic"]
    return not failed


def _product_on(w: int, h: int, bg: tuple[int, int, int], pw: int, ph: int) -> Image.Image:
    img = Image.new("RGB", (w, h), bg)
    left, top = (w - pw) // 2, (h - ph) // 2
    img.paste(PRODUCT, (left, top, left + pw, top + ph))
    return img


def test_off_white_portrait_becomes_a_white_square() -> None:
    out = fix(canvas(900, 1200, border=(245, 245, 245), product=PRODUCT, fill=0.6))
    assert out.width == out.height >= 1000
    assert _band_is_white(out)
    assert _fill(out) == pytest.approx(0.85, abs=0.02)
    assert _passes_amazon_main(out)


def test_scale_one_keeps_the_product_pixels() -> None:
    src = Image.new("RGB", (1200, 1200), (245, 245, 245))
    draw = ImageDraw.Draw(src)
    for i in range(1020):  # a solid product striped in many colours, so any resample would show
        draw.line([(90 + i, 90), (90 + i, 1109)], fill=(40 + i % 150, 70 + i % 7 * 9, 160 - i % 90))
    out = fix(src)
    assert out.size == (1200, 1200)  # ceil(1020 / 0.85) = 1200, inside 1000..2000: scale 1.0
    crop = src.crop((90, 90, 1110, 1110))
    ox, oy = (1200 - crop.width) // 2, (1200 - crop.height) // 2
    got = out.crop((ox, oy, ox + crop.width, oy + crop.height))
    assert got.tobytes() == crop.tobytes()


def test_too_small_and_the_upscale_cap() -> None:
    with pytest.raises(FixRefused) as small:
        fix(_product_on(600, 600, (245, 245, 245), 500, 500))
    assert small.value.reason == "too_small"
    # 680 / 0.85 = 800, upscaled 1.25x to 1000: exactly the cap passes
    out = fix(_product_on(800, 800, (245, 245, 245), 680, 680))
    assert out.size == (1000, 1000)
    with pytest.raises(FixRefused) as over:
        fix(_product_on(800, 800, (245, 245, 245), 679, 679))
    assert over.value.reason == "too_small"


def test_white_product_touching_the_border_leaks() -> None:
    # a white shirt (250) touching both side edges, with a small dark print on it: whitening the
    # background would also whiten the shirt, leaving only the print
    img = Image.new("RGB", (1200, 1200), (245, 245, 245))
    img.paste((250, 250, 250), (0, 100, 1200, 1100))
    img.paste(PRODUCT, (500, 500, 700, 700))
    with pytest.raises(FixRefused) as info:
        fix(img)
    assert info.value.reason == "source_unusable"


def test_grey_border_is_the_ai_paths_job() -> None:
    with pytest.raises(FixRefused) as info:
        fix(canvas(1200, 1200, border=(200, 200, 200), product=PRODUCT))
    assert info.value.reason == "source_unusable"


@pytest.mark.parametrize(
    "img",
    [
        Image.new("RGB", (1, 1), (10, 10, 10)),
        canvas(4000, 10, border=(245, 245, 245), product=PRODUCT),
        Image.new("RGB", (1200, 1200), (255, 255, 255)),
        Image.new("RGB", (1200, 1200), (245, 245, 245)),
    ],
    ids=["1x1", "sliver", "all-white", "all-off-white"],
)
def test_degenerate_sources_are_unusable(img: Image.Image) -> None:
    with pytest.raises(FixRefused) as info:
        fix(img)
    assert info.value.reason == "source_unusable"


def test_large_product_is_downscaled_to_the_cap() -> None:
    out = fix(_product_on(6000, 6000, (245, 245, 245), 5000, 5000))
    assert out.size == (2000, 2000)
    assert _fill(out) == pytest.approx(0.85, abs=0.02)


def test_border_profiles() -> None:
    gradient = Image.new("RGB", (400, 400))
    gradient.putdata([(x % 256, 100, 255 - x % 256) for _ in range(400) for x in range(400)])
    assert not border_profile(gradient).uniform
    prof = border_profile(canvas(600, 600, border=(250, 250, 250), product=PRODUCT))
    assert prof.uniform and prof.near_white and prof.colour == (250, 250, 250)
    grey = border_profile(canvas(600, 600, border=(200, 200, 200), product=PRODUCT))
    assert grey.uniform and not grey.near_white


def _gradient_bg(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h))
    img.putdata([(240 + 10 * x // w,) * 3 for _ in range(h) for x in range(w)])
    return img


def test_real_off_white_gradient_passes_the_main_rules() -> None:
    img = _gradient_bg(1100, 1300)
    img.paste(PRODUCT, (250, 300, 850, 1000))
    out = fix(img)
    assert _passes_amazon_main(out)
    assert _fill(out) == pytest.approx(0.85, abs=0.02)


def test_jpeg_noise_off_white_passes_the_main_rules() -> None:
    src = canvas(1100, 1300, border=(245, 245, 245), product=PRODUCT, fill=0.6)
    noisy = Image.open(BytesIO(encode(src, "JPEG", quality=85)))
    out = fix(noisy)
    assert _passes_amazon_main(out)
    assert _fill(out) == pytest.approx(0.85, abs=0.02)


def test_soft_shadow_never_gives_a_failing_output() -> None:
    img = canvas(1200, 1200, border=(245, 245, 245), product=PRODUCT, fill=0.5)
    draw = ImageDraw.Draw(img)
    for i in range(30):
        shade = 200 + i
        draw.ellipse((300 - i, 880 + i // 3, 900 + i, 940 - i // 3), outline=(shade,) * 3)
    try:
        out = fix(img)
    except FixRefused:
        return
    assert _passes_amazon_main(out)


def test_whiten_never_touches_the_product_colour() -> None:
    img = canvas(800, 800, border=(245, 245, 245), product=PRODUCT, fill=0.5)
    out = whiten_border(img)
    assert out.getpixel((400, 400)) == PRODUCT
    assert out.getpixel((5, 5)) == (255, 255, 255)


def test_frame_square_refuses_an_empty_frame() -> None:
    with pytest.raises(FixRefused) as info:
        frame_square(Image.new("RGB", (900, 900), (255, 255, 255)))
    assert info.value.reason == "source_unusable"


def test_constants_are_pinned() -> None:
    assert image_fix.LEAK_MAX == 0.92
    assert image_fix.FILL_TARGET == 0.85
    assert (image_fix.OUT_MIN, image_fix.OUT_MAX, image_fix.MAX_UPSCALE) == (1000, 2000, 1.25)
