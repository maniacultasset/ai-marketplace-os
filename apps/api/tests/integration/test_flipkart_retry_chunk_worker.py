"""S22b F6, F7 re-check (invariant 10, L5300-5304) through the real publish job, retry-fields
route, S21 retry ledger and Postgres: a family child kept from Flipkart by a 429 on a later create
chunk, or refused again by the retry's create, is created by a later retry; no attempt is burned as
SUBMISSION_NOT_APPLIED. `CHUNK` is lowered to 2 so the 4-child KURTA-FAM-01 family spans two
chunks."""

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


def _sent(fkp: FkWorld, before: int) -> dict[str, str]:
    return {
        e["sku"]: "update" if c["path"].endswith("/update") else "create"
        for c in fkp.calls()[before:]
        for e in json.loads(c["body"])["listings"]
    }


def _created(fkp: FkWorld) -> set[str]:
    return {k[1] for k in fkp.app.state.fake.fk_items}


def _throttle(fkp: FkWorld, monkeypatch: pytest.MonkeyPatch, on: int) -> dict[str, int]:
    """From the `on`-th create call of the job, the fake answers 429 past the retry budget."""
    from app.adapters.flipkart.client import FlipkartClient

    original = FlipkartClient.create
    seen = {"n": 0, "on": on}

    async def create(self: Any, listings: Any) -> Any:
        seen["n"] += 1
        if seen["on"] and seen["n"] == seen["on"]:
            fkp.app.state.fake.fk_429 = 10
        return await original(self, listings)

    monkeypatch.setattr(FlipkartClient, "create", create)
    return seen


def _calm(fkp: FkWorld, seen: dict[str, int]) -> None:
    fkp.app.state.fake.fk_429 = 0
    seen["on"] = 0


async def _publish(api: httpx.AsyncClient, fkp: FkWorld, made: Any) -> None:
    response = await post_publish(api, fkp.pub, OPERATOR, [{"listing_id": str(made.listing_id)}])
    (item,) = response.json()["items"]
    await run_job(uuid.UUID(item["job_id"]))


async def _retry(api: httpx.AsyncClient, fkp: FkWorld, made: Any) -> dict[str, Any]:
    response = await retry(api, fkp.pub, made)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    return fkp.pub.job(response.json()["job_id"])


def _outcomes(fkp: FkWorld, made: Any) -> list[tuple[int, str]]:
    return [(s["attempt"], s["outcome"]) for s in fkp.pub.submissions(made.listing_id)]


def _codes(fkp: FkWorld, made: Any) -> list[Any]:
    return [s.get("error_code") for s in fkp.pub.submissions(made.listing_id)]


async def test_flipkart_worker_retry_creates_the_throttled_chunk(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: chunk 1 applies, chunk 2 is refused 429: the retry sends only chunk 2's children,
    through create, and the listing ends published on attempt 2."""
    from app.adapters.flipkart import constants

    monkeypatch.setattr(constants, "CHUNK", 2)
    made = await approved(fkp.pub, sku="KURTA-FAM-01", marketplace="flipkart")
    seen = _throttle(fkp, monkeypatch, on=2)
    await _publish(api, fkp, made)
    _calm(fkp, seen)
    first = _created(fkp)
    assert len(first) == 2, first
    assert _outcomes(fkp, made) == [(1, "partially_accepted")], _outcomes(fkp, made)

    before = len(fkp.calls())
    job = await _retry(api, fkp, made)
    assert job["status"] == "succeeded", job.get("error_report_json")
    sent = _sent(fkp, before)
    assert set(sent).isdisjoint(first), sent
    assert len(sent) == 2 and set(sent.values()) == {"create"}, sent
    assert len(_created(fkp)) == 4
    outcomes = _outcomes(fkp, made)
    assert outcomes[1][1] not in ("pending", "error"), (outcomes, _codes(fkp, made))


async def test_flipkart_worker_retry_create_refused_then_created(
    api: httpx.AsyncClient, fkp: FkWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7: a child refused at create on one attribute, refused again by attempt 2's create, is
    created by attempt 3 through the create route; no attempt ends SUBMISSION_NOT_APPLIED."""
    from tests.support import fake_marketplace as fake_module

    made = await approved(fkp.pub, sku="KURTA-FAM-01", marketplace="flipkart")
    real = fake_module._fk_write
    target: dict[str, Any] = {"left": 2}

    def write(state: Any, seller: str, entry: Any, *, update: bool) -> dict[str, Any]:
        sku = entry.get("sku") if isinstance(entry, dict) else None
        target.setdefault("sku", sku)
        if sku == target["sku"] and target["left"] > 0 and not update:
            target["left"] -= 1
            name = sorted(entry["attributes"])[0]
            return {
                "sku": sku,
                "status": "FAILURE",
                "errors": [
                    {
                        "code": QUOTA[0],
                        "description": QUOTA[1],
                        "severity": "ERROR",
                        "attribute": name,
                    }
                ],
            }
        return real(state, seller, entry, update=update)

    monkeypatch.setattr(fake_module, "_fk_write", write)
    await _publish(api, fkp, made)
    sku = target["sku"]
    assert sku not in _created(fkp)
    assert _outcomes(fkp, made) == [(1, "partially_accepted")]

    before = len(fkp.calls())
    second = await _retry(api, fkp, made)
    assert _sent(fkp, before) == {sku: "create"}, _sent(fkp, before)
    assert sku not in _created(fkp)
    assert second["status"] in ("succeeded", "failed"), second

    before = len(fkp.calls())
    third = await _retry(api, fkp, made)
    assert third["status"] == "succeeded", (third.get("error_report_json"), _codes(fkp, made))
    assert _sent(fkp, before) == {sku: "create"}, _sent(fkp, before)
    assert sku in _created(fkp)
    assert [a for a, _ in _outcomes(fkp, made)] == [1, 2, 3]
    assert "SUBMISSION_NOT_APPLIED" not in [str(c) for c in _codes(fkp, made)]
