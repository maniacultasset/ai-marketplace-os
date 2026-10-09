"""S18 fix round (tester repro, PLAN.md:165): a content PATCH and a regenerate write their
`audit_log` rows (S19 reads them); free text with NUL or another C0 control character (all but
`\\n` and `\\t`) is a 422 with nothing written, never a 500 or a stored control character."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .test_listings_content_patch_api import ATTRS, CLEAN, _patch, _seeded

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
BAD = ["a\x00b", "a\x07b", "a\x1bb", "a\x7fb", "a\rb"]


def _audit(world: GenWorld, listing_id: Any, action: str) -> list[dict[str, Any]]:
    """`{before, after, actor}` per row (the database keeps the listing's allow-listed keys)."""
    rows = world.conn.execute(
        "SELECT before, after, actor_user_id FROM audit_log WHERE entity_id = %s AND action = %s",
        [listing_id, action],
    ).fetchall()

    def load(v: Any) -> Any:
        return v if v is None or isinstance(v, dict) else json.loads(v)

    return [{"before": load(b), "after": load(a), "actor": u} for b, a, u in rows]


async def test_patch_writes_listing_edited_audit(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    listing_id, parent = _seeded(gen_world)
    response = await _patch(api, gen_world, listing_id, parent)
    assert response.status_code == 200, response.text
    [row] = _audit(gen_world, listing_id, "listing.edited")
    assert row["before"] == {"status": "validated"}
    assert row["after"]["status"] == "validated"
    assert row["actor"] == gen_world.user("operator@acme.test")


async def test_patch_rejected_writes_no_audit(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, parent = _seeded(gen_world)
    first = await _patch(api, gen_world, listing_id, parent)
    assert first.status_code == 200, first.text
    stale = await _patch(
        api, gen_world, listing_id, parent, {"content": {**CLEAN, "title": "Another title"}}
    )
    assert stale.status_code == 409, stale.text
    assert len(_audit(gen_world, listing_id, "listing.edited")) == 1


def _bodies(bad: str) -> dict[str, dict[str, Any]]:
    return {
        "title": {"content": {**CLEAN, "title": bad}},
        "bullet": {"content": {**CLEAN, "bullets": [bad]}},
        "description": {"content": {**CLEAN, "description": bad}},
        "search_terms": {"content": CLEAN, "keywords": {"search_terms": [bad]}},
        "hidden_terms": {"content": CLEAN, "keywords": {"hidden_terms": [bad]}},
        "attr_value": {"content": CLEAN, "attributes": {**ATTRS, "color": bad}},
        "attr_list": {"content": CLEAN, "attributes": {**ATTRS, "sizes": ["s", bad]}},
    }


@pytest.mark.parametrize("where", list(_bodies("x")))
@pytest.mark.parametrize("bad", BAD, ids=repr)
async def test_patch_control_chars_422(
    api: httpx.AsyncClient, gen_world: GenWorld, bad: str, where: str
) -> None:
    listing_id, version = _seeded(gen_world)
    response = await _patch(api, gen_world, listing_id, version, _bodies(bad)[where])
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert gen_world.count("listing_versions", "listing_id = %s", [listing_id]) == 1


async def test_patch_newline_and_tab_pass(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, version = _seeded(gen_world)
    body = {"content": {**CLEAN, "description": "line one\nline\ttwo"}, "attributes": ATTRS}
    response = await _patch(api, gen_world, listing_id, version, body)
    assert response.status_code == 200, response.text
    assert response.json()["content"]["content"]["description"] == "line one\nline\ttwo"


async def _regenerate(
    api: httpx.AsyncClient, world: GenWorld, listing_id: Any, body: dict[str, Any]
) -> httpx.Response:
    return await api.post(
        f"/api/v1/listings/{listing_id}/regenerate",
        headers=world.headers("operator@acme.test"),
        json=body,
    )


@pytest.mark.parametrize("bad", BAD, ids=repr)
async def test_regenerate_instructions_control_chars_422(
    api: httpx.AsyncClient, gen_world: GenWorld, bad: str
) -> None:
    listing_id, _version = _seeded(gen_world)
    response = await _regenerate(api, gen_world, listing_id, {"instructions": bad})
    assert response.status_code == 422, response.text
    assert gen_world.listing(listing_id)["status"] == "validated"
    assert _audit(gen_world, listing_id, "listing.regenerate_requested") == []


async def test_regenerate_writes_audit(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    listing_id, _version = _seeded(gen_world)
    response = await _regenerate(api, gen_world, listing_id, {"instructions": "Shorter title"})
    assert response.status_code == 202, response.text
    [row] = _audit(gen_world, listing_id, "listing.regenerate_requested")
    assert row["before"] == {"status": "validated"}
    assert row["after"] == {"status": "generating"}
    assert row["actor"] == gen_world.user("operator@acme.test")
    busy = await _regenerate(api, gen_world, listing_id, {})
    assert busy.status_code == 409, busy.text
    assert len(_audit(gen_world, listing_id, "listing.regenerate_requested")) == 1
