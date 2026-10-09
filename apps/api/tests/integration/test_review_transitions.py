"""S19 T14 (AC 10; DATABASE.md §6.1): every transition S19 makes is a row of 0004's
`listing_status_transitions`, and a status the API refuses is also refused by the guard."""

from __future__ import annotations

import psycopg
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .review_support import generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


async def test_s19_transitions_match_seed(gen_world: GenWorld) -> None:
    from app.services.review import REVIEW_EDGES

    for from_status, event, to_status, reason in REVIEW_EDGES:
        row = gen_world.conn.execute(
            "SELECT allowed_reasons FROM listing_status_transitions "
            "WHERE from_status = %s AND event = %s AND to_status = %s",
            [from_status, event, to_status],
        ).fetchone()
        assert row is not None, (from_status, event, to_status)
        assert (row[0] is None) == (reason is None)
        if reason is not None:
            assert reason in row[0]


@pytest.mark.parametrize(
    ("status", "event", "to"),
    [
        ("blocked", "approve", "approved"),
        ("created", "approve", "approved"),
        ("approved", "reject", "blocked"),
        ("published", "approve", "approved"),
    ],
)
async def test_s19_refusals_match_guard(
    gen_world: GenWorld, status: str, event: str, to: str
) -> None:
    """The API refuses these before SQL (T8, T11); a raw UPDATE bypassing it gets LST01."""
    made = await generated(gen_world)
    set_status(gen_world, made.listing_id, status)
    reason = "rejected" if to == "blocked" else None
    with pytest.raises(psycopg.Error) as caught, gen_world.conn.transaction():
        gen_world.conn.execute(
            "UPDATE listings SET status = %s, status_reason = %s, last_event = %s WHERE id = %s",
            [to, reason, event, made.listing_id],
        )
    assert caught.value.sqlstate == "LST01"
