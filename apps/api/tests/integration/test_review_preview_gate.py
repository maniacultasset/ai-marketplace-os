"""SI11 T8 (AC2; P:A3, D6): approve refuses an item while the marketplace's dry run has an open
ERROR on its current version; a WARNING, an older version's row, or a pending or failed check
never refuses."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import REVIEWER, Made, generated, item, post_approve

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


def _preview_row(
    world: GenWorld, made: Made, *, severity: str = "error", version: Any = None
) -> None:
    f.listing_issue(
        world.conn,
        world.tenant("acme"),
        made.listing_id,
        source="amazon_adapter",
        field_path="content.title",
        error_code="VALUE_NOT_ALLOWED",
        error_class="invalid_value",
        retry_class="user_action",
        severity=severity,
        message="Amazon refused the title.",
        marketplace_code="99301",
        listing_version_id=version or made.version_id,
        dedupe_key=f"amazon_adapter|content.title|VALUE_NOT_ALLOWED|99301|{uuid.uuid4()}|preview",
    )


def _codes(response: httpx.Response) -> list[tuple[int, str | None]]:
    out = []
    for row in response.json()["items"]:
        error = row.get("error") or {}
        out.append((row["http_status"], error.get("error_code")))
    return out


async def test_open_preview_error_refuses(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    _preview_row(gen_world, made)
    response = await post_approve(api, gen_world, REVIEWER, [item(made)])
    assert _codes(response) == [(422, "VALIDATION_FAILED")]
    error = response.json()["items"][0]["error"]
    assert [e["field"] for e in error["errors"]] == ["content.title"]
    assert error["errors"][0]["message"] == "Amazon refused the title."
    assert gen_world.listing(made.listing_id)["status"] == "validated"


async def test_warning_row_passes(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    _preview_row(gen_world, made, severity="warning")
    response = await post_approve(api, gen_world, REVIEWER, [item(made)])
    assert _codes(response) == [(200, None)]


async def test_older_version_row_passes(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    older = f.version(
        gen_world.conn,
        gen_world.tenant("acme"),
        made.listing_id,
        version=99,
        generated_by_user_id=gen_world.user("operator@acme.test"),
    )
    _preview_row(gen_world, made, version=older)
    response = await post_approve(api, gen_world, REVIEWER, [item(made)])
    assert _codes(response) == [(200, None)]


@pytest.mark.parametrize("status", ["pending", "failed"])
async def test_pending_or_failed_check_passes(
    api: httpx.AsyncClient, gen_world: GenWorld, status: str
) -> None:
    made = await generated(gen_world)
    gen_world.conn.execute(
        "UPDATE listings SET remote_check_version_id = %s, remote_check_status = %s, "
        "remote_check_at = CASE WHEN %s = 'pending' THEN NULL ELSE now() END WHERE id = %s",
        [made.version_id, status, status, made.listing_id],
    )
    response = await post_approve(api, gen_world, REVIEWER, [item(made)])
    assert _codes(response) == [(200, None)]


async def test_a_batch_approves_the_other_item(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    blocked, ok = await generated(gen_world), await generated(gen_world)
    _preview_row(gen_world, blocked)
    response = await post_approve(api, gen_world, REVIEWER, [item(blocked), item(ok)])
    assert _codes(response) == [(422, "VALIDATION_FAILED"), (200, None)]
    assert gen_world.listing(ok.listing_id)["status"] == "approved"
