"""SI4 phase A, tester round 1: attacks on the shared blocking rule (Approval record,
2026-11-07), the fact question shared across marketplaces, and the auto-fix trail across tenants.

- A blocking error on Amazon and a warning on Flipkart for the same fact: one question, linked
  only to the error row; the Flipkart listing stays approvable.
- A live question alone never blocks: once a listing's own issue closes it can be approved while
  the shared question still waits for the other marketplace.
- Another tenant never sees the trail: not in its inbox, not in the listing's history."""

from __future__ import annotations

import uuid

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bearer_for
from .review_support import OPERATOR, REVIEWER, Made, generated, only_item, post_approve
from .test_issue_tasks import (
    CATEGORY,
    FIBER,
    FILLABLE,
    Pipeline,
    issues,
    product_of,
    regenerate,
    require,
    sku_of,
    state,
    tasks,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


def _require_on(world: GenWorld, marketplace: str, severity: str) -> None:
    f.rule(
        world.conn,
        None,
        marketplace=marketplace,
        category=CATEGORY,
        field=f"attributes.{FIBER}",
        rule_type="required_attribute",
        subject=FIBER,
        severity=severity,
        rule_def=f.rule_def(
            "required_attribute",
            attribute=FIBER,
            error_code="MISSING_ATTRIBUTE",
            message="The {attribute} is required.",
            policy_ref=f.POLICY_REF,
        ),
    )


def _current(world: GenWorld, made: Made) -> Made:
    version = world.listing(made.listing_id)["current_version_id"]
    return Made(made.listing_id, version, made.job_id)


async def test_an_error_on_one_marketplace_and_a_warning_on_the_other(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_on(gen_world, "amazon", "error")
    _require_on(gen_world, "flipkart", "warning")
    Pipeline(monkeypatch).overrides[FIBER] = None
    amazon = await generated(gen_world)
    flipkart = await generated(gen_world, sku=sku_of(gen_world, amazon), marketplace="flipkart")
    assert state(gen_world, amazon) == ("blocked", "human_task_open")
    assert state(gen_world, flipkart) == ("needs_review", None)  # a warning leaves it approvable
    [task] = tasks(gen_world, product_of(gen_world, amazon))
    [amazon_row] = [i for i in issues(gen_world, amazon.listing_id) if i["status"] == "open"]
    [flipkart_row] = [i for i in issues(gen_world, flipkart.listing_id) if i["status"] == "open"]
    assert (amazon_row["severity"], amazon_row["human_task_id"]) == ("error", task["id"])
    assert (flipkart_row["severity"], flipkart_row["human_task_id"]) == ("warning", None)


async def test_a_live_shared_question_alone_never_blocks_the_approval(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    require(gen_world, FILLABLE)
    pipeline = Pipeline(monkeypatch)
    pipeline.overrides[FILLABLE] = None
    amazon = await generated(gen_world)
    flipkart = await generated(gen_world, sku=sku_of(gen_world, amazon), marketplace="flipkart")
    product = product_of(gen_world, amazon)
    pipeline.overrides.clear()
    await regenerate(gen_world, amazon)  # Amazon's own issue closes; Flipkart's stays open
    assert state(gen_world, amazon) == ("validated", None)
    assert state(gen_world, flipkart) == ("blocked", "human_task_open")
    [task] = tasks(gen_world, product)
    assert task["status"] == "open"
    response = await post_approve(api, gen_world, REVIEWER, [_item(_current(gen_world, amazon))])
    got = only_item(response)
    assert got["http_status"] == 200, got
    assert gen_world.listing(amazon.listing_id)["status"] == "approved"
    assert [t["status"] for t in tasks(gen_world, product)] == ["open"]


def _item(made: Made) -> dict[str, str]:
    return {
        "listing_id": str(made.listing_id),
        "listing_version_id": str(made.version_id),
        "decision": "approve",
    }


async def test_another_tenant_never_sees_the_trail(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world, scenario="stub:long_title")
    own = bearer_for(gen_world, OPERATOR)
    other = bearer_for(gen_world, "reviewer@bharat.test")
    params = {"status": "auto_fixed", "listing_id": str(made.listing_id)}
    mine = await api.get("/api/v1/issues", params=params, headers=own)
    assert mine.status_code == 200, mine.text
    assert len(mine.json()["data"]) == 1
    theirs = await api.get("/api/v1/issues", params=params, headers=other)
    assert theirs.status_code in (200, 404), theirs.text
    if theirs.status_code == 200:
        assert theirs.json()["data"] == []
    everything = await api.get("/api/v1/issues", params={"status": "auto_fixed"}, headers=other)
    assert everything.status_code == 200, everything.text
    assert str(made.listing_id) not in {i["listing_id"] for i in everything.json()["data"]}
    history = await api.get(f"/api/v1/listings/{made.listing_id}/history", headers=other)
    assert history.status_code == 404, history.text
    issue_id = mine.json()["data"][0]["id"]
    dismiss = await api.post(
        f"/api/v1/issues/{issue_id}/dismiss",
        headers={**other, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert dismiss.status_code in (403, 404), dismiss.text
