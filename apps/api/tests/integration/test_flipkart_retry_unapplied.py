"""S22b fix round 2 (F3, F4; invariant 10, L5300-5304): a Flipkart family child that Flipkart
never created is created by the next retry, and only that child is sent (from the tester's
repros)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .flipkart_publish_support import FkWorld, fkp, pub
from .publish_support import approved, post_publish, retry, run_job
from .review_support import OPERATOR

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["fkp", "pub"]
QUOTA = ("FAKE-FK-QUOTA", "Request throttled")


def _sent(fkp: FkWorld, before: int) -> dict[str, tuple[str, list[str]]]:
    return {
        e["sku"]: (
            "update" if c["path"].endswith("/update") else "create",
            sorted(e["attributes"]),
        )
        for c in fkp.calls()[before:]
        for e in json.loads(c["body"])["listings"]
    }


async def _publish(api: httpx.AsyncClient, fkp: FkWorld, made: Any) -> None:
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    await run_job(uuid.UUID(item["job_id"]))


async def _retry(api: httpx.AsyncClient, fkp: FkWorld, made: Any) -> dict[str, Any]:
    response = await retry(api, fkp.pub, made)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    return fkp.pub.job(response.json()["job_id"])


async def test_flipkart_retry_creates_child_refused_at_create(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: a child refused with a SKU-level error (failed path `listing`) is created by
    retry-fields in the same attempt; no INTERNAL_ERROR, no `pending` attempt left behind."""
    from tests.support import fake_marketplace as fake_module

    made = await approved(fkp.pub, sku="KURTA-FAM-01", marketplace="flipkart")
    real = fake_module._fk_write
    target: dict[str, Any] = {"left": 1}

    def write(state: Any, seller: str, entry: Any, *, update: bool) -> dict[str, Any]:
        sku = entry.get("sku") if isinstance(entry, dict) else None
        target.setdefault("sku", sku)
        if sku == target["sku"] and target["left"] > 0:
            return {
                "sku": sku,
                "status": "FAILURE",
                "errors": [{"code": QUOTA[0], "description": QUOTA[1], "severity": "ERROR"}],
            }
        return real(state, seller, entry, update=update)

    monkeypatch.setattr(fake_module, "_fk_write", write)
    await _publish(api, fkp, made)
    target["left"] = 0
    sku = target["sku"]
    (first,) = fkp.pub.submissions(made.listing_id)
    assert first["outcome"] == "partially_accepted", first["outcome"]

    before = len(fkp.calls())
    job = await _retry(api, fkp, made)
    assert job["status"] == "succeeded", job.get("error_report_json")
    assert set(_sent(fkp, before)) == {sku}
    assert _sent(fkp, before)[sku][0] == "create"
    assert any(k[1] == sku for k in fkp.app.state.fake.fk_items)
    subs = [(s["attempt"], s["outcome"]) for s in fkp.pub.submissions(made.listing_id)]
    assert subs[0] == (1, "partially_accepted") and subs[1][1] != "pending", subs


async def test_flipkart_retry_sends_only_the_unapplied_child(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4: one child refused on every attribute while Flipkart created the other three: the
    attempt is `partially_accepted`, and the retry sends only that child, through create."""
    from tests.support import fake_marketplace as fake_module

    made = await approved(fkp.pub, sku="KURTA-FAM-01", marketplace="flipkart")
    target: dict[str, str] = {}
    real = fake_module._fk_issues

    def issues(state: Any, sku: str, attributes: Any) -> list[dict[str, Any]]:
        found = real(state, sku, attributes)
        if not target:
            target["sku"] = sku
        if sku == target["sku"] and state.fk_requests_left > 0:
            return found + [
                {"code": QUOTA[0], "description": QUOTA[1], "severity": "ERROR", "attribute": n}
                for n in attributes
            ]
        return found

    fkp.app.state.fake.fk_requests_left = 1
    monkeypatch.setattr(fake_module, "_fk_issues", issues)
    await _publish(api, fkp, made)
    fkp.app.state.fake.fk_requests_left = 0
    sku = target["sku"]
    assert all(k[1] != sku for k in fkp.app.state.fake.fk_items)
    (first,) = fkp.pub.submissions(made.listing_id)
    assert first["outcome"] == "partially_accepted", first["outcome"]
    applied = fkp.pub.rows(
        "SELECT seller_sku FROM listing_items WHERE listing_id = %s AND external_id IS NOT NULL",
        [made.listing_id],
    )
    assert len(applied) == 3

    before = len(fkp.calls())
    job = await _retry(api, fkp, made)
    assert job["status"] == "succeeded", job.get("error_report_json")
    sent = _sent(fkp, before)
    assert set(sent) == {sku}, (first["outcome"], sent)
    assert sent[sku][0] == "create"
    assert any(k[1] == sku for k in fkp.app.state.fake.fk_items)
