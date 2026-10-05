"""Band geometry at tiny sizes: the corner/edge split and its one-line-deeper edge crops never
crash, and a plain white image always passes, around the whole-image fallback boundary."""

from __future__ import annotations

import pytest
from PIL import Image
from tests import image_fixtures as fx
from tests.image_fixtures import codes, image_rule, run

RULES = [image_rule("background", rgb=(255, 255, 255))]
SIZES = [1, 2, 3, 4, 5, 6, 7, 49, 50, 51, 99, 100, 101, 149, 150, 151]


@pytest.mark.parametrize("w", SIZES)
@pytest.mark.parametrize("h", SIZES)
def test_white_passes_and_dark_fails_at_every_small_size(w: int, h: int) -> None:
    white = fx.encode(Image.new("RGB", (w, h), fx.WHITE), "PNG")
    dark = fx.encode(Image.new("RGB", (w, h), fx.DARK), "PNG")
    assert codes(run(white, RULES)) == []
    assert codes(run(dark, RULES)) == ["BACKGROUND_OFF"]
