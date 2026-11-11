"""Item 15 (client feedback 2026-11-11): an Amazon listing whose product has no packed box gets
two open warning issues from the marketplace check, and the approval still goes through (the
stored definitions do not require the package, so it never blocks).

The later steps: adding the package closes both warnings on the next check; a content edit keeps
them as warnings (never blocked, no question); a dismissed warning stays dismissed on a re-check
with the same inputs, since a re-check compares only the blocking items with the open rows
(`listing_validate.schema_changed`). So a listing checked before this rule shipped gets the
warnings on its next new version or facts change, not on a plain re-check."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import PRODUCTS, bearer_for
from .review_support import (
    OPERATOR,
    OWNER,
    REVIEWER,
    Made,
    decisions,
    edited,
    generated,
    item,
    post_approve,
    rows,
)

pytestmark = [pytest.mark.allow_hosts(LOCAL_HOSTS), pytest.mark.package_warnings]
__all__ = ["gen_world"]


async def test_a_missing_package_warns_and_never_blocks_the_approval(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    found = rows(
        gen_world,
        "SELECT field_path, error_code, severity, status FROM listing_issues "
        "WHERE listing_id = %s AND source = 'schema' ORDER BY field_path",
        [made.listing_id],
    )
    assert found == [
        ("facts.package_dimensions", "MISSING_ATTRIBUTE", "warning", "open"),
        ("facts.package_weight", "MISSING_ATTRIBUTE", "warning", "open"),
    ]
    assert gen_world.listing(made.listing_id)["status"] == "validated"
    response = await post_approve(api, gen_world, REVIEWER, [item(made)])
    assert response.status_code == 200, response.text
    (got,) = response.json()["items"]
    assert got["http_status"] == 200, got
    assert decisions(gen_world, made.listing_id) == 1


PACKAGE_ROWS = (
    "SELECT field_path, severity, status FROM listing_issues "
    "WHERE listing_id = %s AND source = 'schema' AND field_path LIKE 'facts.package_%%' "
    "ORDER BY field_path, created_at"
)


def _tasks(world: GenWorld, listing_id: uuid.UUID) -> int:
    return world.count(
        "human_tasks t JOIN listing_issues i ON i.human_task_id = t.id",
        "i.listing_id = %s",
        [listing_id],
    )


async def _validate(api: httpx.AsyncClient, world: GenWorld, made: Made) -> Any:
    response = await api.post(
        "/api/v1/listings/validate",
        headers=world.headers(OPERATOR),
        json={"listing_id": str(made.listing_id)},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _add_package(api: httpx.AsyncClient, world: GenWorld, made: Made) -> None:
    product_id = world.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id])
    got = await api.get(f"{PRODUCTS}/{product_id}", headers=bearer_for(world, OPERATOR))
    assert got.status_code == 200, got.text
    response = await api.patch(
        f"{PRODUCTS}/{product_id}",
        json={
            "package": {"length_cm": "32", "breadth_cm": "26", "height_cm": "4", "weight_g": 450}
        },
        headers={**bearer_for(world, OPERATOR), "If-Match": got.headers["ETag"]},
    )
    assert response.status_code == 200, response.text


async def test_adding_the_package_closes_both_warnings(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    await _add_package(api, gen_world, made)
    await _validate(api, gen_world, made)
    found = rows(gen_world, PACKAGE_ROWS, [made.listing_id])
    assert [(field, status) for field, _sev, status in found] == [
        ("facts.package_dimensions", "resolved"),
        ("facts.package_weight", "resolved"),
    ]
    assert gen_world.listing(made.listing_id)["status"] == "validated"
    assert _tasks(gen_world, made.listing_id) == 0


async def test_a_content_edit_keeps_them_warnings_and_approval_goes_through(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    after = await edited(api, gen_world, made, email=OWNER)
    found = rows(gen_world, PACKAGE_ROWS, [made.listing_id])
    assert found == [
        ("facts.package_dimensions", "warning", "open"),
        ("facts.package_weight", "warning", "open"),
    ]
    assert gen_world.listing(made.listing_id)["status"] != "blocked"
    assert _tasks(gen_world, made.listing_id) == 0
    response = await post_approve(api, gen_world, REVIEWER, [item(after)])
    assert response.status_code == 200, response.text
    (got,) = response.json()["items"]
    assert got["http_status"] == 200, got


async def test_a_dismissed_warning_stays_dismissed_on_a_recheck(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    issue = gen_world.scalar(
        "SELECT id FROM listing_issues WHERE listing_id = %s AND source = 'schema' "
        "AND field_path = 'facts.package_weight' AND status = 'open'",
        [made.listing_id],
    )
    headers = {**bearer_for(gen_world, REVIEWER), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"/api/v1/issues/{issue}/dismiss", headers=headers)
    assert response.status_code == 200, response.text
    await _validate(api, gen_world, made)
    listed = await api.get(
        f"/api/v1/listings/{made.listing_id}", headers=bearer_for(gen_world, OPERATOR)
    )
    assert listed.status_code == 200, listed.text
    found = rows(gen_world, PACKAGE_ROWS, [made.listing_id])
    assert found == [
        ("facts.package_dimensions", "warning", "open"),
        ("facts.package_weight", "warning", "dismissed"),
    ]
