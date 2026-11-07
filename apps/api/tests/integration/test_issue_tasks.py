"""SI4 A13 (AC9, AC10; D9, D10, D19; CP-D14, CP-D15; Approval record "shared blocking rule"):
issue-sync step 4. An open blocking issue that only the seller can fix becomes one Inbox question,
linked to it; the question is cancelled when every linked issue closes. A question never blocks a
listing by itself: the open blocking issue behind it does."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bearer_for
from .review_support import OPERATOR, Made, generated, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
CATEGORY = "ethnic_wear_kurta_sets"
FIBER = "fiber_composition"
TARGET = f"facts.attributes.{FIBER}"


def require(world: GenWorld, key: str, *, severity: str = "error") -> None:
    """A platform `required_attribute` rule on `attributes.<key>` for both marketplaces."""
    for marketplace in ("amazon", "flipkart"):
        f.rule(
            world.conn,
            None,
            marketplace=marketplace,
            category=CATEGORY,
            field=f"attributes.{key}",
            rule_type="required_attribute",
            subject=key,
            severity=severity,
            rule_def=f.rule_def(
                "required_attribute",
                attribute=key,
                error_code="MISSING_ATTRIBUTE",
                message="The {attribute} is required.",
                policy_ref=f.POLICY_REF,
            ),
        )


class Pipeline:
    """The stub pipeline, with attributes of its final listing overridden (None drops one)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.workers import generation

        self.overrides: dict[str, Any] = {}
        monkeypatch.setattr(generation, "run_listing_pipeline", self.run)

    async def run(self, ctx: Any, deps: Any) -> Any:
        from mkos_pipeline import run_listing_pipeline

        result = await run_listing_pipeline(ctx, deps)
        listing = result.listing
        if listing is None or not self.overrides:
            return result
        attributes = dict(listing.attributes)
        for key, value in self.overrides.items():
            if value is None:
                attributes.pop(key, None)
            else:
                attributes[key] = value
        listing = listing.model_copy(update={"attributes": attributes})
        return result.model_copy(update={"listing": listing})


