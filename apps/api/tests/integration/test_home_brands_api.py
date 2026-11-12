"""Client feedback 11 Nov 2026, item 18 (ADR-0032 light path): `GET /home/brands` over HTTP, and
the Home bucket filters of `GET /listings?bucket=` and `GET /products?listing=` (item 6), which
must hold exactly what each Home count counts."""

from __future__ import annotations

import types
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .home_support import BUCKET_DELTA, connected, scenario
from .jobs_support import bad_token

__all__ = ["gen_world"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
BRANDS = "/api/v1/home/brands"
HOME = "/api/v1/home/summary"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """As the summary tests: Amazon publishing off, no re-check on read (the list's re-check of
    its first page too), so the scenario's listings keep the states it wrote."""
    from app.core.config import get_settings
    from app.services import listing_display, listing_reads

    async def no_recheck(*_: Any) -> None:
        return None

    async def same_rows(_uow: Any, _principal: Any, rows: Any, **_: Any) -> Any:
        return list(rows)

    monkeypatch.setenv("AMAZON_PUBLISH_ENABLED", "false")
    monkeypatch.setattr(listing_display, "recheck_queue", no_recheck)
    monkeypatch.setattr(listing_reads, "recheck_page", same_rows)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _hdrs(world: GenWorld, email: str) -> dict[str, str]:
    out = world.headers(email)
    out.pop("Idempotency-Key", None)
    return out


async def _json(
    api: httpx.AsyncClient, world: GenWorld, url: str, email: str, **params: Any
) -> dict[str, Any]:
    response = await api.get(url, headers=_hdrs(world, email), params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _join(world: GenWorld, email: str, slug: str, role: str, **columns: Any) -> None:
    """Give `email` a membership in `slug` with `role`."""
    f.insert(
        world.conn,
        "tenant_memberships",
        tenant_id=world.tenant(slug),
        user_id=world.user(email),
        role=role,
        **columns,
    )
    world.conn.commit()


@pytest.mark.endpoint("home_brands", case="success")
async def test_each_brand_counts_inside_itself(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    connected(gen_world, "bharat")
    _join(gen_world, "owner@acme.test", "bharat", "reviewer")
    scenario(gen_world, "bharat")

    body = await _json(api, gen_world, BRANDS, "owner@acme.test")
    assert body["more"] is False
    rows = {r["tenant_id"]: r for r in body["brands"]}
    assert set(rows) == {str(gen_world.tenant("acme")), str(gen_world.tenant("bharat"))}
    names = [r["name"] for r in body["brands"]]
    assert names == sorted(names, key=str.casefold)

    acme = rows[str(gen_world.tenant("acme"))]
    assert acme["current"] is True and acme["role"] == "owner"
    acme_home = await _json(api, gen_world, HOME, "owner@acme.test")
    assert acme["listings"] == acme_home["listings"]
    assert acme["products"] == acme_home["products"]
    assert acme["needs_you"] == acme_home["needs_you"]["total"]

    # bharat as the reviewer the caller is there: the same counts its own reviewer sees
    bharat = rows[str(gen_world.tenant("bharat"))]
    assert bharat["current"] is False and bharat["role"] == "reviewer"
    bharat_home = await _json(api, gen_world, HOME, "reviewer@bharat.test")
    assert bharat["listings"] == bharat_home["listings"]
    assert bharat["products"] == bharat_home["products"]
    assert bharat["needs_you"] == bharat_home["needs_you"]["total"]
    assert bharat["listings"]["total"] >= BUCKET_DELTA["total"]


@pytest.mark.endpoint("home_brands", case="wrong_tenant")
async def test_only_the_callers_live_memberships(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """Another tenant's brand never shows, nor one the caller left, nor a suspended one."""
    conn = gen_world.conn
    _join(
        gen_world, "owner@acme.test", "bharat", "operator",
        is_active=False, deactivated_at=datetime.now(UTC),
    )  # fmt: skip
    suspended = f.tenant(conn, status="suspended")
    f.insert(
        conn, "tenant_memberships", tenant_id=suspended,
        user_id=gen_world.user("owner@acme.test"), role="owner",
    )  # fmt: skip
    conn.commit()
    scenario(gen_world, "bharat")
    body = await _json(api, gen_world, BRANDS, "owner@acme.test")
    assert [r["tenant_id"] for r in body["brands"]] == [str(gen_world.tenant("acme"))]
    assert body["brands"][0]["current"] is True


@pytest.mark.endpoint("home_brands", case="missing_auth")
async def test_brands_missing_auth(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    assert (await api.get(BRANDS)).status_code == 401


@pytest.mark.endpoint("home_brands", case="invalid_token", variant="expired")
@pytest.mark.endpoint("home_brands", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("home_brands", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_brands_invalid_token(
    api: httpx.AsyncClient, gen_world: GenWorld, variant: str
) -> None:
    shim = types.SimpleNamespace(world=gen_world, tenant=gen_world.tenant)
    headers = bad_token(shim, variant)  # type: ignore[arg-type]
    assert (await api.get(BRANDS, headers=headers)).status_code == 401


async def _walk(
    api: httpx.AsyncClient, world: GenWorld, url: str, email: str, **params: Any
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        query = {**params, "limit": 2, **({"cursor": cursor} if cursor else {})}
        page = await _json(api, world, url, email, **query)
        rows += page["data"]
        if not page["page"]["has_more"]:
            return rows
        cursor = page["page"]["next_cursor"]


FILTERS = {
    "live": ("live",),
    "partly_live": ("partly_live",),
    "any_live": ("live", "partly_live"),
    "in_progress": ("in_progress",),
    "problem": ("problem",),
    "ready_to_approve": ("ready_to_approve",),
    "ready_to_publish": ("ready_to_publish",),
    "draft": ("draft",),
    "inactive": ("inactive",),
}


async def test_bucket_filters_hold_what_home_counts(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """Every bucket filter, over keyset pages of 2, lists exactly the listings the summary
    counts in that bucket (per marketplace too), and the products list exactly their owners."""
    connected(gen_world, "bharat")
    scenario(gen_world, "bharat")
    email = "owner@bharat.test"
    home = await _json(api, gen_world, HOME, email)
    per = {m["marketplace"]: m for m in home["by_marketplace"]}
    seen: set[str] = set()
    for name, buckets in FILTERS.items():
        listings = await _walk(api, gen_world, "/api/v1/listings", email, bucket=name)
        assert len(listings) == sum(home["listings"][b] for b in buckets), name
        ids = {row["id"] for row in listings}
        assert len(ids) == len(listings), name
        seen |= ids
        owners = {row["product_id"] for row in listings}
        products = await _walk(api, gen_world, "/api/v1/products", email, listing=name)
        assert {p["id"] for p in products} == owners, name
        if name in ("live", "problem", "in_progress", "ready_to_approve", "ready_to_publish"):
            amazon = await _walk(
                api, gen_world, "/api/v1/listings", email, bucket=name, marketplace="amazon"
            )
            assert all(row["marketplace"] == "amazon" for row in amazon)
            assert len(amazon) == per["amazon"][name], name
            on_amazon = {row["product_id"] for row in amazon}
            scoped = await _walk(
                api, gen_world, "/api/v1/products", email, listing=name, marketplace="amazon"
            )
            assert {p["id"] for p in scoped} == on_amazon, name
    # the single buckets cover every listing that is not archived exactly once
    assert len(seen) == home["listings"]["total"]


async def test_bucket_filter_stays_in_the_tenant(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    s = scenario(gen_world, "bharat")
    rows = await _walk(api, gen_world, "/api/v1/listings", "owner@acme.test", bucket="problem")
    assert str(s.blocked) not in {r["id"] for r in rows}
    products = await _walk(api, gen_world, "/api/v1/products", "owner@acme.test", listing="problem")
    assert str(s.blocked_product) not in {p["id"] for p in products}


async def test_unknown_bucket_is_a_422(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    for url, key in (("/api/v1/listings", "bucket"), ("/api/v1/products", "listing")):
        response = await api.get(
            url, headers=_hdrs(gen_world, "owner@acme.test"), params={key: "everything"}
        )
        assert response.status_code == 422, url
