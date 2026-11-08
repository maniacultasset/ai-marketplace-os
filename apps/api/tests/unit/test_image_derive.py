"""SI1 T15 (AC9; PLAN D5, CP17): the `clean` and `vision` derivatives."""

from __future__ import annotations

import base64
import io
from typing import Any

import pytest
from app.services import image_derive
from app.services.image_derive import MAX_IMAGE_BYTES, derive
from mkos_schemas.jobs import MAX_CONTEXT_IMAGE_B64_CHARS
from PIL import Image
from tests import image_fixtures as fx

APP1 = b"\xff\xe1"
COM = b"\xff\xfe"


def _open(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _no_metadata(data: bytes) -> None:
    img = _open(data)
    assert len(img.getexif()) == 0
    assert b"Exif" not in data
    assert b"http://ns.adobe.com/xap" not in data
    assert b"AcmeCam" not in data
    assert b"shot by acme" not in data
    if data.startswith(b"\xff\xd8"):
        assert APP1 not in data
        assert COM not in data


def test_clean_has_no_exif() -> None:
    found = derive(fx.jpeg_with_gps_orientation6(40, 20))
    assert found is not None
    assert found.clean_mime == "image/jpeg"
    assert _open(found.clean).size == (20, 40)  # orientation 6 applied
    _no_metadata(found.clean)


def test_vision_has_no_metadata_either() -> None:
    found = derive(fx.jpeg_with_gps_orientation6(40, 20))
    assert found is not None
    _no_metadata(found.vision)
    assert (found.vision_w, found.vision_h) == (20, 40)


def test_icc_profile_kept() -> None:
    from PIL import ImageCms

    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    data = fx.encode(fx.canvas(32, 32), "JPEG", quality=90, icc_profile=icc)
    found = derive(data)
    assert found is not None
    assert _open(found.clean).info.get("icc_profile") == icc


def test_png_keeps_png_and_drops_text() -> None:
    found = derive(fx.png_with_text_chunk())
    assert found is not None
    assert found.clean_mime == "image/png"
    assert b"tEXt" not in found.clean and b"secret location" not in found.clean
    assert not _open(found.clean).text  # type: ignore[attr-defined]


def _transparent_png(mode: str) -> bytes:
    """600x600 with a transparent background (index/value 0) and an opaque centre square."""
    if mode == "P":
        img = Image.new("P", (600, 600), 0)
        img.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
        img.paste(1, (200, 200, 400, 400))
        transparency: Any = 0
    elif mode == "RGB":
        img = Image.new("RGB", (600, 600), (0, 0, 0))
        img.paste((255, 0, 0), (200, 200, 400, 400))
        transparency = (0, 0, 0)
    else:
        img = Image.new("L", (600, 600), 0)
        img.paste(128, (200, 200, 400, 400))
        transparency = 0
    buf = io.BytesIO()
    img.save(buf, format="PNG", transparency=transparency)
    return buf.getvalue()


@pytest.mark.parametrize("mode", ["P", "RGB", "L"])
def test_png_transparency_key_kept(mode: str) -> None:
    """Round 1 repro: a tRNS-keyed PNG keeps its transparent background in the clean copy and
    lands on white in the vision copy, never opaque black."""
    found = derive(_transparent_png(mode))
    assert found is not None
    assert found.clean_mime == "image/png"
    clean = _open(found.clean)
    assert clean.convert("RGBA").getpixel((0, 0))[3] == 0  # type: ignore[index]
    assert clean.convert("RGBA").getpixel((300, 300))[3] == 255  # type: ignore[index]
    assert b"tEXt" not in found.clean
    assert _open(found.vision).convert("RGB").getpixel((0, 0)) == (255, 255, 255)


def test_webp_rgba_becomes_jpeg_on_white() -> None:
    found = derive(fx.webp_rgba())
    assert found is not None
    assert found.clean_mime == "image/jpeg"
    img = _open(found.clean).convert("RGB")
    corner = img.getpixel((0, 0))
    assert isinstance(corner, tuple) and min(corner) > 240


def test_vision_long_side_capped() -> None:
    found = derive(fx.encode(fx.canvas(3000, 1000), "JPEG", quality=90))
    assert found is not None
    assert max(found.vision_w, found.vision_h) <= 1024
    assert found.vision.startswith(b"\xff\xd8\xff")
    assert len(base64.b64encode(found.vision)) <= MAX_CONTEXT_IMAGE_B64_CHARS
    assert _open(found.vision).format == "JPEG"


def test_vision_under_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # a tiny cap forces the quality loop; a cap no quality meets gives None
    found = derive(fx.encode(fx.canvas(800, 800), "JPEG", quality=95))
    assert found is not None
    size = len(base64.b64encode(found.vision))
    monkeypatch.setattr(image_derive, "MAX_CONTEXT_IMAGE_B64_CHARS", size - 1)
    lower = derive(fx.encode(fx.canvas(800, 800), "JPEG", quality=95))
    assert lower is None or len(base64.b64encode(lower.vision)) < size
    monkeypatch.setattr(image_derive, "MAX_CONTEXT_IMAGE_B64_CHARS", 10)
    assert derive(fx.encode(fx.canvas(800, 800), "JPEG", quality=95)) is None


def test_clean_capped_at_max_image_bytes() -> None:
    data = fx.noisy_png_rgba(2500, 2500)  # random pixels: the PNG is far over 10 MiB
    assert len(data) > MAX_IMAGE_BYTES
    found = derive(data)
    assert found is None or len(found.clean) <= MAX_IMAGE_BYTES


def test_clean_cap_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """A small cap: the PNG is re-encoded as JPEG, then downscaled, else None."""
    data = fx.noisy_png_rgba(300, 300)
    monkeypatch.setattr(image_derive, "MAX_IMAGE_BYTES", 60_000)
    found = derive(data)
    assert found is None or (found.clean_mime == "image/jpeg" and len(found.clean) <= 60_000)
    monkeypatch.setattr(image_derive, "MAX_IMAGE_BYTES", 10)
    assert derive(data) is None


@pytest.mark.parametrize(
    "data",
    [
        fx.truncated(fx.jpeg(64, 64), 100),
        fx.png_header_only(30000, 30000),
        fx.png_header_only(20000, 5000),
        b"",
        b"not an image",
    ],
)
def test_hostile_input_is_none(data: bytes) -> None:
    assert derive(data) is None


def test_memory_error_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise MemoryError

    monkeypatch.setattr(Image, "open", boom)
    assert derive(fx.jpeg(16, 16)) is None


# ---- SI7 T8: the border profile travels with the derivatives ----


def test_border_profile_of_a_near_white_photo() -> None:
    found = derive(fx.png(400, 400, border=(250, 250, 250)))
    assert found is not None and found.border is not None
    assert found.border == {"uniform": True, "near_white": True, "colour": [250, 250, 250]}


def test_border_profile_of_a_grey_photo() -> None:
    found = derive(fx.jpeg(400, 400, border=(200, 200, 200), quality=95))
    assert found is not None and found.border is not None
    assert found.border["uniform"] is True and found.border["near_white"] is False
