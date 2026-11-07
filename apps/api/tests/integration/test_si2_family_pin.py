"""SI2 T14 (AC12; D9): one family digest at every pin site. A generated, approved size_color
family keeps equal pins; a child's price change after approval fails the publish job with
FACTS_CHANGED_SINCE_APPROVAL and nothing is sent; a standalone keeps today's digest bytes."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .family_support import family
from .publish_support import PubWorld, pub, publish_one, run_job
from .review_support import OPERATOR, REVIEWER, Made, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]
CATEGORY = "ethnic_wear_kurta_sets"


async def _approve(pw: PubWorld, made: Made) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.review import ReviewItem, approve_batch

    principal = pw.world.principal(REVIEWER)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        outcome = await approve_batch(
            uow,
            principal,
            [ReviewItem(made.listing_id, made.version_id, "approve")],
            http_key=str(uuid.uuid4()),
        )
    return outcome.items[0]


async def _family_made(pw: PubWorld) -> tuple[Made, dict[str, uuid.UUID]]:
    sku = f"SI2F-{uuid.uuid4().hex[:6].upper()}"
    _style, kids = family(pw.conn, pw.world.tenant("acme"), sku, category=CATEGORY)
    made = await generated(pw.world, OPERATOR, sku=sku)
    return made, kids


async def test_family_approves_then_child_price_change_blocks_publish(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made, kids = await _family_made(pub)
    approved = await _approve(pub, made)
    assert approved.http_status == 200, approved  # equal pins at generation and approve
    child = sorted(kids)[0]
    pub.conn.execute(
        "UPDATE products SET selling_price = 450.00, provenance = provenance || %s WHERE id = %s",
        [json.dumps({"selling_price": f.fact(value="450.00")}), kids[child]],
    )
    item = await publish_one(api, pub, made)
    try:
        await run_job(uuid.UUID(item["job_id"]))
    except Exception as exc:
        assert type(exc).__name__ == "RetryLater", repr(exc)
    job = pub.job(item["job_id"])
    assert job["status"] == "failed"
    text = json.dumps(job.get("error_report_json") or job.get("error_json") or {}, default=str)
    assert "FACTS_CHANGED_SINCE_APPROVAL" in text
    assert pub.fake.calls == []


async def test_standalone_digest_bytes_unchanged(pub: PubWorld) -> None:
    from app.core.resolved_facts import facts_digest, listing_facts_digest
    from app.repositories.uow import tenant_uow

    made = await generated(pub.world, OPERATOR)
    row = pub.listing(made.listing_id)
    async with tenant_uow(pub.world.tenant("acme")) as uow:
        found = await uow.product_facts.resolve_family(row["product_id"])
    assert found is not None and found[1] == ()
    assert listing_facts_digest(*found) == facts_digest(found[0])
    run = pub.scalar(
        "SELECT facts_digest FROM validation_runs WHERE listing_version_id = %s "
        "ORDER BY created_at DESC LIMIT 1",
        [made.version_id],
    )
    assert bytes(run) == facts_digest(found[0])
    assert (await _approve(pub, made)).http_status == 200
