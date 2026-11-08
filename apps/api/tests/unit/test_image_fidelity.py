"""SI7 T6: the fidelity judge, the registration and the composite (AC5). Scenes are drawn at
512 px, so the judge's 256 px work size halves every length."""

from __future__ import annotations

import colorsys
import random
from collections.abc import Callable

import pytest
from app.services import image_fidelity as fid
from app.services.image_fidelity import composite, decide, judge, pad_square
from app.services.image_fix import BorderProfile, border_profile
from PIL import Image, ImageChops, ImageDraw, ImageFilter

SIDE = 512
WHITE = (255, 255, 255)
OFF_WHITE = (240, 240, 240)
RED = (200, 40, 40)
STRIPES = ((60, 150, 80), (70, 90, 170))


def _scene(
    bg: tuple[int, int, int] | None,
    draw_product: Callable[[ImageDraw.ImageDraw], None],
    *,
    side: int = SIDE,
) -> Image.Image:
    img = Image.new("RGB", (side, side), bg or WHITE)
    draw = ImageDraw.Draw(img)
    if bg is None:  # a busy two-colour striped background
        for x in range(0, side, 16):
            draw.rectangle((x, 0, x + 15, side), fill=STRIPES[(x // 16) % 2])
    draw_product(draw)
    return img


def _box(draw: ImageDraw.ImageDraw, dx: int = 0, colour: tuple[int, int, int] = RED) -> None:
    draw.rectangle((156 + dx, 156, 356 + dx, 356), fill=colour)


def _hue(rgb: tuple[int, int, int], degrees: float) -> tuple[int, int, int]:
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    r, g, b = colorsys.hsv_to_rgb((h + degrees / 360) % 1, s, v)
    return round(r * 255), round(g * 255), round(b * 255)


def _profile(img: Image.Image) -> BorderProfile:
    return border_profile(img)


def test_background_swapped_to_white_passes() -> None:
    original = _scene(OFF_WHITE, _box)
    edit = _scene(WHITE, _box)
    result = judge(original, edit, _profile(original))
    assert result.passed and result.agreement >= 0.98
    assert result.mask_iou is not None and result.mask_iou >= 0.95
    assert result.hole_share == 0


def test_shifted_product_is_registered() -> None:
    """The model moved the product by a tenth of the frame: the registration finds the move and
    the mask still fits the seller's product."""
    original = _scene(OFF_WHITE, _box)
    edit = _scene(WHITE, lambda d: _box(d, dx=int(0.10 * SIDE)))
    result = judge(original, edit, _profile(original))
    assert result.passed and result.mask_iou is not None and result.mask_iou >= 0.95
    assert abs(result.alignment.dx - 0.10) <= 0.01 and abs(result.alignment.dy) <= 0.01


def test_recoloured_product_fails_on_colour() -> None:
    original = _scene(OFF_WHITE, _box)
    edit = _scene(WHITE, lambda d: _box(d, colour=_hue(RED, 30)))
    result = judge(original, edit, _profile(original))
    assert not result.passed and result.colour_distance > fid.HIST_MAX


def test_scaled_product_is_registered() -> None:
    original = _scene(OFF_WHITE, _box)
    edit = _scene(WHITE, lambda d: d.rectangle((176, 176, 336, 336), fill=RED))  # 0.8 x
    result = judge(original, edit, _profile(original))
    assert result.passed and result.mask_iou is not None and result.mask_iou >= 0.9
    assert abs(result.alignment.sx - 0.8) <= 0.02 and abs(result.alignment.sy - 0.8) <= 0.02


@pytest.mark.parametrize(
    "box",
    [
        (206, 206, 306, 306),  # half the size: beyond SCALE_MIN
        (0, 0, 200, 200),  # moved to the corner: beyond SHIFT_MAX
        (156, 206, 356, 306),  # squashed to half the height: beyond ASPECT_MAX
    ],
)
def test_product_beyond_the_registration_range_fails(box: tuple[int, int, int, int]) -> None:
    original = _scene(OFF_WHITE, _box)
    edit = _scene(WHITE, lambda d: d.rectangle(box, fill=RED))
    assert not judge(original, edit, _profile(original)).passed


def test_busy_background_skips_iou() -> None:
    original = _scene(None, _box)
    edit = _scene(WHITE, _box)
    prof = _profile(original)
    assert not prof.uniform
    result = judge(original, edit, prof)
    assert result.mask_iou is None and result.passed


def test_non_square_edit_fails_without_scoring() -> None:
    original = _scene(OFF_WHITE, _box)
    edit = Image.new("RGB", (512, 683), WHITE)
    ImageDraw.Draw(edit).rectangle((156, 208, 356, 475), fill=RED)  # the product, stretched
    result = judge(original, edit, _profile(original))
    assert not result.passed and result.agreement == 0


def test_all_white_edit_fails() -> None:
    original = _scene(OFF_WHITE, _box)
    assert not judge(original, Image.new("RGB", (SIDE, SIDE), WHITE), _profile(original)).passed


def _bag(draw: ImageDraw.ImageDraw, *, strap_top: bool = True) -> None:
    draw.rectangle((116, 340, 396, 460), fill=RED)  # body
    draw.rectangle((116, 180, 140, 340), fill=RED)  # left handle
    draw.rectangle((372, 180, 396, 340), fill=RED)  # right handle
    if strap_top:
        draw.rectangle((116, 154, 396, 180), fill=RED)  # the strap across the top


def test_busy_background_erased_strap_fails_on_holes() -> None:
    original = _scene(None, _bag)
    prof = _profile(original)
    erased = judge(original, _scene(WHITE, lambda d: _bag(d, strap_top=False)), prof)
    assert erased.mask_iou is None and erased.agreement >= fid.AGREEMENT_MIN
    assert erased.hole_share > fid.HOLE_MAX and not erased.passed
    kept = judge(original, _scene(WHITE, _bag), prof)
    assert kept.passed


def _shirt(draw: ImageDraw.ImageDraw, *, right_sleeve: bool = True) -> None:
    draw.rectangle((192, 56, 320, 456), fill=RED)  # body
    draw.rectangle((152, 56, 192, 456), fill=RED)  # left sleeve
    if right_sleeve:
        draw.rectangle((320, 56, 360, 456), fill=RED)


def test_uniform_border_erased_sleeve_fails_on_holes_with_good_iou() -> None:
    original = _scene((250, 250, 250), _shirt)
    result = judge(
        original, _scene(WHITE, lambda d: _shirt(d, right_sleeve=False)), _profile(original)
    )
    assert result.mask_iou is not None and result.mask_iou >= fid.IOU_MIN
    assert result.hole_share > fid.HOLE_MAX and not result.passed


BEIGE = (214, 198, 172)


def _white_padded(
    colour: tuple[int, int, int] = RED, inset: int = 0
) -> tuple[Image.Image, Image.Image, Image.Image]:
    """A 384 x 512 beige photo padded with white, and the matching edit (white, product kept or
    drawn `inset` px smaller on each side)."""
    photo = Image.new("RGB", (384, SIDE), BEIGE)
    ImageDraw.Draw(photo).rectangle((92, 156, 292, 356), fill=RED)
    original = pad_square(photo, fid.PAD_COLOUR)
    edit = Image.new("RGB", (SIDE, SIDE), WHITE)
    box = (156 + inset, 156 + inset, 356 - inset, 356 - inset)
    ImageDraw.Draw(edit).rectangle(box, fill=colour)
    return photo, original, edit


def test_white_padded_photo_is_judged_inside_the_photo() -> None:
    photo, original, edit = _white_padded()
    prof = _profile(photo)
    assert prof.uniform and not prof.near_white
    result = judge(original, edit, prof, photo_size=photo.size)
    assert result.passed and result.mask_iou is not None and result.mask_iou >= 0.95
    # Without the photo's size the white bars read as product (far from beige) and IOU fails.
    blind = judge(original, edit, prof)
    assert blind.mask_iou is not None and blind.mask_iou < fid.IOU_MIN


def test_white_padded_photo_still_fails_a_changed_product() -> None:
    photo, original, _ = _white_padded()
    prof = _profile(photo)
    _, _, redrawn = _white_padded()
    ImageDraw.Draw(redrawn).rectangle((156, 256, 356, 356), fill=(40, 60, 170))  # a new panel
    changed = judge(original, redrawn, prof, photo_size=photo.size)
    assert changed.agreement < fid.AGREEMENT_MIN and not changed.passed
    _, _, recoloured = _white_padded(colour=_hue(RED, 30))
    result = judge(original, recoloured, prof, photo_size=photo.size)
    assert not result.passed and result.colour_distance > fid.HIST_MAX


def test_composite_of_a_white_padded_photo_pastes_no_padding_colour() -> None:
    _, original, edit = _white_padded()
    stray = edit.copy()
    ImageDraw.Draw(stray).rectangle((0, 0, 40, SIDE), fill=BEIGE)  # the model kept a bar
    out = composite(original, stray)
    assert out.getpixel((10, 200)) == WHITE  # the seller's padding is white, not beige
    assert out.getpixel((256, 256)) == original.getpixel((256, 256)) == RED


@pytest.mark.parametrize(
    ("scores", "passed"),
    [
        ({"agreement": fid.AGREEMENT_MIN}, True),
        ({"agreement": fid.AGREEMENT_MIN - 0.001}, False),
        ({"colour_distance": fid.HIST_MAX}, True),
        ({"colour_distance": fid.HIST_MAX + 0.001}, False),
        ({"mask_iou": fid.IOU_MIN}, True),
        ({"mask_iou": fid.IOU_MIN - 0.001}, False),
        ({"mask_iou": None}, True),
        ({"hole_share": fid.HOLE_MAX}, True),
        ({"hole_share": fid.HOLE_MAX + 0.001}, False),
    ],
)
def test_decide_threshold_pairs(scores: dict[str, float | None], passed: bool) -> None:
    base: dict[str, float | None] = {
        "mask_iou": 0.99,
        "colour_distance": 0.0,
        "agreement": 1.0,
        "hole_share": 0.0,
    }
    assert decide(**{**base, **scores}) is passed  # type: ignore[arg-type]


def test_constants_are_pinned() -> None:
    assert (fid.WORK_PX, fid.ASPECT_TOL, fid.BLUR, fid.CAND_WHITE) == (256, 0.02, 1, 245)
    assert (fid.ORIG_DIST, fid.COLOUR_TOL, fid.HIST_BINS) == (24, 32, 8)
    assert (fid.AGREEMENT_MIN, fid.HIST_MAX, fid.IOU_MIN) == (0.90, 0.15, 0.80)
    assert (fid.PALETTE_MIN, fid.HOLE_MAX, fid.HOLE_RING) == (0.02, 0.10, 3)
    assert (fid.PAD_COLOUR, fid.PHOTO_EDGE) == ((255, 255, 255), 2)
    assert (fid.SCALE_MIN, fid.SCALE_MAX, fid.ASPECT_MAX, fid.SHIFT_MAX) == (0.80, 1.25, 0.01, 0.25)
    assert (fid.COARSE_PX, fid.FIT_PX, fid.FINE_PX, fid.DIFF_CAP, fid.BG_GAP) == (
        32,
        128,
        512,
        64,
        0.06,
    )


def test_aspect_tolerance_pair() -> None:
    original = _scene(OFF_WHITE, _box)
    inside = Image.new("RGB", (512, 522), WHITE)  # 1.9 % off
    ImageDraw.Draw(inside).rectangle((156, 156, 356, 361), fill=RED)
    assert judge(original, inside, _profile(original)).agreement > 0
    outside = Image.new("RGB", (512, 524), WHITE)  # 2.3 % off
    ImageDraw.Draw(outside).rectangle((156, 156, 356, 362), fill=RED)
    assert judge(original, outside, _profile(original)).agreement == 0


def test_composite_keeps_the_sellers_pixels() -> None:
    original = Image.new("RGB", (1200, 1200), OFF_WHITE)
    draw = ImageDraw.Draw(original)
    for i in range(500):
        draw.line([(350 + i, 350), (350 + i, 849)], fill=(60 + i % 120, 30 + i % 50, 90))
    edit = Image.new("RGB", (1024, 1024), WHITE)  # the model's size, the product drawn flat
    ImageDraw.Draw(edit).rectangle((299, 299, 724, 724), fill=(80, 40, 90))
    out = composite(original, edit)
    assert out.size == (1200, 1200)
    for xy in ((400, 400), (600, 600), (800, 800), (420, 780)):
        assert out.getpixel(xy) == original.getpixel(xy)
    assert out.getpixel((10, 10)) == WHITE


def test_pad_square() -> None:
    img = Image.new("RGB", (300, 400), (1, 2, 3))
    out = pad_square(img, (250, 250, 250))
    assert out.size == (400, 400)
    assert out.getpixel((0, 0)) == (250, 250, 250) and out.getpixel((200, 200)) == (1, 2, 3)
    assert pad_square(out, (0, 0, 0)) is out


# ---- registration: the model's mask, the seller's pixels ----

BEIGE_ROOM = (214, 198, 172)


def _print(img: Image.Image, box: tuple[int, int, int, int]) -> None:
    """A printed product: navy with rust and gold diagonal bands, so a misregistered mask shows
    (diagonal, so no whole row of pixels sits on one histogram bin boundary)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    tile = Image.new("RGB", (w, h), (48, 48, 112))
    draw = ImageDraw.Draw(tile)
    for k, x in enumerate(range(-h, w, 64)):
        draw.line((x, h, x + h, 0), fill=((176, 80, 48), (208, 144, 80))[k % 2], width=24)
    # cloth texture: smooth blotches of light and shade, so the colours spread over many
    # histogram bins as in a photo
    rng = random.Random(7)  # noqa: S311 - a fixed texture, not a secret
    shade = Image.new("L", (w // 8, h // 8))
    shade.putdata([128 + rng.randint(-40, 40) for _ in range((w // 8) * (h // 8))])
    shade = shade.resize((w, h), Image.Resampling.BICUBIC).convert("RGB")
    img.paste(ImageChops.add(tile, shade, 1.0, -128), (x0, y0))


def _room_photo() -> Image.Image:
    """A 384 x 512 photo: the product in a beige room with a plant-green stripe and a dark floor."""
    photo = Image.new("RGB", (384, SIDE), BEIGE_ROOM)
    draw = ImageDraw.Draw(photo)
    draw.rectangle((330, 0, 384, 300), fill=(70, 110, 60))
    draw.rectangle((0, 440, 384, SIDE), fill=(110, 80, 60))
    _print(photo, (112, 120, 272, 420))
    return photo.filter(ImageFilter.GaussianBlur(1.5))  # a lens, not a vector drawing


def _model_edit(original: Image.Image, scale: float, dx: float, dy: float) -> Image.Image:
    """What a model returns: the product alone on white, scaled about the centre and shifted (in
    units of the side), at the model's 1024 px."""
    n = original.width
    cut = Image.new("RGB", original.size, WHITE)
    _print(cut, (112 + 64, 120, 272 + 64, 420))  # the photo sits 64 px in
    cut = cut.filter(ImageFilter.GaussianBlur(1.5))
    c = n / 2
    data = (1 / scale, 0, c - (c + dx * n) / scale, 0, 1 / scale, c - (c + dy * n) / scale)
    moved = cut.transform(cut.size, Image.Transform.AFFINE, data, fillcolor=WHITE)
    return moved.resize((1024, 1024), Image.Resampling.LANCZOS)


@pytest.mark.parametrize(
    ("scale", "dx", "dy"), [(1.0, 0.0, 0.0), (0.93, 0.06, -0.03), (1.08, -0.05, 0.04)]
)
def test_moved_model_output_keeps_the_sellers_pixels(scale: float, dx: float, dy: float) -> None:
    photo = _room_photo()
    original = pad_square(photo, fid.PAD_COLOUR)
    edit = _model_edit(original, scale, dx, dy)
    result = judge(original, edit, _profile(photo), photo_size=photo.size)
    assert result.passed, result
    al = result.alignment
    assert abs(al.sx - scale) <= 0.01 and abs(al.sy - scale) <= 0.01
    assert abs(al.dx - dx) <= 0.01 and abs(al.dy - dy) <= 0.01
    out = composite(original, edit, al)
    for xy in ((200, 140), (256, 270), (320, 400), (190, 131)):  # inside the print, off its edge
        assert out.getpixel(xy) == original.getpixel(xy)
    for xy in ((100, 50), (420, 100), (300, 480), (10, 10)):  # the room, the floor, the padding
        assert out.getpixel(xy) == WHITE


def test_moved_and_recoloured_model_output_still_fails() -> None:
    photo = _room_photo()
    original = pad_square(photo, fid.PAD_COLOUR)
    r, g, b = _model_edit(original, 0.95, 0.04, 0.0).split()
    swapped = Image.merge("RGB", (b, g, r))  # navy turns brown, rust turns blue
    result = judge(original, swapped, _profile(photo), photo_size=photo.size)
    assert not result.passed and result.agreement < fid.AGREEMENT_MIN


def test_moved_model_output_with_a_part_erased_still_fails() -> None:
    photo = _room_photo()
    original = pad_square(photo, fid.PAD_COLOUR)
    edit = _model_edit(original, 0.95, 0.04, 0.0)
    ImageDraw.Draw(edit).rectangle((0, 0, 1024, 420), fill=WHITE)  # the top third erased
    result = judge(original, edit, _profile(photo), photo_size=photo.size)
    assert result.hole_share > fid.HOLE_MAX and not result.passed
