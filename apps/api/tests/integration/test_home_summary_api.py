"""FB6 (client feedback 11 Nov 2026, item 6): `GET /home/summary` over HTTP. The counts change by
exactly the scenario's rows and reconcile with the listing rows, the caller's work follows the
role and the publish switch, and another tenant's rows never show."""

from __future__ import annotations

import types
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .home_support import BUCKET_DELTA, PLAIN, PRODUCTS_DELTA, scenario
from .jobs_support import bad_token

__all__ = ["gen_world"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
HOME = "/api/v1/home/summary"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Amazon publishing off (as in production), and no re-check on read: the scenario's
    listings keep the states it wrote (the re-check has its own tests)."""
    from app.core.config import get_settings
    from app.services import listing_display

    async def no_recheck(*_: Any) -> None:
        return None

    monkeypatch.setenv("AMAZON_PUBLISH_ENABLED", "false")
    monkeypatch.setattr(listing_display, "recheck_queue", no_recheck)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _hdrs(world: GenWorld, email: str) -> dict[str, str]:
    out = world.headers(email)
    out.pop("Idempotency-Key", None)
    return out


async def _get(api: httpx.AsyncClient, world: GenWorld, email: str) -> dict[str, Any]:
    response = await api.get(HOME, headers=_hdrs(world, email))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _delta(after: dict[str, Any], before: dict[str, Any]) -> dict[str, int]:
    return {k: after[k] - before[k] for k in before if isinstance(before[k], int)}


def _listing_rows(world: GenWorld, slug: str) -> int:
    return world.count("listings", "tenant_id = %s AND archived_at IS NULL", [world.tenant(slug)])


@pytest.mark.endpoint("home_summary", case="success")
async def test_counts_reconcile_with_listing_rows(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    before = await _get(api, gen_world, "owner@bharat.test")
    s = scenario(gen_world, "bharat")
    after = await _get(api, gen_world, "owner@bharat.test")

    listings = after["listings"]
    assert listings["total"] == _listing_rows(gen_world, "bharat")
    buckets = {k: v for k, v in listings.items() if k not in ("total", "live_products")}
    assert sum(buckets.values()) == listings["total"]
    assert _delta(listings, before["listings"]) == {**BUCKET_DELTA, "live_products": 2}
    per = after["by_marketplace"]
    assert sum(m["listings"] for m in per) == listings["total"]
    assert sum(m["problem"] for m in per) == listings["problem"]
    assert after["products"] - before["products"] == PRODUCTS_DELTA

    needs = _delta(after["needs_you"], before["needs_you"])
    assert needs == {
        "total": 6,  # 2 blocked + 1 question + 2 to approve + 1 Flipkart to publish
        "blocked": 2,
        "questions": 1,
        "ready_to_approve": 2,
        "ready_to_publish": 1,
        "publishing_paused": 1,  # the approved Amazon listing
        "improve": 1,
    }

    # the newest blocked listing leads, with its issue in the seller's words
    first = after["needs_you_items"][0]
    assert first["kind"] == "blocked" and first["action"] == "fix"
    assert first["listing_id"] == str(s.blocked) and first["product_id"] == str(s.blocked_product)
    assert first["issue_id"] == str(s.blocked_issue) and first["issue"] == PLAIN
    assert first["issue_count"] == 1 and first["product_sku"] and first["product_title"]
    failed = next(i for i in after["needs_you_items"] if i["listing_id"] == str(s.failed))
    assert failed["issue"] == "Writing the listing failed." and failed["issue_id"] is None
    # the listing that waits on an answer is the question's, never a second blocked item
    assert all(i["listing_id"] != str(s.question_listing) for i in after["needs_you_items"])

    newest = after["activity"][0]
    assert newest["kind"] == "fix" and newest["state"] == "completed"
    assert newest["text"] == f"Fixed the title of the Amazon listing for {s.fixed_title}"

    amazon = next(m for m in after["marketplaces"] if m["marketplace"] == "amazon")
    assert amazon["publishing_enabled"] is False and amazon["enabled"] is True


@pytest.mark.parametrize(
    ("email", "approve", "publish", "paused"),
    [
        ("reviewer@bharat.test", 2, 0, 1),  # approves, never publishes
        ("operator@bharat.test", 0, 1, 1),  # publishes, never approves
        ("admin@bharat.test", 0, 0, 0),  # does no listing work
    ],
)
async def test_needs_you_follows_the_role(
    api: httpx.AsyncClient,
    gen_world: GenWorld,
    email: str,
    approve: int,
    publish: int,
    paused: int,
) -> None:
    before = await _get(api, gen_world, email)
    scenario(gen_world, "bharat")
    after = await _get(api, gen_world, email)
    needs = _delta(after["needs_you"], before["needs_you"])
    assert needs["ready_to_approve"] == approve and needs["ready_to_publish"] == publish
    assert needs["publishing_paused"] == paused
    assert needs["total"] == 2 + 1 + approve + publish
    # the listing buckets never depend on the role
    assert _delta(after["listings"], before["listings"])["total"] == BUCKET_DELTA["total"]
    kinds = {i["kind"] for i in after["needs_you_items"]}
    assert ("approve" in kinds) <= (approve > 0) and ("publish" in kinds) <= (publish > 0)


async def test_publish_counts_when_the_switch_is_on(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("AMAZON_PUBLISH_ENABLED", "true")
    get_settings.cache_clear()
    before = await _get(api, gen_world, "owner@bharat.test")
    scenario(gen_world, "bharat")
    needs = _delta(
        (await _get(api, gen_world, "owner@bharat.test"))["needs_you"], before["needs_you"]
    )
    assert needs["ready_to_publish"] == 2 and needs["publishing_paused"] == 0


@pytest.mark.endpoint("home_summary", case="wrong_tenant")
async def test_another_tenants_rows_never_show(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    mine = await _get(api, gen_world, "owner@acme.test")
    s = scenario(gen_world, "bharat")
    again = await _get(api, gen_world, "owner@acme.test")
    assert again["listings"] == mine["listings"] and again["needs_you"] == mine["needs_you"]
    assert again["listings"]["total"] == _listing_rows(gen_world, "acme")
    ids = {i.get("listing_id") for i in again["needs_you_items"]}
    ids |= {a.get("listing_id") for a in again["activity"]}
    assert not ids & {str(s.blocked), str(s.failed), str(s.question_listing)}


@pytest.mark.endpoint("home_summary", case="missing_auth")
async def test_home_missing_auth(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.get(HOME)).status_code == 401


@pytest.mark.endpoint("home_summary", case="invalid_token", variant="expired")
@pytest.mark.endpoint("home_summary", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("home_summary", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_home_invalid_token(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    shim = types.SimpleNamespace(world=gen_world, tenant=gen_world.tenant)
    headers = bad_token(shim, variant)  # type: ignore[arg-type]
    assert (await api.get(HOME, headers=headers)).status_code == 401
