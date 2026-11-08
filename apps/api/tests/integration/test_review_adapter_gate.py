"""SI9 T9 (AC10): the approve gate re-runs the marketplace check on the version, so a stale
verdict never approves what the adapter would refuse (422 `VALIDATION_FAILED`, per item)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from app.adapters.amazon import schema_check

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .ptd_support import store
from .review_support import OWNER, REVIEWER, Made, decisions, generated, item, post_approve

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
# SI2: Amazon `fabric_type` is now fed by the seller's `fiber_composition` fact, so the stale
# verdict drops an AI-written attribute instead: `fit` feeds `fit_type`
NEEDS_FIT: dict[str, Any] = {
    "type": "object",
    "required": ["fit_type"],
    "properties": {"fit_type": {"type": "array"}},
}
UNFED: dict[str, Any] = {
    "type": "object",
    "required": ["unfed_x"],
    "properties": {"unfed_x": {"type": "array"}},
}


@pytest.fixture(autouse=True)
def _cold_cache() -> Iterator[None]:
    schema_check.clear_cache()
    yield
    schema_check.clear_cache()


def _with_fit(world: GenWorld, made: Made) -> None:
    """The stub writes only the seller's facts, so set-up gives the version an AI-written `fit`."""
    with world.conn.transaction():
        world.conn.execute("SET LOCAL session_replication_role = replica")
        world.conn.execute(
            "UPDATE listing_versions SET content_json = jsonb_set(content_json, "
            "'{attributes,fit}', '\"Regular\"') WHERE id = %s",
            [made.version_id],
        )


def _drop_fit(world: GenWorld, made: Made) -> None:
    """The version loses `fit` after its passed run (a stale verdict; set-up only)."""
    with world.conn.transaction():
        world.conn.execute("SET LOCAL session_replication_role = replica")
        world.conn.execute(
            "UPDATE listing_versions SET content_json = jsonb_set(content_json, '{attributes}', "
            "(content_json->'attributes') - 'fit') WHERE id = %s",
            [made.version_id],
        )


def _items(response: httpx.Response) -> dict[str, dict[str, Any]]:
    assert response.status_code == 200, response.text
    return {i["listing_id"]: i for i in response.json()["items"]}


@pytest.mark.parametrize("status", ["validated", "needs_review"])
async def test_blocking_check_refuses_the_approve(
    api: httpx.AsyncClient, gen_world: GenWorld, status: str
) -> None:
    made = await generated(gen_world)
    set_status(gen_world, made.listing_id, status)
    store(gen_world.conn, UNFED)
    got = _items(await post_approve(api, gen_world, REVIEWER, [item(made)]))[str(made.listing_id)]
    assert got["http_status"] == 422
    error = got["error"]
    assert error["error_code"] == "VALIDATION_FAILED"
    assert error["listing_id"] == str(made.listing_id) and error["request_id"]
    assert [(e["field"], e["error_code"], e["source"]) for e in error["errors"]] == [
        ("listing", "MISSING_ATTRIBUTE", "schema")
    ]
    assert decisions(gen_world, made.listing_id) == 0
    row = gen_world.listing(made.listing_id)
    assert (row["status"], row["approved_version_id"]) == (status, None)


async def test_batch_only_the_failing_item_is_refused(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    clean = await generated(gen_world)
    stale = await generated(gen_world)
    store(gen_world.conn, NEEDS_FIT)
    _with_fit(gen_world, clean)
    _drop_fit(gen_world, stale)
    items = [item(clean), item(stale)]
    key = str(uuid.uuid4())
    first = await post_approve(api, gen_world, REVIEWER, items, key=key)
    got = _items(first)
    assert got[str(clean.listing_id)]["http_status"] == 200
    assert got[str(stale.listing_id)]["http_status"] == 422
    assert decisions(gen_world, clean.listing_id) == 1
    assert decisions(gen_world, stale.listing_id) == 0
    replay = await post_approve(api, gen_world, REVIEWER, items, key=key)
    assert replay.status_code == 200 and replay.json() == first.json()
    other = await post_approve(api, gen_world, REVIEWER, items[:1], key=key)
    assert other.status_code == 409


async def test_gate_order_adapter_before_maker_checker(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """The owner generated both (the maker), so a clean check still ends in maker-checker."""
    clean = await generated(gen_world, OWNER)
    stale = await generated(gen_world, OWNER)
    store(gen_world.conn, NEEDS_FIT)
    _with_fit(gen_world, clean)
    _drop_fit(gen_world, stale)
    got = _items(await post_approve(api, gen_world, OWNER, [item(clean)]))
    assert got[str(clean.listing_id)]["http_status"] == 403
    assert got[str(clean.listing_id)]["error"]["error_code"] == "MAKER_CHECKER_VIOLATION"
    got = _items(await post_approve(api, gen_world, OWNER, [item(stale)]))
    assert got[str(stale.listing_id)]["http_status"] == 422


async def test_other_tenants_listing_is_404_and_never_checked(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import review

    theirs = await generated(gen_world, "operator@bharat.test")
    calls: list[uuid.UUID] = []
    real = review.adapter_check  # type: ignore[attr-defined]

    async def spy(uow: Any, listing_id: uuid.UUID, **kw: Any) -> Any:
        calls.append(listing_id)
        return await real(uow, listing_id, **kw)

    monkeypatch.setattr(review, "adapter_check", spy)
    got = _items(await post_approve(api, gen_world, REVIEWER, [item(theirs)]))
    assert got[str(theirs.listing_id)]["http_status"] == 404
    assert calls == []
