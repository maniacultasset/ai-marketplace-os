"""S23 T11 (AC5; C6): the staff cost page's day filter, per-model and per-trigger tables, top
listings and the per-listing page, rendered from `app/workers/rollup.py` (local-only, D3)."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Any

import pytest

from . import factories as f
from .admin_support import ADMIN, NOW, AdminClient, OpsWorld, external_refs
from .conftest import LOCAL_HOSTS
from .products_support import bearer_for

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
COST = f"{ADMIN}/ops/cost"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.admin.ops_views import OpsView

    monkeypatch.setattr(OpsView, "clock", staticmethod(lambda: NOW))


@pytest.fixture
def costs(ops_world: OpsWorld) -> dict[str, Any]:
    """On top of the S14b ops world (bharat 0.4 at 00:00Z on 5 Oct, model-x, generate): two acme
    listings with runs on 5 Oct, one a `correct` call on another model."""
    conn, acme = ops_world.conn, ops_world.world.tenant("acme")
    conn.execute("UPDATE tenants SET name = %s WHERE id = %s", ["<b>Acme</b> & co", acme])
    one, two = f.listing(conn, acme), f.listing(conn, acme)
    at = "2026-10-05T06:00:00Z"
    for listing, cost, model, trigger in (
        (one, "0.000001", "model-x", "generate"),
        (one, "0.000002", "model-y", "correct"),
        (two, "0.250000", "model-y", "generate"),
    ):
        f.agent_run(
            conn,
            acme,
            listing_id=listing,
            cost_usd=Decimal(cost),
            model_used=model,
            trigger=trigger,
            created_at=at,
            started_at=at,
        )
    return {"one": one, "two": two}


def _cells(html: str, table: str) -> list[list[str]]:
    body = html.split(f'data-ops="{table}"', 1)[1].split("</table>", 1)[0]
    rows = re.findall(r"<tr>(.*?)</tr>", body, flags=re.S)
    cells = [[c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", r, flags=re.S)] for r in rows]
    return [c for c in cells if c]


def _text(cell: str) -> str:
    return re.sub(r"<[^>]+>", "", cell).strip()


async def test_cost_page_golden_day(admin_client: AdminClient, costs: dict[str, Any]) -> None:
    async with admin_client() as client:
        response = await client.get(COST, params={"day": "2026-10-05"})
    assert response.status_code == 200, response.text
    html = response.text
    assert _cells(html, "by-model") == [
        ["model-x", "0.400001", "2"],
        ["model-y", "0.250002", "2"],
    ]
    assert _cells(html, "by-trigger") == [
        ["generate", "0.650001", "3"],
        ["correct", "0.000002", "1"],
    ]
    top = [[_text(c) for c in row] for row in _cells(html, "top-listings")]
    assert top == [
        [str(costs["two"]), "&lt;b&gt;Acme&lt;/b&gt; &amp; co", "0.250000", "1"],
        [str(costs["one"]), "&lt;b&gt;Acme&lt;/b&gt; &amp; co", "0.000003", "2"],
    ]


async def test_cost_page_default_today(admin_client: AdminClient, costs: dict[str, Any]) -> None:
    async with admin_client() as client:
        today = await client.get(COST)
        explicit = await client.get(COST, params={"day": NOW.date().isoformat()})
    assert today.status_code == explicit.status_code == 200
    assert _cells(today.text, "by-model") == _cells(explicit.text, "by-model")
    assert "Spend on 2026-10-05 (UTC)" in today.text


@pytest.mark.parametrize("day", ["2026-13-01", "abc", "", "1900-01-01", "2026-10-5", "20261005"])
async def test_cost_page_bad_day_400(
    admin_client: AdminClient, ops_world: OpsWorld, day: str
) -> None:
    async with admin_client() as client:
        response = await client.get(COST, params={"day": day})
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


async def test_cost_page_day_366_back_ok(admin_client: AdminClient, ops_world: OpsWorld) -> None:
    async with admin_client() as client:
        response = await client.get(COST, params={"day": "2025-10-04"})
        older = await client.get(COST, params={"day": "2025-10-03"})
    assert (response.status_code, older.status_code) == (200, 400)


async def test_cost_page_listing_golden(admin_client: AdminClient, costs: dict[str, Any]) -> None:
    async with admin_client() as client:
        response = await client.get(f"{COST}/listings/{costs['one']}")
    assert response.status_code == 200, response.text
    block = response.text.split('data-ops="listing-cost"', 1)[1].split("</dl>", 1)[0]
    assert "<dd>0.000003</dd>" in block and "<dd>2</dd>" in block
    assert "&lt;b&gt;Acme&lt;/b&gt; &amp; co" in block


async def test_cost_page_listing_unknown_404(
    admin_client: AdminClient, ops_world: OpsWorld
) -> None:
    async with admin_client() as client:
        response = await client.get(f"{COST}/listings/{uuid.uuid4()}")
    assert response.status_code == 404


@pytest.mark.parametrize("bad", ["not-a-uuid", "1234", "00000000-0000-0000-0000-00000000000g"])
async def test_cost_page_listing_bad_uuid_400(
    admin_client: AdminClient, ops_world: OpsWorld, bad: str
) -> None:
    async with admin_client() as client:
        response = await client.get(f"{COST}/listings/{bad}")
    assert response.status_code == 400


async def test_cost_page_staging_403_with_tenant_jwt(
    admin_client: AdminClient, costs: dict[str, Any], ops_world: OpsWorld
) -> None:
    headers = bearer_for(ops_world.world, "admin@acme.test")
    async with admin_client("staging") as client:
        page = await client.get(COST, headers=headers)
        listing = await client.get(f"{COST}/listings/{costs['one']}", headers=headers)
    assert (page.status_code, listing.status_code) == (403, 403)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
async def test_cost_page_write_405(
    admin_client: AdminClient, costs: dict[str, Any], method: str
) -> None:
    async with admin_client() as client:
        page = await client.request(method, COST)
        listing = await client.request(method, f"{COST}/listings/{costs['one']}")
    assert (page.status_code, listing.status_code) == (405, 405)


async def test_cost_page_no_store(admin_client: AdminClient, costs: dict[str, Any]) -> None:
    async with admin_client() as client:
        for path in (COST, f"{COST}/listings/{costs['one']}"):
            response = await client.get(path)
            assert response.headers["cache-control"] == "no-store"


async def test_cost_page_escapes_tenant_name(
    admin_client: AdminClient, costs: dict[str, Any]
) -> None:
    async with admin_client() as client:
        response = await client.get(COST)
    assert "<b>Acme</b>" not in response.text
    assert "&lt;b&gt;Acme&lt;/b&gt; &amp; co" in response.text


async def test_cost_page_no_external_assets(
    admin_client: AdminClient, costs: dict[str, Any]
) -> None:
    async with admin_client() as client:
        page = await client.get(COST)
        listing = await client.get(f"{COST}/listings/{costs['one']}")
    assert external_refs(page.text) == []
    assert external_refs(listing.text) == []
