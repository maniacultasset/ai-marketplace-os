"""S26 T5 [C3]: the limiter 429's `Retry-After` is the whole seconds left in the minute window."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.services.rate_limit import seconds_left, window_start


@pytest.mark.parametrize(
    ("second", "micro", "want"),
    [(0, 0, 60), (59, 900_000, 1), (30, 0, 30), (59, 0, 1)],
    ids=["0", "59.9", "30", "59"],
)
def test_retry_after_seconds_bounds(second: int, micro: int, want: int) -> None:
    moment = datetime(2026, 10, 6, 12, 0, second, micro, tzinfo=UTC)
    assert seconds_left(moment) == want
    assert 1 <= seconds_left(moment) <= 60
    assert window_start(moment) == datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def test_retry_after_next_window_is_60() -> None:
    # the first instant of the next window ("60" seconds in) starts a full minute again
    assert seconds_left(datetime(2026, 10, 6, 12, 1, 0, tzinfo=UTC)) == 60
