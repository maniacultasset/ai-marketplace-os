"""SI2 tester attacks (AC3, AC5, AC12) through the real publish job, `AmazonAdapter` and the
SP-API client against the in-process fake marketplace: a price above the MRP and a non-INR
currency are refused with nothing sent, a replayed publish job sends nothing twice, a brand
manufacturer edit after approval breaks the approval pin, and a marketplace issue that echoes
the full manufacturer contact never stores the address (a partial echo: docs/plans/
si2-marketplace-mapping/repro/test_partial_address_echo.py)."""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .failure_support import AmzWorld, amz
from .publish_support import approved, post_publish, publish_one, run_job
from .review_support import OPERATOR

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz"]

ADDRESS_PART = "1 Mill Road"


async def _run(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # RetryLater between attempts is the wrapper's signal
        assert type(exc).__name__ == "RetryLater", repr(exc)


async def _product(amz: AmzWorld, **money: Any) -> str:
    from app.services.products import NewProduct, create_product

    sku = f"SI2X-{uuid.uuid4().hex[:8].upper()}"
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


def _stored_errors(amz: AmzWorld, listing_id: uuid.UUID, job_id: Any) -> str:
    job = amz.pub.job(job_id)
    text = json.dumps(
        {k: v for k, v in job.items() if "error" in k or k == "result_json"}, default=str
    )
    rows = amz.pub.rows(
        "SELECT field, status, error_json FROM publish_field_results WHERE listing_id = %s",
        [listing_id],
    )
    return text + json.dumps(rows, default=str)


async def test_price_above_mrp_and_non_inr_refused_before_any_send(amz: AmzWorld) -> None:
    """AC3's PRICE_ABOVE_MRP and USD cases cannot reach the publish job: the product service
    refuses a price above the MRP and the products CHECKs refuse both in SQL, so the adapter
    errors are a preview-only backstop (unit-tested in test_amazon_offer.py)."""
    import psycopg
    from app.core.errors import InputError

    with pytest.raises(InputError) as refused:
        await _product(amz, mrp=Decimal("999.00"), selling_price=Decimal("1499.00"))
    assert refused.value.field == "selling_price"
    sku = await _product(amz, mrp=Decimal("1499.00"), selling_price=Decimal("999.00"))
    tenant = amz.world.principal(OPERATOR).tenant_id
    changes = (
        "UPDATE products SET selling_price = 1999.00 WHERE tenant_id = %s AND sku = %s",
        "UPDATE products SET currency = 'USD' WHERE tenant_id = %s AND sku = %s",
    )
    for change in changes:
        with pytest.raises(psycopg.errors.CheckViolation), amz.pub.conn.transaction():
            amz.pub.conn.execute(change, [tenant, sku])
    assert amz.writes() == 0


async def test_replayed_publish_job_sends_nothing_twice(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
    first = amz.writes()
    assert first >= 1
    await _run(item["job_id"])  # duplicate delivery of the same job, same facts
    assert amz.writes() == first
    assert len(amz.pub.submissions(made.listing_id)) == 1


async def test_brand_manufacturer_edit_after_approval_breaks_the_pin(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)
    tenant = amz.world.principal(OPERATOR).tenant_id
    amz.pub.conn.execute(
        "UPDATE business_parties SET address_line1 = '9 New Mill Lane' WHERE tenant_id = %s"
        " AND id = (SELECT default_manufacturer_party_id FROM brand_profiles"
        " WHERE tenant_id = %s)",
        [tenant, tenant],
    )
    response = await post_publish(api, amz.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    if item["http_status"] == 202:
        await _run(item["job_id"])
        assert amz.pub.job(item["job_id"])["status"] != "succeeded"
        text = _stored_errors(amz, made.listing_id, item["job_id"])
    else:
        text = json.dumps(item, default=str)
    assert amz.writes() == 0
    assert "FACTS_CHANGED_SINCE_APPROVAL" in text


async def test_marketplace_echo_of_the_address_is_never_stored(
    api: httpx.AsyncClient, amz: AmzWorld, caplog: pytest.LogCaptureFixture
) -> None:
    made = await approved(amz.pub)
    contact = amz.pub.scalar(
        "SELECT concat_ws(', ', legal_name, address_line1) FROM business_parties"
        " WHERE tenant_id = %s AND legal_name = 'Test Mills Pvt Ltd' LIMIT 1",
        [amz.world.principal(OPERATOR).tenant_id],
    )
    assert ADDRESS_PART in contact
    # Amazon echoes the value it refused: the full contact line the adapter sent
    amz.state.spapi_reject["manufacturer_contact_information"] = (
        "90220",
        "The value 'Test Mills Pvt Ltd, 1 Mill Road, Surat, GJ, 395003, IN' is invalid.",
    )
    with caplog.at_level(logging.DEBUG):
        item = await publish_one(api, amz.pub, made)
        await _run(item["job_id"])
    (sub,) = amz.pub.submissions(made.listing_id)
    rows = amz.pub.rows(
        "SELECT field, status, error_json FROM publish_field_results WHERE submission_id = %s",
        [sub["id"]],
    )
    status = {r[0]: r[1] for r in rows}
    assert status.get("facts.manufacturer") == "failed", status
    issues = amz.pub.rows(
        "SELECT raw_message, message, plain_message, value_submitted FROM listing_issues"
        " WHERE listing_id = %s",
        [made.listing_id],
    )
    assert issues
    stored = json.dumps(rows + issues, default=str)
    stored += _stored_errors(amz, made.listing_id, item["job_id"])
    assert ADDRESS_PART not in stored
    assert ADDRESS_PART not in caplog.text
