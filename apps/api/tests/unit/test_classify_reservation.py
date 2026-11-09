"""The reservation of one classification follows its model route (`CATEGORY_SUGGEST_MODEL`)."""

from __future__ import annotations

from decimal import Decimal

from app.core.config import Settings
from app.workers.budget import (
    CLASSIFY_RESERVATION_USD,
    CLASSIFY_VISION_RESERVATION_USD,
    classify_reservation_usd,
)


def test_vision_route_reserves_enough_for_three_gpt_4o_samples_with_six_photos() -> None:
    # 3 samples x (about 850 text + 6 x 85 photo tokens at USD 2.50/M + 120 output at USD 10/M)
    worst = 3 * (Decimal(850 + 6 * 85) * Decimal("2.50") + Decimal(120) * Decimal(10)) / 10**6
    assert (
        classify_reservation_usd(Settings(_env_file=None, app_env="local"))
        == CLASSIFY_VISION_RESERVATION_USD
    )
    assert worst <= CLASSIFY_VISION_RESERVATION_USD


def test_text_route_keeps_the_small_reservation() -> None:
    s = Settings(_env_file=None, app_env="local", category_suggest_model="text")
    assert classify_reservation_usd(s) == CLASSIFY_RESERVATION_USD == Decimal("0.005")
