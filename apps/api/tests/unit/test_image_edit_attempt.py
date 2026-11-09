"""The `attempt` a white-background edit request carries per try (`edit_attempt`)."""

from __future__ import annotations

import pytest
from app.workers.image_candidate import FIDELITY_TRIES, edit_attempt


def test_the_first_try_keeps_the_job_attempt() -> None:
    assert edit_attempt(1, 1) == 1


def test_the_fidelity_retry_is_the_next_attempt() -> None:
    assert FIDELITY_TRIES == 2
    assert edit_attempt(1, 2) == 2


@pytest.mark.parametrize(
    ("job_attempt", "try_no", "expected"), [(0, 1, 1), (3, 1, 3), (3, 2, 3), (2, 2, 3)]
)
def test_the_attempt_stays_within_the_schema_bounds(
    job_attempt: int, try_no: int, expected: int
) -> None:
    assert edit_attempt(job_attempt, try_no) == expected
