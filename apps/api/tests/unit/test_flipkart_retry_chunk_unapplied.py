"""S22b F6, F7 (invariant 10, L5300-5304): a family child that never reached Flipkart keeps a
failed `listing` marker in every result, so S21's retry, which reads the newest verdict per
(item, field) and keeps the failed ones (submissions.py `retry_payload`), always sends it through
the create route; the update route would answer LISTING_NOT_FOUND and the attempt would burn as
SUBMISSION_NOT_APPLIED.

- F6: a create chunk refused 429 after an earlier chunk applied, in `create_listing` and in the
  create step of `update_listing`; the children of later chunks too.
- F7: a never-created child refused again at the retry's create."""

from __future__ import annotations

import json
from typing import Any

import httpx
from tests.adapters.flipkart_support import fk_family, flipkart_adapter
from tests.support.fake_marketplace import build_fake


async def _control(app: Any, **body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        await client.post("/__control", json=body)


def _family(style: str, colours: tuple[str, ...]) -> Any:
    sizes = ("XS", "S", "M", "L", "XL", "XXL")
    return fk_family(
        style=style, children=[{"size": s, "color": c} for c in colours for s in sizes]
    )


class _Ledger:
    """S21's retry ledger in memory: the newest verdict per (seller_sku, field)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}

    def record(self, result: Any) -> None:
        for one in result.items:
            for f in one.fields:
                self.rows[(one.seller_sku, f.field)] = f.status

    def retry(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for (sku, path), status in sorted(self.rows.items()):
            if status == "failed":
                out.setdefault(sku, []).append(path)
        return out


def _flipkart_calls(app: Any) -> list[dict[str, Any]]:
    return [c for c in app.state.fake.calls if c["marketplace"] == "flipkart"]


def _route(call: dict[str, Any]) -> str:
    return "update" if call["path"].endswith("/update") else "create"


def _throttle_create(app: Any, adapter: Any, on: int) -> None:
    """The fake answers 429 (past the retry budget) from the `on`-th create call."""
    original = adapter._client.create
    seen = 0

    async def create(listings: Any) -> Any:
        nonlocal seen
        seen += 1
        if seen == on:
            await _control(app, fk_429=10)
        return await original(listings)

    adapter._client.create = create


async def test_flipkart_retry_creates_children_of_an_undelivered_chunk() -> None:
    app = build_fake()
    sent = _family("FKCH", ("navy blue", "maroon"))
    adapter = flipkart_adapter(app)
    original = adapter._client.create
    _throttle_create(app, adapter, on=2)
    first = await adapter.create_listing(sent)
    assert first.status == "partially_published"
    ledger = _Ledger()
    ledger.record(first)
    retry = ledger.retry()
    late = {i.seller_sku for i in first.items[10:]}
    assert set(retry) == late
    assert all("listing" in paths for paths in retry.values())
    app.state.fake.fk_429 = 0
    adapter._client.create = original
    result = await adapter.update_listing(sent, fields=retry)
    assert result.status == "published", result.error
    assert {k[1] for k in app.state.fake.fk_items} >= late


async def test_flipkart_retry_creates_children_of_a_later_skipped_chunk() -> None:
    """Children of a chunk after the refused one are `skipped` on their paths but fail
    `listing`, so the retry includes them."""
    app = build_fake()
    sent = _family("FKCL", ("navy blue", "maroon", "black", "white"))
    adapter = flipkart_adapter(app)
    original = adapter._client.create
    _throttle_create(app, adapter, on=2)
    first = await adapter.create_listing(sent)
    assert first.status == "partially_published"
    ledger = _Ledger()
    ledger.record(first)
    later = {i.seller_sku for i in first.items[20:]}
    assert later
    retry = ledger.retry()
    assert set(retry) == {i.seller_sku for i in first.items[10:]}
    for sku in later:
        assert retry[sku] == ["listing"], sku
    app.state.fake.fk_429 = 0
    adapter._client.create = original
    result = await adapter.update_listing(sent, fields=retry)
    assert result.status == "published", result.error
    assert {k[1] for k in app.state.fake.fk_items} >= later


async def test_flipkart_retry_create_step_throttled_keeps_the_marker() -> None:
    """The retry's update batch and first create batch apply, then its second create batch is
    refused 429: the children of that batch and of the batch after it still fail `listing`
    (a later batch's `skipped` must not replace it), and the next retry creates them."""
    app = build_fake()
    colours = ("navy blue", "maroon", "black", "white", "red", "green")
    sent = _family("FKCT", colours)
    adapter = flipkart_adapter(app)
    original = adapter._client.create
    _throttle_create(app, adapter, on=2)
    ledger = _Ledger()
    ledger.record(await adapter.create_listing(sent))
    app.state.fake.fk_429 = 0
    adapter._client.create = original
    retry = ledger.retry()
    assert len(retry) == len(sent.items) - 10
    created = sent.items[0].seller_sku
    held = await adapter.get_listing(created)
    retry[created] = [f.field for f in held.fields][:1]
    assert retry[created]
    _throttle_create(app, adapter, on=2)
    second = await adapter.update_listing(sent, fields=retry)
    assert second.status == "partially_published"
    ledger.record(second)
    app.state.fake.fk_429 = 0
    adapter._client.create = original
    third = ledger.retry()
    missing = {i.seller_sku for i in sent.items[20:]}
    assert set(third) == missing
    assert all("listing" in third[s] for s in missing)
    result = await adapter.update_listing(sent, fields=third)
    assert result.status == "published", result.error
    assert {k[1] for k in app.state.fake.fk_items} >= missing


async def test_flipkart_child_refused_twice_still_goes_to_create() -> None:
    """F7: a child refused at create, then refused again by the retry's create, keeps `listing`
    failed; the third attempt still uses the create route and creates it."""
    app = build_fake()
    adapter = flipkart_adapter(app)
    sent = fk_family(style="FKRR")
    bad = sent.items[1].seller_sku
    from app.adapters.flipkart.mapping import build

    mapped, _ = build(sent)
    names = sorted(next(m for m in mapped if m.unit.seller_sku == bad).payload["attributes"])
    refuse = {bad: dict.fromkeys(names, "FK-QUOTA")}
    await _control(app, fk_errors=refuse)
    ledger = _Ledger()
    first = await adapter.create_listing(sent)
    assert first.status == "partially_published"
    ledger.record(first)
    assert "listing" in ledger.retry()[bad]
    await _control(app, fk_errors=refuse)
    second = await adapter.update_listing(sent, fields={bad: ledger.retry()[bad]})
    assert second.status == "failed"
    ledger.record(second)
    assert ledger.rows[(bad, "listing")] == "failed"
    before = len(_flipkart_calls(app))
    result = await adapter.update_listing(sent, fields={bad: ledger.retry()[bad]})
    calls = _flipkart_calls(app)[before:]
    assert [_route(c) for c in calls] == ["create"]
    assert [e["sku"] for e in json.loads(calls[0]["body"])["listings"]] == [bad]
    assert result.status == "published", result.error
    assert any(k[1] == bad for k in app.state.fake.fk_items)
