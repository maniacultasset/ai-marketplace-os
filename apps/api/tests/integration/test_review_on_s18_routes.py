"""S19 T14 (AC 8, 9; CP-D1): the review side of S18's `PATCH /listings/{id}/content` and
`POST /listings/{id}/regenerate`: an edit or a regeneration voids the approval, the editor or
regenerator becomes a maker who cannot approve, paused and archived listings are refused, both
are audited without their text, and free text with control characters never reaches SQL."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bearer_for, statements
from .review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    Made,
    audits,
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


async def _patch(
    api: httpx.AsyncClient, world: GenWorld, made: Made, body: Any = None, email: str = OWNER
) -> httpx.Response:
    headers = {**bearer_for(world, email), "If-Match": f'"{made.version_id}"'}
    return await api.patch(
        f"/api/v1/listings/{made.listing_id}/content",
        headers=headers,
        json=body if body is not None else {"content": CONTENT},
    )


async def _regenerate(
    api: httpx.AsyncClient, world: GenWorld, made: Made, email: str = REVIEWER, body: Any = None
) -> httpx.Response:
    headers = {**bearer_for(world, email), "Idempotency-Key": str(uuid.uuid4())}
    return await api.post(
        f"/api/v1/listings/{made.listing_id}/regenerate", headers=headers, json=body
    )


def _pause(world: GenWorld, made: Made, column: str) -> None:
    with world.conn.transaction():
        world.conn.execute("SET LOCAL session_replication_role = replica")
        world.conn.execute(
            f"UPDATE listings SET {column} = now() WHERE id = %s",  # noqa: S608
            [made.listing_id],
        )


async def _approved(api: httpx.AsyncClient, world: GenWorld) -> Made:
    made = await generated(world, OPERATOR)
    assert only_item(await post_approve(api, world, REVIEWER, [item(made)]))["http_status"] == 200
    return made


# ---- PATCH ----


async def test_review_on_s18_patch_with_etag_200(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    detail = await api.get(
        f"/api/v1/listings/{made.listing_id}", headers=bearer_for(gen_world, OWNER)
    )
    headers = {**bearer_for(gen_world, OWNER), "If-Match": detail.headers["ETag"]}
    response = await api.patch(
        f"/api/v1/listings/{made.listing_id}/content", headers=headers, json={"content": CONTENT}
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("column", "code"), [("paused_at", "LISTING_PAUSED"), ("archived_at", "LISTING_ARCHIVED")]
)
async def test_review_on_s18_patch_paused_archived_409(
    api: httpx.AsyncClient, gen_world: GenWorld, column: str, code: str
) -> None:
    made = await generated(gen_world)
    _pause(gen_world, made, column)
    response = await _patch(api, gen_world, made)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == code


async def test_review_on_s18_patch_marketplace_key_never_stored(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """Invariant 4: a marketplace field inside `content` never reaches the canonical listing
    (S18 drops nested unknown keys; `test_patch_nested_unknown_key_not_stored`)."""
    made = await generated(gen_world)
    body = {"content": {**CONTENT, "bullet_points": ["x"], "item_name": "y"}}
    response = await _patch(api, gen_world, made, body)
    assert response.status_code == 200, response.text
    stored = gen_world.scalar(
        "SELECT content_json FROM listing_versions WHERE id = %s",
        [response.json()["current_version_id"]],
    )
    assert "bullet_points" not in str(stored) and "item_name" not in str(stored)


@pytest.mark.parametrize(
    "body",
    [{"content": CONTENT, "marketplace": "amazon"}, {"content": CONTENT, "browse_node": "123"}],
    ids=["marketplace_top_level", "unknown_key"],
)
async def test_review_on_s18_patch_marketplace_or_unknown_key_422(
    api: httpx.AsyncClient, gen_world: GenWorld, body: dict[str, Any]
) -> None:
    made = await generated(gen_world)
    response = await _patch(api, gen_world, made, body)
    assert response.status_code == 422


async def test_review_on_s18_patch_voids_approval_editor_cannot_approve(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await _approved(api, gen_world)
    response = await _patch(api, gen_world, made)
    assert response.status_code == 200, response.text
    row = gen_world.listing(made.listing_id)
    assert row["approved_version_id"] is None and row["status"] == "validated"
    new = Made(made.listing_id, row["current_version_id"], made.job_id)
    refused = only_item(await post_approve(api, gen_world, OWNER, [item(new)]))
    assert refused["error"]["error_code"] == "MAKER_CHECKER_VIOLATION"
    assert (
        only_item(await post_approve(api, gen_world, REVIEWER, [item(new)]))["http_status"] == 200
    )


async def test_review_on_s18_patch_writes_no_agent_runs(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    before = gen_world.count("agent_runs")
    assert (await _patch(api, gen_world, made)).status_code == 200
    assert gen_world.count("agent_runs") == before


async def test_review_on_s18_patch_writes_audit_edited(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    first = await _patch(api, gen_world, made)
    assert first.status_code == 200
    assert audits(gen_world, made.listing_id, "listing.edited") == 1
    row = gen_world.conn.execute(
        "SELECT before, after FROM audit_log WHERE entity_id = %s AND action = 'listing.edited'",
        [made.listing_id],
    ).fetchone()
    assert row is not None and "Cotton Kurta Set" not in str(row)
    current = Made(made.listing_id, uuid.UUID(first.json()["current_version_id"]), made.job_id)
    assert (await _patch(api, gen_world, current)).status_code == 200  # a no-op: no new row
    assert audits(gen_world, made.listing_id, "listing.edited") == 1


@pytest.mark.parametrize("where", ["content.title", "keyword"])
async def test_review_on_s18_patch_nul_422(
    api: httpx.AsyncClient, gen_world: GenWorld, where: str
) -> None:
    made = await generated(gen_world)
    if where == "content.title":
        body: dict[str, Any] = {"content": {**CONTENT, "title": "Kurta\u0000Set"}}
    else:
        body = {"content": CONTENT, "keywords": {"search_terms": ["kurta\u0000"]}}
    with statements() as seen:
        response = await _patch(api, gen_world, made, body)
    assert response.status_code == 422
    writes = ("INSERT INTO LISTING_VERSIONS", "INSERT INTO VALIDATION_RUNS", "UPDATE LISTINGS")
    assert not [s for s in seen if s.lstrip().upper().startswith(writes)]


# ---- regenerate ----


async def test_review_on_s18_regenerate_voids_then_regenerator_approve_403(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await _approved(api, gen_world)
    response = await _regenerate(api, gen_world, made, REVIEWER)
    assert response.status_code == 202, response.text
    assert gen_world.listing(made.listing_id)["approved_version_id"] is None
    await run_job(uuid.UUID(response.json()["job_id"]))
    row = gen_world.listing(made.listing_id)
    new = Made(made.listing_id, row["current_version_id"], made.job_id)
    refused = only_item(await post_approve(api, gen_world, REVIEWER, [item(new)]))
    assert refused["error"]["error_code"] == "MAKER_CHECKER_VIOLATION"


async def test_review_on_s18_regenerate_second_reviewer_or_owner_approves(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, OPERATOR)
    response = await _regenerate(api, gen_world, made, REVIEWER)
    await run_job(uuid.UUID(response.json()["job_id"]))
    new = Made(
        made.listing_id, gen_world.listing(made.listing_id)["current_version_id"], made.job_id
    )
    assert only_item(await post_approve(api, gen_world, OWNER, [item(new)]))["http_status"] == 200


@pytest.mark.parametrize(
    ("column", "code"), [("paused_at", "LISTING_PAUSED"), ("archived_at", "LISTING_ARCHIVED")]
)
async def test_review_on_s18_regenerate_paused_archived_409(
    api: httpx.AsyncClient, gen_world: GenWorld, column: str, code: str
) -> None:
    made = await generated(gen_world)
    _pause(gen_world, made, column)
    response = await _regenerate(api, gen_world, made)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == code


async def test_review_on_s18_regenerate_admin_403(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    response = await _regenerate(api, gen_world, made, "admin@acme.test")
    assert response.status_code == 403


async def test_review_on_s18_regenerate_writes_audit_regenerate_requested(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    text = "Shorter title, please, and mention the fabric."
    response = await _regenerate(api, gen_world, made, body={"instructions": text})
    assert response.status_code == 202
    assert audits(gen_world, made.listing_id, "listing.regenerate_requested") == 1
    rows = gen_world.conn.execute(
        "SELECT before, after, reason FROM audit_log WHERE entity_id = %s", [made.listing_id]
    ).fetchall()
    assert text not in str(rows)


@pytest.mark.parametrize("bad", ["\x00", "\x07", "\x1b"])
async def test_review_on_s18_regenerate_instructions_c0_422(
    api: httpx.AsyncClient, gen_world: GenWorld, bad: str
) -> None:
    made = await generated(gen_world)
    jobs = gen_world.count("job_runs", "listing_id = %s", [made.listing_id])
    response = await _regenerate(api, gen_world, made, body={"instructions": f"short{bad}er"})
    assert response.status_code == 422
    assert gen_world.count("job_runs", "listing_id = %s", [made.listing_id]) == jobs
    assert gen_world.count("review_decisions", "listing_id = %s", [made.listing_id]) == 0


async def test_review_on_s18_regenerate_instructions_newline_tab_ok(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    body = {"instructions": "Shorter title.\n\tMention the fabric."}
    assert (await _regenerate(api, gen_world, made, body=body)).status_code == 202
