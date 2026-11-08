"""SI2 T15 (AC1, AC3, AC11, AC12): the real publish job sends the seller's offer and facts
through `AmazonAdapter` and the SP-API client against the in-process fake marketplace."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .failure_support import RETRYABLE_CODE, AmzWorld, amz
from .publish_support import approved, publish_one, retry, run_job
from .review_support import OPERATOR, REVIEWER, Made, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz"]


async def _run(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # RetryLater between attempts is the wrapper's signal
        assert type(exc).__name__ == "RetryLater", repr(exc)


def _sent(amz: AmzWorld, sku: str) -> dict[str, Any]:
    stored = [v for (seller, s), v in amz.state.spapi_items.items() if s == sku]
    assert stored, sku
    return stored[0]["attributes"]  # type: ignore[no-any-return]


async def _product(amz: AmzWorld, **money: Any) -> str:
    from app.services.products import NewProduct, create_product

    sku = f"SI2-{uuid.uuid4().hex[:8].upper()}"
    await create_product(
        amz.world.principal(OPERATOR),
        NewProduct(
            sku=sku,
            title="Cotton Kurta Set",
            category="ethnic_wear_kurta_sets",
            images=(),
            attributes={"color": "blue", "fabric_type": "cotton"},
            **money,
        ),
    )
    return sku


async def test_offer_and_facts_reach_amazon(api: httpx.AsyncClient, amz: AmzWorld) -> None:
    made = await approved(amz.pub)
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
    (sku,) = {s for (_, s) in amz.state.spapi_items}
    attrs = _sent(amz, sku)
    offer = attrs["purchasable_offer"][0]
    assert offer["our_price"][0]["schedule"][0]["value_with_tax"] == 999.0
    assert offer["maximum_retail_price"][0]["schedule"][0]["value_with_tax"] == 1499.0
    assert attrs["condition_type"][0]["value"] == "new_new"
    assert attrs["country_of_origin"][0]["value"] == "IN"
    assert attrs["manufacturer"][0]["value"] == "Test Mills Pvt Ltd"
    assert attrs["material"][0]["value"] == "Cotton"
    (sub,) = amz.pub.submissions(made.listing_id)
    fields = {
        f
        for (f,) in amz.pub.rows(
            "SELECT field FROM publish_field_results WHERE submission_id = %s", [sub["id"]]
        )
    }
    assert {"facts.mrp", "facts.selling_price", "facts.manufacturer"} <= fields


def _schema_issues(amz: AmzWorld, listing_id: uuid.UUID) -> set[tuple[str, str]]:
    rows = amz.pub.rows(
        "SELECT field_path, error_code FROM listing_issues WHERE listing_id = %s "
        "AND source = 'schema'",
        [listing_id],
    )
    return {(field, code) for field, code in rows}


async def _refused_approve(amz: AmzWorld, made: Made) -> int:
    from app.repositories.uow import tenant_uow
    from app.services.review import ReviewItem, approve_batch

    principal = amz.world.principal(REVIEWER)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        outcome = await approve_batch(
            uow,
            principal,
            [ReviewItem(made.listing_id, made.version_id, "approve")],
            http_key=str(uuid.uuid4()),
        )
    return outcome.items[0].http_status


async def test_missing_selling_price_never_sent(api: httpx.AsyncClient, amz: AmzWorld) -> None:
    """SI9 B4: the marketplace check before approval sees the missing offer price, so the
    listing is blocked at generation, never approved, and nothing reaches Amazon."""
    sku = await _product(amz, mrp=Decimal("1499.00"))  # SI1 does not require a price
    made = await generated(amz.world, sku=sku)
    assert amz.pub.listing(made.listing_id)["status"] == "blocked"
    assert ("facts.selling_price", "MISSING_ATTRIBUTE") in _schema_issues(amz, made.listing_id)
    assert await _refused_approve(amz, made) == 409
    assert amz.writes() == 0


async def test_missing_mrp_blocked_before_approval(api: httpx.AsyncClient, amz: AmzWorld) -> None:
    """SI9 B4: a unit without MRP is blocked by the marketplace check at generation (the SI1 D8
    refusal at the publish request stays, `test_publish_mrp_required.py`); the preview shows
    the same item."""
    sku = await _product(amz, selling_price=Decimal("999.00"))
    made = await generated(amz.world, sku=sku)
    assert amz.pub.listing(made.listing_id)["status"] == "blocked"
    assert ("facts.mrp", "MISSING_ATTRIBUTE") in _schema_issues(amz, made.listing_id)
    assert await _refused_approve(amz, made) == 409
    assert amz.writes() == 0
    preview = await api.get(
        f"/api/v1/listings/{made.listing_id}/preview", headers=amz.world.headers(OPERATOR)
    )
    errors = {(e["field"], e["error_code"]) for e in preview.json()["report"]["errors"]}
    assert ("facts.mrp", "MISSING_ATTRIBUTE") in errors


async def test_offer_rejected_then_offer_only_retry(api: httpx.AsyncClient, amz: AmzWorld) -> None:
    amz.state.spapi_reject["purchasable_offer"] = (RETRYABLE_CODE, "Throttled for the offer.")
    made = await approved(amz.pub)
    first = await publish_one(api, amz.pub, made)
    await _run(first["job_id"])
    (sub,) = amz.pub.submissions(made.listing_id)
    assert sub["outcome"] == "partially_accepted"
    status = dict(
        amz.pub.rows(
            "SELECT field, status FROM publish_field_results WHERE submission_id = %s",
            [sub["id"]],
        )
    )
    assert status["facts.mrp"] == status["facts.selling_price"] == "failed"
    assert status["content.title"] != "failed"
    amz.state.spapi_reject.clear()
    response = await retry(api, amz.pub, made)
    assert response.status_code == 202, response.text
    await _run(response.json()["job_id"])
    patches = [c for c in amz.state.calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    last = amz.pub.submissions(made.listing_id)[-1]
    assert sorted(last["field_paths"]) == ["facts.mrp", "facts.selling_price"]
    assert amz.pub.listing(made.listing_id)["status"] == "published"
