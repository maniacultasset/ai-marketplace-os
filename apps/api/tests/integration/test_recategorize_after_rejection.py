"""SI13 T8 (AC3; P:A2, D9; CP-D7): a rejected, never-live listing with Amazon's open
CATEGORY_MISMATCH may be recategorised. It keeps `failed` (no edge: the user `edit` edge needs a
new edit version), the audit reason names the issue, and the seller's regenerate (which voids the
approval), the maker-checker approval and the publish follow as usual. Everything else stays
409."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import set_status
from .publish_support import PubWorld, approved, pub, publish_and_run
from .review_support import OPERATOR, OWNER, REVIEWER, item, post_approve, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]
NEW = "tshirts"


def _url(product_id: object) -> str:
    return f"/api/v1/products/{product_id}/recategorize"


async def _post(api: httpx.AsyncClient, pw: PubWorld, product: Any, category: str = NEW) -> Any:
    return await api.post(
        _url(product), headers=pw.world.headers(OPERATOR), json={"category": category}
    )


async def _rejected(api: httpx.AsyncClient, pw: PubWorld, *, issue: bool = True) -> Any:
    """An approved listing whose create Amazon refused in part (failed, never live)."""
    made = await approved(pw)
    pw.fake.set_fail_fields(
        {
            "content.title": (
                "4000003",
                "The product type is not valid.",
                ErrorCode.CATEGORY_MISMATCH,
            )
        }
    )
    await publish_and_run(api, pw, made)
    pw.fake.set_fail_fields({})
    row = pw.listing(made.listing_id)
    assert row["status"] == "failed" and row["first_published_at"] is None, row
    pw.conn.execute(
        "DELETE FROM listing_issues WHERE listing_id = %s AND error_code = 'CATEGORY_MISMATCH'",
        [made.listing_id],
    )
    issue_id = None
    if issue:
        issue_id = f.listing_issue(
            pw.conn,
            pw.world.tenant("acme"),
            made.listing_id,
            source="amazon_adapter",
            field_path="facts.category",
            error_code="CATEGORY_MISMATCH",
            error_class="category_mismatch",
            retry_class="user_action",
            severity="error",
            message="Amazon refused the product type.",
            marketplace_code="4000003",
            listing_version_id=made.version_id,
            dedupe_key=f"amazon_adapter|facts.category|CATEGORY_MISMATCH|4000003|{uuid.uuid4()}",
        )
    product = pw.scalar("SELECT product_id FROM listings WHERE id = %s", [made.listing_id])
    return made, uuid.UUID(str(product)), issue_id


# ---- the happy path ----


async def test_recategorize_regenerate_approve_publish(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made, product, issue_id = await _rejected(api, pub)
    response = await _post(api, pub, product)
    assert response.status_code == 200, response.text
    assert pub.scalar("SELECT category FROM products WHERE id = %s", [product]) == NEW
    row = pub.listing(made.listing_id)
    assert row["status"] == "failed" and row["category"] == NEW
    reason = pub.scalar(
        "SELECT reason FROM audit_log WHERE action = 'product.recategorized' "
        "AND entity_id = %s ORDER BY occurred_at DESC LIMIT 1",
        [product],
    )
    assert str(reason).startswith(f"recategorize;issue:{issue_id};listings:")
    # the issue stays open until the regenerate makes a new version
    assert pub.scalar("SELECT status FROM listing_issues WHERE id = %s", [issue_id]) == "open"
    regen = await api.post(
        f"/api/v1/listings/{made.listing_id}/regenerate",
        headers=pub.world.headers(OWNER, str(uuid.uuid4())),
    )
    assert regen.status_code in (200, 202), regen.text
    job = pub.scalar(
        "SELECT id FROM job_runs WHERE listing_id = %s AND kind = 'listing_generate' "
        "AND status = 'queued'",
        [made.listing_id],
    )
    await run_job(job)
    after = pub.listing(made.listing_id)
    assert after["current_version_id"] != made.version_id
    assert after["approved_version_id"] is None  # the regenerate voided the old approval
    pinned = pub.scalar(
        "SELECT t.product_type FROM listing_versions v JOIN category_templates t "
        "ON t.id = v.category_template_id WHERE v.id = %s",
        [after["current_version_id"]],
    )
    assert pinned == "SHIRT"
    new = type(made)(made.listing_id, after["current_version_id"], job)
    by_maker = await post_approve(api, pub.world, OWNER, [item(new)])  # the regenerate's maker
    assert by_maker.json()["items"][0]["http_status"] == 403, by_maker.text
    by_other = await post_approve(api, pub.world, REVIEWER, [item(new)])
    assert by_other.json()["items"][0]["http_status"] == 200, by_other.text
    calls = len(pub.fake.calls)
    await publish_and_run(api, pub, new)
    assert len(pub.fake.calls) > calls  # the new version went out, a new attempt 1
    attempt = pub.scalar(
        "SELECT attempt FROM listing_submissions WHERE listing_id = %s "
        "AND listing_version_id = %s ORDER BY created_at DESC LIMIT 1",
        [made.listing_id, new.version_id],
    )
    assert attempt == 1


# ---- refusals and replay ----


async def test_without_the_issue_is_409(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made, product, _ = await _rejected(api, pub, issue=False)
    response = await _post(api, pub, product)
    assert response.status_code == 409, response.text
    assert pub.listing(made.listing_id)["status"] == "failed"


async def test_ever_live_is_409(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made, product, _ = await _rejected(api, pub)
    pub.conn.execute(
        "UPDATE listings SET first_published_at = now() WHERE id = %s", [made.listing_id]
    )
    assert (await _post(api, pub, product)).status_code == 409


async def test_published_is_409(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made, product, _ = await _rejected(api, pub)
    set_status(pub.world, made.listing_id, "published")
    assert (await _post(api, pub, product)).status_code == 409


async def test_same_category_is_idempotent(api: httpx.AsyncClient, pub: PubWorld) -> None:
    made, product, _ = await _rejected(api, pub)
    current = pub.scalar("SELECT category FROM products WHERE id = %s", [product])
    response = await _post(api, pub, product, str(current))
    assert response.status_code == 200
    assert pub.listing(made.listing_id)["status"] == "failed"


async def test_another_tenants_product_is_404(api: httpx.AsyncClient, pub: PubWorld) -> None:
    _made, product, _ = await _rejected(api, pub)
    response = await api.post(
        _url(product), headers=pub.world.headers("owner@bharat.test"), json={"category": NEW}
    )
    assert response.status_code == 404, response.text