def tasks(world: GenWorld, product: uuid.UUID) -> list[dict[str, Any]]:
    cur = world.conn.execute(
        "SELECT * FROM human_tasks WHERE product_id = %s ORDER BY created_at, id", [product]
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def issues(world: GenWorld, listing: uuid.UUID) -> list[dict[str, Any]]:
    cur = world.conn.execute(
        "SELECT * FROM listing_issues WHERE listing_id = %s ORDER BY created_at, id", [listing]
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def product_of(world: GenWorld, made: Made) -> uuid.UUID:
    return world.scalar(  # type: ignore[no-any-return]
        "SELECT product_id FROM listings WHERE id = %s", [made.listing_id]
    )


def sku_of(world: GenWorld, made: Made) -> str:
    return world.scalar(  # type: ignore[no-any-return]
        "SELECT p.sku FROM products p JOIN listings l ON l.product_id = p.id WHERE l.id = %s",
        [made.listing_id],
    )


def state(world: GenWorld, made: Made) -> tuple[str, str | None]:
    row = world.listing(made.listing_id)
    return row["status"], row["status_reason"]


async def regenerate(world: GenWorld, made: Made, instructions: str | None = None) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_regeneration

    principal = world.principal(OPERATOR)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        accepted = await request_regeneration(
            uow, principal, str(uuid.uuid4()), made.listing_id, instructions
        )
    await run_job(accepted.job_id)


# ---- AC9 ----


async def test_a_missing_critical_fact_is_one_question(
    api: httpx.AsyncClient, gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    require(gen_world, FIBER)
    Pipeline(monkeypatch).overrides[FIBER] = None
    made = await generated(gen_world)
    assert state(gen_world, made) == ("blocked", "human_task_open")
    [task] = tasks(gen_world, product_of(gen_world, made))
    assert (task["kind"], task["target_path"], task["status"]) == ("missing_fact", TARGET, "open")
    assert 5 <= len(task["question"]) <= 500 and 5 <= len(task["reason"]) <= 500
    assert task["expected_answer"] == {"kind": "percent_composition"}
    [issue] = [i for i in issues(gen_world, made.listing_id) if i["status"] == "open"]
    assert (issue["field_path"], issue["error_code"], issue["human_task_id"]) == (
        f"attributes.{FIBER}",
        "MISSING_ATTRIBUTE",
        task["id"],
    )
    listed = await api.get("/api/v1/tasks", headers=bearer_for(gen_world, OPERATOR))
    assert listed.status_code == 200, listed.text
    assert str(task["id"]) in [t["id"] for t in listed.json()["data"]]


async def test_two_marketplace_listings_share_one_question(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    require(gen_world, FIBER)
    Pipeline(monkeypatch).overrides[FIBER] = None
    amazon = await generated(gen_world)
    flipkart = await generated(gen_world, sku=sku_of(gen_world, amazon), marketplace="flipkart")
    assert state(gen_world, amazon) == state(gen_world, flipkart) == ("blocked", "human_task_open")
    [task] = tasks(gen_world, product_of(gen_world, amazon))
    for made in (amazon, flipkart):
        linked = [i["human_task_id"] for i in issues(gen_world, made.listing_id)]
        assert linked == [task["id"]]


# ---- AC10 ----


async def test_a_regenerate_that_fills_the_fact_cancels_the_question(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    require(gen_world, FIBER)
    pipeline = Pipeline(monkeypatch)
    pipeline.overrides[FIBER] = None
    made = await generated(gen_world)
    [task] = tasks(gen_world, product_of(gen_world, made))
    pipeline.overrides.clear()  # `stub:ok` fills every required attribute
    await regenerate(gen_world, made)
    assert state(gen_world, made) == ("validated", None)
    [issue] = issues(gen_world, made.listing_id)
    assert (issue["status"], issue["resolution"]) == ("resolved", "regenerated")
    [after] = tasks(gen_world, product_of(gen_world, made))
    assert (after["id"], after["status"], after["cancelled_reason"]) == (
        task["id"],
        "cancelled",
        "issues_closed",
    )


async def test_the_question_waits_for_every_listing(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    require(gen_world, FIBER)
    pipeline = Pipeline(monkeypatch)
    pipeline.overrides[FIBER] = None
    amazon = await generated(gen_world)
    flipkart = await generated(gen_world, sku=sku_of(gen_world, amazon), marketplace="flipkart")
    product = product_of(gen_world, amazon)
    pipeline.overrides.clear()
    await regenerate(gen_world, amazon)
    assert [t["status"] for t in tasks(gen_world, product)] == ["open"]
    await regenerate(gen_world, flipkart)
    assert [(t["status"], t["cancelled_reason"]) for t in tasks(gen_world, product)] == [
        ("cancelled", "issues_closed")
    ]


# ---- what never asks ----


async def test_a_non_dictionary_key_stays_an_open_issue(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    require(gen_world, "fit_note")
    Pipeline(monkeypatch).overrides["fit_note"] = None
    made = await generated(gen_world)
    assert state(gen_world, made) == ("blocked", "corrections_exhausted")
    assert tasks(gen_world, product_of(gen_world, made)) == []
    [issue] = [i for i in issues(gen_world, made.listing_id) if i["status"] == "open"]
    assert (issue["field_path"], issue["human_task_id"]) == ("attributes.fit_note", None)


async def test_a_warning_never_asks(gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    require(gen_world, FIBER, severity="warning")
    Pipeline(monkeypatch).overrides[FIBER] = None
    made = await generated(gen_world)
    assert state(gen_world, made) == ("needs_review", None)  # still approvable
    assert tasks(gen_world, product_of(gen_world, made)) == []


# ---- CP-D15: a remap that does not apply becomes the question ----


async def test_a_value_that_does_not_fold_is_asked(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch).overrides["fit"] = "zigzag"
    made = await generated(gen_world)
    assert state(gen_world, made) == ("blocked", "human_task_open")
    [issue] = [i for i in issues(gen_world, made.listing_id) if i["status"] == "open"]
    assert (issue["field_path"], issue["error_code"]) == ("attributes.fit", "VALUE_NOT_ALLOWED")
    [task] = tasks(gen_world, product_of(gen_world, made))
    assert (task["target_path"], issue["human_task_id"]) == ("facts.attributes.fit", task["id"])


async def test_a_value_that_folds_is_fixed_and_never_asked(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch).overrides["fit"] = "relaxed_"
    made = await generated(gen_world)
    assert state(gen_world, made) == ("validated", None)
    assert tasks(gen_world, product_of(gen_world, made)) == []


# ---- D19: another lane's task at the same fact ----


async def test_a_task_another_lane_opened_is_reused(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    require(gen_world, FIBER)
    Pipeline(monkeypatch).overrides[FIBER] = None
    tenant = gen_world.tenant("acme")
    sku = "KURTA-002"
    product = gen_world.product("acme", sku)
    lane = f.human_task(gen_world.conn, tenant, product_id=product, target_path=TARGET)
    made = await generated(gen_world, sku=sku)
    assert [t["id"] for t in tasks(gen_world, product)] == [lane]
    assert [i["human_task_id"] for i in issues(gen_world, made.listing_id)] == [lane]
    assert state(gen_world, made) == ("blocked", "human_task_open")


async def test_an_unlinked_lane_task_outlives_a_clean_regenerate(gen_world: GenWorld) -> None:
    tenant = gen_world.tenant("acme")
    sku = "KURTA-002"
    product = gen_world.product("acme", sku)
    lane = f.human_task(gen_world.conn, tenant, product_id=product, target_path=TARGET)
    made = await generated(gen_world, sku=sku)
    await regenerate(gen_world, made)
    assert state(gen_world, made) == ("validated", None)  # a question alone never blocks
    assert [(t["id"], t["status"]) for t in tasks(gen_world, product)] == [(lane, "open")]


# ---- the edit check point ----


async def test_an_edit_opens_and_then_closes_the_question(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    require(gen_world, FIBER)
    body = {
        "content": {
            "title": "Cotton Kurta Set for Women",
            "bullets": ["Soft cotton fabric"],
            "description": "A cotton kurta set with a straight fit.",
        },
        "attributes": {"color": "Black", "fabric_type": "Cotton"},
    }
    headers = {**bearer_for(gen_world, OPERATOR), "If-Match": f'"{made.version_id}"'}
    first = await api.patch(
        f"/api/v1/listings/{made.listing_id}/content", headers=headers, json=body
    )
    assert first.status_code == 200, first.text
    assert state(gen_world, made) == ("blocked", "human_task_open")
    [task] = tasks(gen_world, product_of(gen_world, made))
    assert task["status"] == "open"
    body["attributes"] = {**body["attributes"], FIBER: "100% cotton"}  # type: ignore[dict-item]
    headers["If-Match"] = f'"{first.json()["current_version_id"]}"'
    second = await api.patch(
        f"/api/v1/listings/{made.listing_id}/content", headers=headers, json=body
    )
    assert second.status_code == 200, second.text
    assert state(gen_world, made)[0] == "validated"
    [after] = tasks(gen_world, product_of(gen_world, made))
    assert (after["status"], after["cancelled_reason"]) == ("cancelled", "issues_closed")


# ---- replay ----


async def test_a_replayed_persist_writes_one_task(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation_persist import make_persist as real
    from app.workers import generation

    require(gen_world, FIBER)
    Pipeline(monkeypatch).overrides[FIBER] = None
    plans: list[Any] = []

    def keep(plan: Any) -> Any:
        plans.append(plan)
        return real(plan)

    monkeypatch.setattr(generation, "make_persist", keep)
    made = await generated(gen_world)
    async with tenant_uow(gen_world.tenant("acme")) as uow:
        await real(plans[-1])(uow)
    assert len(tasks(gen_world, product_of(gen_world, made))) == 1
