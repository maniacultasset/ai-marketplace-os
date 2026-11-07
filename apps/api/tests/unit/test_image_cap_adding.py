"""SI1 T14 (CP8): the live image job cap counts the jobs a call is about to add."""

from __future__ import annotations

from typing import Any

import pytest
from app.core.errors import LimitError
from app.services.images import LIVE_IMAGE_JOB_CAP, enforce_live_image_cap


class _Images:
    def __init__(self, live: int) -> None:
        self.live = live

    async def count_live_image_jobs(self) -> int:
        return self.live


@pytest.mark.parametrize(
    ("live", "adding", "refused"),
    [(15, 8, True), (15, 5, False), (19, 1, False), (20, 1, True), (0, 20, False), (0, 21, True)],
)
async def test_cap_counts_the_new_jobs(live: int, adding: int, refused: bool) -> None:
    images: Any = _Images(live)
    assert LIVE_IMAGE_JOB_CAP == 20
    if refused:
        with pytest.raises(LimitError):
            await enforce_live_image_cap(images, adding=adding)
    else:
        await enforce_live_image_cap(images, adding=adding)


async def test_default_adding_keeps_the_single_upload_rule() -> None:
    images: Any = _Images(20)
    with pytest.raises(LimitError):
        await enforce_live_image_cap(images)
    await enforce_live_image_cap(_Images(19))  # type: ignore[arg-type]
