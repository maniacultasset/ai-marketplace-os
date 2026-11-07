"""SI3 T12 AC9b (design §2.10, CP-D17): Amazon and Flipkart generated together. Both runs read the
same (empty) photo record and both call vision; only the first persist applies its decision, so
both listings stay approvable."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import make_generating
from .photo_support import SKU, PhotoWorld, approve, photo_world, seller_patch, spy_pipeline

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["photo_world"]
WAIT_S = 20


async def _together(
    pw: PhotoWorld,
    monkeypatch: pytest.MonkeyPatch,
    *,
    flipkart_change: Any = None,
    amazon_change: Any = None,
    while_paused: Any = None,
) -> tuple[Any, Any, list[str]]:
    """Both jobs past their read transaction before either persists; Amazon persists first."""
    from app.workers import wrapper
    from app.workers.tasks import register_bodies

    entered: list[str] = []
    order: list[str] = []
    both_read = asyncio.Event()
    amazon_done = asyncio.Event()

    async def around(gctx: Any, deps: Any, real: Any) -> Any:
        market = gctx.marketplace.value
        entered.append(market)
        if len(entered) == 2:
            if while_paused is not None:
                await while_paused()
            both_read.set()
        await asyncio.wait_for(both_read.wait(), WAIT_S)
        if market == "flipkart":
            await asyncio.wait_for(amazon_done.wait(), WAIT_S)
        result = await real(gctx, deps)
        if market == "flipkart" and flipkart_change is not None:
            result = flipkart_change(gctx, result)
        if market == "amazon" and amazon_change is not None:
            result = amazon_change(gctx, result)
        return result

    spy_pipeline(pw, monkeypatch, around)
    amazon = await make_generating(pw.world, sku=SKU, instructions="stub:photo_ok")
    flipkart = await make_generating(
        pw.world, sku=SKU, instructions="stub:photo_ok", marketplace="flipkart"
    )
    register_bodies()

    async def run_amazon() -> None:
        await wrapper.run_job(str(amazon.job_id))
        order.append("amazon")
        amazon_done.set()

    async def run_flipkart() -> None:
        await wrapper.run_job(str(flipkart.job_id))
        order.append("flipkart")

    await asyncio.wait_for(asyncio.gather(run_amazon(), run_flipkart()), WAIT_S * 3)
    return amazon, flipkart, order


def _ai(pw: PhotoWorld) -> dict[str, Any]:
    return {k: v for k, v in pw.attributes().items() if v.get("source") == "ai"}


async def test_the_second_persist_is_superseded(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world
    amazon, flipkart, order = await _together(pw, monkeypatch)
    assert order == ["amazon", "flipkart"]
    assert all(ctx.photo_targets for ctx in pw.seen)  # both runs saw no record: both called vision
    assert len(pw.photo_records(amazon.job_id)) == 1
    assert len(pw.photo_records(flipkart.job_id)) == 1
    assert "photo_set" in pw.job(amazon.job_id)["result_json"]
    assert "photo_set" not in pw.job(flipkart.job_id)["result_json"]
    run_id = {f["agent_run_id"] for f in _ai(pw).values()}
    assert run_id == {str(pw.photo_records(amazon.job_id)[0]["id"])}
    assert len(pw.audit("product.fact_updated")) == 1
    assert (await approve(pw, amazon.listing_id)).http_status == 200
    assert (await approve(pw, flipkart.listing_id)).http_status == 200


def _without_occasion(gctx: Any, result: Any) -> Any:
    kept = [o for o in result.photo_observations if o.key != "occasion"]
    return result.model_copy(update={"photo_observations": kept})


async def test_different_values_still_apply_only_the_first(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world

    def other_neckline(gctx: Any, result: Any) -> Any:
        targets = {t.key: t for t in gctx.photo_targets}
        changed = [
            (
                o.model_copy(update={"value": targets["neckline"].allowed_values[1]})
                if o.key == "neckline"
                else o
            )
            for o in result.photo_observations
        ]
        return result.model_copy(update={"photo_observations": changed})

    amazon, flipkart, _ = await _together(
        pw, monkeypatch, flipkart_change=other_neckline, amazon_change=_without_occasion
    )
    amazon_neck = {t.key: t for t in pw.seen[0].photo_targets}["neckline"].allowed_values[0]
    assert pw.attributes()["neckline"]["value"] == amazon_neck
    # Flipkart saw `occasion` too, but its decision was superseded: nothing moved the facts
    assert "occasion" not in pw.attributes()
    assert (await approve(pw, amazon.listing_id)).http_status == 200
    assert (await approve(pw, flipkart.listing_id)).http_status == 200


async def test_a_seller_edit_while_both_run_voids_both(
    photo_world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = photo_world

    async def edit() -> None:
        await seller_patch(pw, {"attributes": {"fit": "slim"}})

    amazon, flipkart, _ = await _together(pw, monkeypatch, while_paused=edit)
    assert pw.attributes()["fit"]["source"] == "seller"
    first = await approve(pw, amazon.listing_id)
    second = await approve(pw, flipkart.listing_id)
    assert (first.http_status, second.http_status) == (409, 409)
