"""SI1 T20 (AC16; decision 17; CP10): a sellable unit without MRP is refused at the publish request
and again in the worker. Run in the Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, post_publish, pub, publish_one
from .review_support import OPERATOR
from .test_publish_attacks import _run_quiet

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


def _product_of(pub: PubWorld, listing_id: uuid.UUID) -> uuid.UUID:
    found = pub.conn.execute(
        "SELECT product_id FROM listings WHERE id = %s", [listing_id]
    ).fetchone()
    assert found is not None
    product: uuid.UUID = found[0]
    return product


def _clear_mrp(pub: PubWorld, product_id: uuid.UUID) -> None:
    pub.conn.execute(
        "UPDATE products SET mrp = NULL, provenance = provenance - 'mrp' WHERE id = %s",
        [product_id],
    )


def _item(response: Any) -> dict[str, Any]:
    (item,) = response.json()["items"]
    return item  # type: ignore[no-any-return]


async def test_standalone_without_mrp_refused_at_request(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    product = _product_of(pub, made.listing_id)
    sku = pub.conn.execute("SELECT sku FROM products WHERE id = %s", [product]).fetchone()
    assert sku is not None
    _clear_mrp(pub, product)
    response = await post_publish(api, pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    item = _item(response)
    assert item["http_status"] == 409
    error = item["error"]
    assert error["error_code"] == "CRITICAL_FACT_MISSING"
    assert error["errors"][0]["field"] == "facts.mrp"
    assert sku[0] in error["errors"][0]["message"]
    jobs = pub.conn.execute(
        "SELECT count(*) FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish'",
        [made.listing_id],
    ).fetchone()
    assert jobs == (0,)


async def test_family_child_without_mrp_refused_and_inherited_accepted(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub, sku="KURTA-FAM-01")
    style = _product_of(pub, made.listing_id)
    child = pub.conn.execute(
        "SELECT id, sku FROM products WHERE parent_product_id = %s ORDER BY id LIMIT 1", [style]
    ).fetchone()
    assert child is not None
    # the children inherit the style's seeded MRP: accepted
    ok = await post_publish(api, pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    assert ok.status_code == 202, ok.text
    assert _item(ok)["http_status"] == 202 and _item(ok)["error"] is None
    assert child[1]


async def test_family_without_any_mrp_refused(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made = await approved(pub, sku="KURTA-FAM-01")
    style = _product_of(pub, made.listing_id)
    _clear_mrp(pub, style)
    for (child,) in pub.conn.execute(
        "SELECT id FROM products WHERE parent_product_id = %s", [style]
    ).fetchall():
        _clear_mrp(pub, child)
    response = await post_publish(api, pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    error = _item(response)["error"]
    assert error["error_code"] == "CRITICAL_FACT_MISSING"
    assert error["errors"][0]["field"].startswith("variants[")
    assert error["errors"][0]["field"].endswith("].facts.mrp")


async def test_mrp_cleared_after_enqueue_worker_refuses(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    item = await publish_one(api, pub, made)
    _clear_mrp(pub, _product_of(pub, made.listing_id))
    await _run_quiet(item["job_id"])
    job = pub.job(item["job_id"])
    assert (job["status"], job["error_code"]) == ("failed", "CRITICAL_FACT_MISSING")
    assert pub.fake.calls == [] and pub.submissions(made.listing_id) == []
