"""Prod QA 2026-11-09 item 1: a listing whose stored verdict passed, while the marketplace check
with today's definition blocks it, read "Ready to approve" on the Overview card, the product
header and the Products list while Review showed blocking gates. Every read now carries one
server-computed `display_state`, and a stale reviewable listing is checked again on read."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from app.adapters.amazon import schema_check

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .ptd_support import store
from .review_support import OWNER, Made, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
UNFED: dict[str, Any] = {
    "type": "object",
    "required": ["unfed_x"],
    "properties": {"unfed_x": {"type": "array"}},
}


@pytest.fixture(autouse=True)
def _whole_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's database holds other tests' validated listings: check the whole queue."""
    from app.services import listing_display

    monkeypatch.setattr(listing_display, "LIST_RECHECK_CAP", 10_000)
    monkeypatch.setattr(listing_display, "QUEUE_SCAN", 200)


@pytest.fixture(autouse=True)
def _cold_cache() -> Iterator[None]:
    schema_check.clear_cache()
    yield
    schema_check.clear_cache()


async def _get(api: httpx.AsyncClient, world: GenWorld, path: str, **params: Any) -> Any:
    from .products_support import bearer_for

    got = await api.get(f"/api/v1{path}", headers=bearer_for(world, OWNER), params=params)
    assert got.status_code == 200, got.text
    return got.json()


def _open_errors(world: GenWorld, made: Made) -> int:
    return world.count(
        "listing_issues",
        "listing_id = %s AND status = 'open' AND severity = 'error'",
        [made.listing_id],
    )


def _product(world: GenWorld, made: Made) -> str:
    return str(world.listing(made.listing_id)["product_id"])


def _row(page: dict[str, Any], made: Made) -> dict[str, Any]:
    return next(r for r in page["data"] if r["id"] == str(made.listing_id))


async def test_a_clean_validated_listing_reads_ready_to_approve(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    body = await _get(api, gen_world, f"/listings/{made.listing_id}")
    assert (body["status"], body["display_state"], body["needs_you_count"]) == (
        "validated",
        "ready_to_approve",
        0,
    )


async def test_a_stale_verdict_is_rechecked_on_read_and_reads_needs_you(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    assert gen_world.listing(made.listing_id)["status"] == "validated"
    store(gen_world.conn, UNFED)  # a newer definition: the stored "passed" is now stale
    assert _open_errors(gen_world, made) == 0  # the defect: nothing re-asked

    body = await _get(api, gen_world, f"/listings/{made.listing_id}")
    assert body["display_state"] == "needs_you"
    assert body["needs_you_count"] == _open_errors(gen_world, made) == 1
    assert body["status"] == "blocked"  # the same move "Run checks" makes
    # every list read now agrees with the detail
    by_product = await _get(api, gen_world, "/listings", product_id=_product(gen_world, made))
    listed = await _get(api, gen_world, "/listings", limit=200)
    for page in (by_product, listed):
        row = _row(page, made)
        assert (row["display_state"], row["needs_you_count"]) == ("needs_you", 1)


async def test_the_product_list_read_rechecks_before_any_detail_read(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    store(gen_world.conn, UNFED)
    page = await _get(api, gen_world, "/listings", product_id=_product(gen_world, made))
    row = _row(page, made)
    assert (row["display_state"], row["needs_you_count"]) == ("needs_you", 1)


async def test_a_second_read_of_a_fresh_verdict_writes_nothing(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    store(gen_world.conn, UNFED)
    await _get(api, gen_world, f"/listings/{made.listing_id}")
    runs = gen_world.count("validation_runs", "listing_id = %s", [made.listing_id])
    events = gen_world.count("audit_log", "entity_id = %s", [made.listing_id])
    await _get(api, gen_world, f"/listings/{made.listing_id}")
    assert gen_world.count("validation_runs", "listing_id = %s", [made.listing_id]) == runs
    assert gen_world.count("audit_log", "entity_id = %s", [made.listing_id]) == events


@pytest.mark.parametrize("status", ["validated", "needs_review"])
async def test_open_blocking_issues_read_needs_you_whatever_the_status(
    api: httpx.AsyncClient, gen_world: GenWorld, status: str
) -> None:
    from .review_support import open_issue

    made = await generated(gen_world)
    set_status(gen_world, made.listing_id, status)
    await open_issue(gen_world, made, severity="error")
    page = await _get(api, gen_world, "/listings", limit=200)
    row = _row(page, made)
    assert (row["status"], row["display_state"], row["needs_you_count"]) == (
        status,
        "needs_you",
        1,
    )


def test_display_state_maps_every_status() -> None:
    from app.services.listing_display import display_state

    class Row:
        def __init__(self, status: str) -> None:
            self.status = status

    expected = {
        "created": "draft",
        "generating": "working",
        "validated": "ready_to_approve",
        "needs_review": "needs_review",
        "approved": "ready_to_list",
        "publishing": "submitting",
        "published": "published",
        "blocked": "blocked",
        "failed": "error",
    }
    assert {s: display_state(Row(s), 0) for s in expected} == expected
    assert {s for s in expected if display_state(Row(s), 2) == "needs_you"} == {
        "validated",
        "needs_review",
        "blocked",
    }


async def test_the_inbox_read_rechecks_the_review_queue_first(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """Item 9: the Inbox read "Nothing needs you" while a stale "passed" hid blocking issues."""
    made = await generated(gen_world)
    store(gen_world.conn, UNFED)
    page = await _get(api, gen_world, "/issues", status="open", limit=200)
    mine = [i for i in page["data"] if i["listing_id"] == str(made.listing_id)]
    assert [(i["severity"], i["source"]) for i in mine] == [("error", "schema")]
    assert gen_world.listing(made.listing_id)["status"] == "blocked"


async def test_the_tasks_read_rechecks_the_review_queue_first(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    store(gen_world.conn, UNFED)
    await _get(api, gen_world, "/tasks", status="open")
    assert _open_errors(gen_world, made) == 1


async def test_a_facts_save_is_checked_again_on_the_next_read(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """Item 26: after the seller saved facts, Review kept the old verdict until "Check again".
    The facts are a pin, so the next read of the listing validates the version again."""
    from .review_support import OPERATOR

    made = await generated(gen_world)
    url = f"/api/v1/products/{_product(gen_world, made)}"
    got = await api.get(url, headers=gen_world.headers(OPERATOR))
    assert got.status_code == 200, got.text
    runs = gen_world.count("validation_runs", "listing_id = %s", [made.listing_id])
    patched = await api.patch(
        url,
        headers={**gen_world.headers(OPERATOR), "If-Match": got.headers["ETag"]},
        json={"attributes": {"color": "red"}},
    )
    assert patched.status_code == 200, patched.text
    await _get(api, gen_world, f"/listings/{made.listing_id}")
    assert gen_world.count("validation_runs", "listing_id = %s", [made.listing_id]) == runs + 1
