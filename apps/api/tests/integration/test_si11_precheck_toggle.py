"""SI11 fix round 3 (R3-1, the tester repro): a fact toggled twice keeps a truthful pre-check.

R3-1: A -> B -> A -> B. On the second B, validate finds equal pins and no open pre-check ERROR
(A2 closed it), so no dry run is asked; remote_check stays A2 clean for facts B the marketplace
refused, and approve is 200.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs, preview_rows
from .publish_support import PubWorld, pub
from .review_support import generated
from .test_remote_preview_revert import _colour_error, _run_previews, _set_colour

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev", "pub"]


def _state(pw: PubWorld, listing_id: uuid.UUID) -> tuple[str, int]:
    open_rows = [r for r in preview_rows(pw, listing_id) if r["status"] == "open"]
    return pw.listing(listing_id)["remote_check_status"], len(open_rows)


async def test_a_fact_toggled_twice_keeps_a_truthful_precheck(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    from .review_support import REVIEWER, item, only_item, post_approve

    made = await generated(prev.world)
    await _run_previews(prev, made.listing_id)
    seen: list[tuple[str, str, int, int]] = []

    async def visit(colour: str, refused: bool) -> None:
        prev.fake.preview_errors = [_colour_error()] if refused else []
        await _set_colour(api, prev, made.listing_id, colour)
        await _run_previews(prev, made.listing_id)
        check, open_rows = _state(prev, made.listing_id)
        seen.append((colour, check, open_rows, len(preview_jobs(prev, made.listing_id))))

    await visit("red", False)  # A: clean
    await visit("black", True)  # B: refused
    await visit("red", False)  # A again: D-23 closes B's row
    await visit("black", True)  # B again: the marketplace still refuses black
    check, open_rows = _state(prev, made.listing_id)
    if (check, open_rows) != ("issues", 1):
        early = await post_approve(api, prev.world, REVIEWER, [item(made)])
        raise AssertionError(
            f"B again: check={check} open={open_rows} approve={only_item(early)['http_status']}"
            f" seen={seen}"
        )
    await visit("red", False)  # A a third time
    check, open_rows = _state(prev, made.listing_id)
    approve = await post_approve(api, prev.world, REVIEWER, [item(made)])
    assert (check, open_rows, only_item(approve)["http_status"]) == ("clean", 0, 200), (
        seen,
        approve.text,
    )
