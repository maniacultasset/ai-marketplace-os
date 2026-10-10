"""S22 T11, T12 (AC1, AC4-AC7, AC9, AC10; S20 C2, C3, C7): `FlipkartAdapter` over the in-process
fake Flipkart listings API (no network)."""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import date
from typing import Any

import httpx
import pytest
from tests.adapters.flipkart_support import (
    FakeTokens,
    fk_family,
    fk_item,
    flipkart_adapter,
    listing,
)
from tests.support.fake_marketplace import CANARY, build_fake
from tests.support.fault_transport import FaultTransport


async def _control(app: Any, **body: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        await client.post("/__control", json=body)


def _calls(app: Any) -> list[dict[str, Any]]:
    return [c for c in app.state.fake.calls if c["marketplace"] == "flipkart"]


def _status(result: Any) -> dict[str, str]:
    return {f.field: f.status for f in result.fields}


# ---- T11: create, results, families, chunks ----


async def test_flipkart_partial_result() -> None:
    """2 of 5 attributes rejected: their paths fail with the raw code, the rest succeed."""
    app = build_fake()
    body = listing(fit="slim", sleeve_length="half", neckline="round")
    sent = fk_item(body, sku="FK-P1")
    await _control(app, fk_errors={"FK-P1": {"Colour": "FK-COLOUR-1", "Fit": "FK-FIT-2"}})
    result = await flipkart_adapter(app).create_listing(sent)
    assert result.status == "partially_published"
    assert result.marketplace_state == "partially_live"
    status = _status(result)
    failed = sorted(k for k, v in status.items() if v == "failed")
    assert failed == ["attributes.color", "attributes.fit"]
    for path in ("attributes.fabric_type", "attributes.neckline", "attributes.sleeve_length"):
        assert status[path] == "succeeded"
    errors = {f.field: f.error for f in result.fields if f.error is not None}
    assert errors["attributes.color"].raw_code == "FK-COLOUR-1"
    assert errors["attributes.color"].source.value == "flipkart_adapter"
    assert result.error is not None and result.error.error_code == "API_REJECTED_PARTIAL"
    (stored,) = app.state.fake.fk_items.values()
    assert "Colour" not in stored["attributes"] and "Fabric" in stored["attributes"]


async def test_flipkart_batch_one_sku_rejected() -> None:
    app = build_fake()
    sent = fk_family(style="FKB")
    rejected = sent.items[1].seller_sku
    await _control(app, fk_reject=[rejected])
    result = await flipkart_adapter(app).create_listing(sent)
    assert result.status == "partially_published"
    states = {i.seller_sku: i.marketplace_state for i in result.items}
    assert states[rejected] == "rejected"
    assert sum(1 for s in states.values() if s == "processing") == 3
    bad = next(i for i in result.items if i.seller_sku == rejected)
    assert any(f.error and f.error.raw_code == "FK-REJECTED" for f in bad.fields)
    assert len(_calls(app)) == 1  # one batch request


async def test_flipkart_all_rejected_failed() -> None:
    app = build_fake()
    sent = fk_family()
    await _control(app, fk_reject=[u.seller_sku for u in sent.items])
    result = await flipkart_adapter(app).create_listing(sent)
    assert result.status == "failed" and result.marketplace_state == "rejected"
    assert result.error is not None and result.error.error_code == "PUBLISH_FAILED"
    assert app.state.fake.fk_items == {}


async def test_flipkart_warning_succeeded() -> None:
    app = build_fake()
    await _control(app, fk_warnings={"FK-W": {"title": "FK-W1"}})
    result = await flipkart_adapter(app).create_listing(fk_item(sku="FK-W"))
    assert result.status == "published"
    (title,) = [f for f in result.fields if f.field == "content.title"]
    assert title.status == "succeeded"
    assert title.error is not None and title.error.severity == "warning"


async def test_flipkart_error_without_attribute_to_listing() -> None:
    app = build_fake()
    await _control(app, fk_reject=["FK-L"])
    result = await flipkart_adapter(app).create_listing(fk_item(sku="FK-L"))
    assert result.status == "failed"
    (item,) = result.items
    listing_field = [f for f in item.fields if f.field == "listing"]
    assert listing_field and listing_field[0].status == "failed"
    assert listing_field[0].error is not None and listing_field[0].error.raw_code == "FK-REJECTED"


async def test_flipkart_unknown_error() -> None:
    """An unmapped code: MARKETPLACE_UNKNOWN_ERROR, class unknown, user_action, the raw code kept
    and the raw text scrubbed of a token."""
    app = build_fake()
    tokens = FakeTokens(app)
    leaked = tokens.token
    assert leaked.startswith(CANARY)
    await _control(app, fk_reject_attribute={"title": ["FK-NEW-999", f"Odd title, token {leaked}"]})
    result = await flipkart_adapter(app, tokens=tokens).create_listing(fk_item(sku="FK-U"))
    (title,) = [f for f in result.fields if f.field == "content.title"]
    error = title.error
    assert error is not None and error.error_code == "MARKETPLACE_UNKNOWN_ERROR"
    assert error.error_class == "unknown" and error.retry_class == "user_action"
    assert error.raw_code == "FK-NEW-999"
    assert leaked not in result.model_dump_json()


async def test_flipkart_error_map_row_wins() -> None:
    from app.adapters.error_map import ErrorMapRow
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import ErrorClass, RetryClass, Severity

    row = ErrorMapRow(
        marketplace="flipkart",
        match_on="code",
        raw_value="FAKE-FK-COLOUR",
        version=1,
        error_code=ErrorCode.VALUE_NOT_ALLOWED,
        error_class=ErrorClass.INVALID_VALUE,
        retry_class=RetryClass.USER_ACTION,
        severity=Severity.ERROR,
        auto_fixable=False,
        fix_action=None,
        field_hint=None,
        plain_text="{field} is not allowed on {marketplace}.",
        seller_action="Pick an allowed colour.",
    )
    app = build_fake()
    await _control(app, fk_errors={"FK-M": {"Colour": "FAKE-FK-COLOUR"}})
    result = await flipkart_adapter(app, rows=[row]).create_listing(fk_item(sku="FK-M"))
    (colour,) = [f for f in result.fields if f.field == "attributes.color"]
    assert colour.error is not None and colour.error.error_code == "VALUE_NOT_ALLOWED"
    assert colour.error.message.startswith("attributes.color is not allowed on flipkart.")
    assert colour.error.suggestion == "Pick an allowed colour."


async def test_flipkart_family_no_parent_item() -> None:
    app = build_fake()
    sent = fk_family(style="KFAM")
    result = await flipkart_adapter(app).create_listing(sent)
    assert result.status == "published"
    assert result.external_id is None
    assert [i.seller_sku for i in result.items] == [u.seller_sku for u in sent.items]
    assert len(result.items) == 4 and "KFAM" not in {i.seller_sku for i in result.items}
    assert all(i.external_id and i.external_id.startswith("LST") for i in result.items)
    assert result.submission_ref is not None and result.submission_ref.startswith("fk-req-")
    (call,) = _calls(app)
    skus = [entry["sku"] for entry in json.loads(call["body"])["listings"]]
    assert skus == [u.seller_sku for u in sent.items]


async def test_flipkart_standalone_external_id() -> None:
    app = build_fake()
    result = await flipkart_adapter(app).create_listing(fk_item(sku="FK-S"))
    (item,) = result.items
    assert result.external_id == item.external_id and item.external_id is not None


def _twelve() -> Any:
    rows = [{"size": s, "color": c} for s in ("XS", "S", "M", "L", "XL", "XXL") for c in ("navy blue", "maroon")]  # fmt: skip
    return fk_family(style="F12", children=rows)


async def test_flipkart_chunk2_429_partial() -> None:
    """12 children: chunk 1 (10) applied, chunk 2 429 past the budget -> partially published,
    chunk-2 items failed RATE_LIMITED."""
    app = build_fake()
    sent = _twelve()
    adapter = flipkart_adapter(app)
    original = adapter._client.create
    seen = 0

    async def create(listings: Any) -> Any:
        nonlocal seen
        seen += 1
        if seen == 2:
            await _control(app, fk_429=10)
        return await original(listings)

    adapter._client.create = create
    result = await adapter.create_listing(sent)
    assert result.status == "partially_published"
    assert result.error is not None and result.error.error_code == "RATE_LIMITED"
    states = [i.marketplace_state for i in result.items]
    assert states == ["processing"] * 10 + ["not_submitted"] * 2
    for late in result.items[10:]:
        codes = {f.error.error_code for f in late.fields if f.error is not None}
        assert codes == {"RATE_LIMITED"}
    assert len(app.state.fake.fk_items) == 10


async def test_flipkart_chunk2_5xx_ambiguous_with_completed() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    adapter = flipkart_adapter(app)
    original = adapter._client.create
    seen = 0

    async def create(listings: Any) -> Any:
        nonlocal seen
        seen += 1
        if seen == 2:
            await _control(app, fk_after_write=502)
        return await original(listings)

    adapter._client.create = create
    with pytest.raises(MarketplaceAmbiguous) as caught:
        await adapter.create_listing(_twelve())
    assert len(caught.value.completed) == 10
    assert caught.value.report.error_code == "MARKETPLACE_UNCONFIRMED"


async def test_flipkart_first_chunk_429_raised() -> None:
    from app.adapters.base import RateLimited

    app = build_fake()
    await _control(app, fk_429=10)
    with pytest.raises(RateLimited) as caught:
        await flipkart_adapter(app).create_listing(fk_item())
    assert caught.value.completed == ()


async def test_flipkart_blocking_error_no_http() -> None:
    app = build_fake()
    result = await flipkart_adapter(app).create_listing(fk_item(listing(color="ultraviolet")))
    assert result.status == "failed" and result.marketplace_state == "not_submitted"
    assert result.error is not None and result.error.error_code == "PUBLISH_FAILED"
    assert _calls(app) == []
    unmapped = await flipkart_adapter(app).create_listing(fk_item(template=None))
    assert unmapped.status == "failed" and _calls(app) == []


async def test_flipkart_get_states() -> None:
    from app.adapters.base import ListingNotFound

    app = build_fake()
    adapter = flipkart_adapter(app)
    await adapter.create_listing(fk_item(sku="FK-G1"))
    got = await adapter.get_listing("FK-G1")
    assert got.status == "published" and got.marketplace_state == "processing"
    assert got.external_id is not None and got.external_id.startswith("LST")
    assert "content.title" in _status(got) and "images[1]" in _status(got)
    await _control(app, fk_state={"FK-G1": "ACTIVE"})
    assert (await adapter.get_listing("FK-G1")).marketplace_state == "live"
    with pytest.raises(ListingNotFound):
        await adapter.get_listing("FK-NONE")


async def test_flipkart_performance_range() -> None:
    adapter = flipkart_adapter(build_fake())
    with pytest.raises(ValueError):
        await adapter.get_performance(uuid.uuid4(), date(2026, 2, 1), date(2026, 1, 1))
    with pytest.raises(ValueError):
        await adapter.get_performance(uuid.uuid4(), date(2025, 1, 1), date(2026, 6, 1))
    with pytest.raises(NotImplementedError):
        await adapter.get_performance(uuid.uuid4(), date(2026, 1, 1), date(2026, 1, 2))


async def test_flipkart_attempt_passed_through() -> None:
    app = build_fake()
    sent = dataclasses.replace(fk_item(), attempt=3)
    assert (await flipkart_adapter(app).create_listing(sent)).attempt == 3


# ---- T12: update, replay, auth ----


async def test_flipkart_update_sends_only_given_fields() -> None:
    app = build_fake()
    adapter = flipkart_adapter(app)
    sent = fk_item(sku="FK-UP")
    await adapter.create_listing(sent)
    before = dict(next(iter(app.state.fake.fk_items.values()))["attributes"])
    body = sent.listing
    changed = body.model_copy(
        update={
            "content": body.content.model_copy(update={"title": "New title", "description": "X"}),
            "attributes": {**body.attributes, "color": "maroon"},
        }
    )
    update = dataclasses.replace(sent, listing=changed)
    result = await adapter.update_listing(update, fields=["content.title", "attributes.color"])
    assert [f.field for f in result.fields] == ["attributes.color", "content.title"]
    assert all(f.status == "succeeded" for f in result.fields)
    last = _calls(app)[-1]
    assert last["path"].endswith("/listings/v3/update")
    (entry,) = json.loads(last["body"])["listings"]
    assert entry["sku"] == "FK-UP" and sorted(entry["attributes"]) == ["Colour", "title"]
    after = next(iter(app.state.fake.fk_items.values()))["attributes"]
    changed_names = sorted(k for k in after if after[k] != before.get(k))
    assert changed_names == ["Colour", "title"]
    assert after["description"] == before["description"]


async def test_flipkart_update_shared_keywords_full_value() -> None:
    app = build_fake()
    adapter = flipkart_adapter(app)
    sent = fk_item(sku="FK-KW")
    await adapter.create_listing(sent)
    result = await adapter.update_listing(sent, fields=["keywords.hidden_terms"])
    assert [f.field for f in result.fields] == ["keywords.hidden_terms", "keywords.search_terms"]
    (entry,) = json.loads(_calls(app)[-1]["body"])["listings"]
    terms = entry["attributes"]["keywords"][0]["value"]
    for term in [*sent.listing.keywords.search_terms, *sent.listing.keywords.hidden_terms]:
        assert term in terms


async def test_flipkart_update_named_child_only() -> None:
    app = build_fake()
    adapter = flipkart_adapter(app)
    sent = fk_family()
    await adapter.create_listing(sent)
    target = sent.items[2].seller_sku
    result = await adapter.update_listing(sent, fields={target: ["content.description"]})
    assert [i.seller_sku for i in result.items] == [target]
    (entry,) = json.loads(_calls(app)[-1]["body"])["listings"]
    assert entry["sku"] == target and list(entry["attributes"]) == ["description"]


async def test_flipkart_update_unknown_path_rejected() -> None:
    app = build_fake()
    with pytest.raises(ValueError):
        await flipkart_adapter(app).update_listing(fk_item(), fields=["attributes.nope"])
    assert _calls(app) == []


async def test_flipkart_update_empty_no_call() -> None:
    app = build_fake()
    with pytest.raises(ValueError):
        await flipkart_adapter(app).update_listing(fk_item(), fields=[])
    assert _calls(app) == []


async def test_flipkart_update_unknown_sku_created() -> None:
    """S22b F3: an update of a SKU Flipkart never created answers LISTING_NOT_FOUND; the adapter
    sends that SKU whole through the create route in the same call, never `ListingNotFound`."""
    app = build_fake()
    result = await flipkart_adapter(app).update_listing(
        fk_item(sku="NOPE"), fields=["content.title"]
    )
    paths = [_route(c) for c in _calls(app)]
    assert paths == ["update", "create"]
    assert result.status == "published" and result.items[0].external_id is not None
    assert any(k[1] == "NOPE" for k in app.state.fake.fk_items)


def _route(call: dict[str, Any]) -> str:
    return "update" if call["path"].endswith("/update") else "create"


def _attribute_names(sent: Any, sku: str) -> list[str]:
    from app.adapters.flipkart.mapping import build

    mapped, _ = build(sent)
    return sorted(next(m for m in mapped if m.unit.seller_sku == sku).payload["attributes"])


async def test_flipkart_family_one_child_all_failed_is_partial() -> None:
    """S22b F4: one child refused on every attribute while Flipkart created the other three is
    `partially_published`, not `failed`; the other children keep their succeeded fields."""
    app = build_fake()
    sent = fk_family(style="FKALL")
    bad = sent.items[1].seller_sku
    app.state.fake.fk_errors[bad] = dict.fromkeys(_attribute_names(sent, bad), "FK-QUOTA")
    result = await flipkart_adapter(app).create_listing(sent)
    assert result.status == "partially_published", result.status
    assert result.marketplace_state == "partially_live"
    assert result.error is not None and result.error.error_code == "API_REJECTED_PARTIAL"
    by_sku = {i.seller_sku: i for i in result.items}
    assert by_sku[bad].marketplace_state == "rejected"
    for sku, one in by_sku.items():
        if sku != bad:
            assert one.external_id is not None
            assert all(f.status != "failed" for f in one.fields), sku
            assert any(f.status == "succeeded" for f in one.fields), sku


async def test_flipkart_update_listing_path_creates_refused_child() -> None:
    """S22b F3: a child refused at create (failed path `listing`) is sent through the create
    route by `update_listing`; the children Flipkart holds are not re-sent."""
    app = build_fake()
    adapter = flipkart_adapter(app)
    sent = fk_family(style="FKRC")
    bad = sent.items[2].seller_sku
    await _control(app, fk_reject=[bad])
    first = await adapter.create_listing(sent)
    assert first.status == "partially_published"
    failed = {f.field for i in first.items if i.seller_sku == bad for f in i.fields}
    assert "listing" in failed
    app.state.fake.fk_reject.clear()
    before = len(_calls(app))
    result = await adapter.update_listing(sent, fields={bad: ["listing"]})
    calls = _calls(app)[before:]
    assert [_route(c) for c in calls] == ["create"]
    (entry,) = json.loads(calls[0]["body"])["listings"]
    assert entry["sku"] == bad
    assert result.status == "published", result.error
    assert [i.seller_sku for i in result.items] == [bad]
    assert result.items[0].external_id is not None
    assert any(k[1] == bad for k in app.state.fake.fk_items)


async def test_flipkart_update_variant_path_creates_child() -> None:
    app = build_fake()
    sent = fk_family(style="FKVR")
    target = sent.items[0].seller_sku
    result = await flipkart_adapter(app).update_listing(
        sent, fields={target: [f"variants[{target}]"]}
    )
    assert [_route(c) for c in _calls(app)] == ["create"]
    assert result.status == "published"


async def test_flipkart_replay_same_outcome() -> None:
    app = build_fake()
    adapter = flipkart_adapter(app)
    sent = fk_item(sku="FK-R", key="same-key")
    first = await adapter.create_listing(sent)
    again = await adapter.create_listing(sent)
    assert [(f.field, f.status) for f in first.fields] == [
        (f.field, f.status) for f in again.fields
    ]
    assert len(app.state.fake.fk_items) == 1


async def test_flipkart_auth_refresh_once_then_ok() -> None:
    app = build_fake()
    tokens = FakeTokens(app)
    await _control(app, fk_auth={"mode": "expired", "times": 1})
    result = await flipkart_adapter(app, tokens=tokens).create_listing(fk_item())
    assert result.status == "published" and tokens.refreshes == 1


async def test_flipkart_auth_second_401_auth_expired() -> None:
    from app.adapters.base import AuthExpired

    app = build_fake()
    tokens = FakeTokens(app)
    await _control(app, fk_auth={"mode": "expired", "times": 2})
    with pytest.raises(AuthExpired):
        await flipkart_adapter(app, tokens=tokens).create_listing(fk_item())
    assert tokens.refreshes == 1 and app.state.fake.fk_items == {}


async def test_flipkart_auth_refresh_rejected_auth_expired() -> None:
    from app.adapters.base import AuthExpired

    app = build_fake()
    tokens = FakeTokens(app, refuse_refresh=True)
    await _control(app, fk_auth={"mode": "expired", "times": 1})
    with pytest.raises(AuthExpired):
        await flipkart_adapter(app, tokens=tokens).create_listing(fk_item())


async def test_flipkart_auth_403_after_fresh_permission_denied() -> None:
    from app.adapters.base import PermissionDenied

    app = build_fake()
    tokens = FakeTokens(app)
    await _control(app, fk_auth={"mode": "denied", "times": 2})
    with pytest.raises(PermissionDenied) as caught:
        await flipkart_adapter(app, tokens=tokens).create_listing(fk_item())
    assert caught.value.report.error_code == "ACCOUNT_PERMISSION_DENIED"


async def test_flipkart_auth_refresh_called_once_per_session() -> None:
    app = build_fake()
    tokens = FakeTokens(app)
    adapter = flipkart_adapter(app, tokens=tokens)
    await _control(app, fk_auth={"mode": "expired", "times": 1})
    await adapter.create_listing(fk_item(sku="FK-A1"))
    await adapter.create_listing(fk_item(sku="FK-A2"))
    assert tokens.refreshes == 1


async def test_flipkart_adapter_ambiguous_timeout_completed_empty() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    fault = FaultTransport(httpx.ASGITransport(app), after_write=httpx.ReadTimeout("x"))
    with pytest.raises(MarketplaceAmbiguous) as caught:
        await flipkart_adapter(app, transport=fault).create_listing(fk_item())
    assert caught.value.completed == ()
