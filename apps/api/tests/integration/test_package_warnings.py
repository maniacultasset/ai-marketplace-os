"""Item 15 (client feedback 2026-11-11): an Amazon listing whose product has no packed box gets
two open warning issues from the marketplace check, and the approval still goes through (the
stored definitions do not require the package, so it never blocks)."""

from __future__ import annotations

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import REVIEWER, decisions, generated, item, post_approve, rows

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
