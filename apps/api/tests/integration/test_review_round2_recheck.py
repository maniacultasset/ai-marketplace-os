"""S19 fix round 2 re-check (tester): the CP12 feedback check on every approve item (R1-1) and
the content PATCH's nested unknown-key 422 (R1-2, PLAN AC 8, invariant 4), attacked with
variants the fix tests do not cover: a mixed batch (nothing written), encoded secret-shaped JSON
on a plain reject, benign JSON feedback that must still pass and still regenerate, and key-name
variants inside `content`/`keywords`."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bearer_for
from .review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    Made,
    generated,
    item,
    only_item,
    post_approve,
    run_job,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]

CONTENT = {
    "title": "Cotton Kurta Set for Women",
    "bullets": ["Soft cotton fabric"],
    "description": "A cotton kurta set with a straight fit.",
}
_SECRET_OBJ = {"nested": [{"client_secret": "not-a-real-secret-000"}]}


def _reject(made: Made, feedback: str, **extra: Any) -> dict[str, Any]:
    return item(made, "reject", reason_code="poor_quality", feedback=feedback, **extra)


# ---- R1-1: CP12 on every item's feedback ----


@pytest.mark.parametrize(
    "feedback",
    [
        "﻿" + json.dumps(_SECRET_OBJ),
        json.dumps(json.dumps(_SECRET_OBJ)),
        "see: " + json.dumps([_SECRET_OBJ]),
    ],
    ids=["bom_prefixed", "double_encoded", "array_after_prefix"],
)
async def test_plain_reject_encoded_secret_feedback_422(
    api: httpx.AsyncClient, gen_world: GenWorld, feedback: str
) -> None:
    made = await generated(gen_world)
    response = await post_approve(api, gen_world, REVIEWER, [_reject(made, feedback)])
    assert response.status_code == 422, response.text
    assert "not-a-real-secret" not in response.text
    assert gen_world.count("review_decisions", "listing_id = %s", [made.listing_id]) == 0


async def test_mixed_batch_secret_feedback_writes_nothing(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """A clean approve in the same batch as a secret-shaped plain reject: the whole request is a
    422 before any write, so the clean item is not approved either."""
    clean = await generated(gen_world, OPERATOR)
    dirty = await generated(gen_world, OPERATOR)
    response = await post_approve(
        api,
        gen_world,
        REVIEWER,
        [item(clean), _reject(dirty, json.dumps(_SECRET_OBJ))],
    )
    assert response.status_code == 422, response.text
    locs = [d["loc"] for d in response.json()["error"]["details"]]
    assert locs == [["body", "items", 1, "feedback"]], locs
    assert gen_world.count("batches") == 0
    for made in (clean, dirty):
        assert gen_world.count("review_decisions", "listing_id = %s", [made.listing_id]) == 0
        assert gen_world.listing(made.listing_id)["status"] == "validated"


async def test_plain_reject_benign_json_feedback_then_regenerates(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """Benign JSON in a plain reject's feedback is not over-refused, and the stored feedback does
    not lock the listing out of a later regeneration (the context builds and the job succeeds)."""
    made = await generated(gen_world, OPERATOR)
    feedback = 'Use {"fit": "straight", "sizes": ["S", "M"]} in the bullets'
    result = only_item(await post_approve(api, gen_world, REVIEWER, [_reject(made, feedback)]))
    assert result["http_status"] == 200, result
    headers = {**bearer_for(gen_world, OWNER), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(
        f"/api/v1/listings/{made.listing_id}/regenerate", headers=headers, json=None
    )
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    row = gen_world.listing(made.listing_id)
    assert row["status"] == "validated", row
    assert row["current_version_id"] != made.version_id


# ---- R1-2: nested unknown key in a content PATCH is a 422 ----


async def _patch(api: httpx.AsyncClient, world: GenWorld, made: Made, body: Any) -> httpx.Response:
    headers = {**bearer_for(world, OWNER), "If-Match": f'"{made.version_id}"'}
    return await api.patch(
        f"/api/v1/listings/{made.listing_id}/content", headers=headers, json=body
    )


@pytest.mark.parametrize(
    "body",
    [
        {"content": {**CONTENT, "Title": "x"}},
        {"content": {**CONTENT, " title": "x"}},
        {"content": {**CONTENT, "amazon": {"item_name": "x"}}},
        {"content": CONTENT, "keywords": {"search_terms": ["kurta"], "generic_keyword": "x"}},
        {"content": {**CONTENT, "x": None}},
    ],
    ids=["case_variant", "space_variant", "nested_marketplace_block", "keywords_amazon", "null"],
)
async def test_patch_unknown_key_variants_422(
    api: httpx.AsyncClient, gen_world: GenWorld, body: dict[str, Any]
) -> None:
    made = await generated(gen_world)
    response = await _patch(api, gen_world, made, body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert gen_world.count("listing_versions", "listing_id = %s", [made.listing_id]) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"content": CONTENT, "keywords": None},
        {"content": CONTENT, "keywords": {}},
        {"content": CONTENT, "keywords": {"search_terms": ["kurta"], "hidden_terms": []}},
    ],
    ids=["keywords_null", "keywords_empty", "keywords_known"],
)
async def test_patch_known_keys_still_200(
    api: httpx.AsyncClient, gen_world: GenWorld, body: dict[str, Any]
) -> None:
    """The new before-validator does not over-refuse a body with only known keys."""
    made = await generated(gen_world)
    response = await _patch(api, gen_world, made, body)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "body", [{"content": ["title"]}, {"content": CONTENT, "keywords": ["x"]}, ["content"]]
)
async def test_patch_non_dict_shapes_422_not_500(
    api: httpx.AsyncClient, gen_world: GenWorld, body: Any
) -> None:
    made = await generated(gen_world)
    response = await _patch(api, gen_world, made, body)
    assert response.status_code == 422, response.text
