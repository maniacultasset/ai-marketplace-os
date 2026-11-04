"""DW2 review round 3 re-check (tester): attacks on the F1/F3 update path and the F2 deep-schema
path. The create path must still block on a schema ERROR, never on a WARNING; an update with a
WARNING-only schema result is sent; the depth limit is fail-closed on both sides of the boundary."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from app.adapters.amazon import definitions, mapping, schema_check
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Severity
from tests.adapters.amazon_support import SELLER, amazon_adapter, amazon_item, listing
from tests.support.fake_marketplace import build_fake

from .test_amazon_schema_check import _five, built, kurta, ready, schema_errors


def _nested(depth: int, kind: str) -> dict[str, Any]:
    body = {
        "props": ('{"properties":{"a":' * depth) + "{}" + ("}}" * depth),
        "items": ('{"items":' * depth) + "{}" + ("}" * depth),
        "allOf": ('{"allOf":[' * depth) + "{}" + ("]}" * depth),
    }[kind]
    schema: dict[str, Any] = definitions.parse_schema(body.encode())
    assert definitions.canonical_digest(schema)
    return schema


async def test_create_still_blocked_by_a_schema_error() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    schema = kurta()
    schema["properties"]["item_name"]["items"]["properties"]["value"]["maxLength"] = 3
    sent = dataclasses.replace(_five(), product_type_schema=await ready(schema))
    result = await adapter.create_listing(sent)
    assert (SELLER, sent.seller_sku) not in app.state.fake.spapi_items
    assert result.error is not None
    assert any(
        e.field == "content.title" and (e.raw_code or "").startswith("ptd:")
        for e in result.error.errors
    )


async def test_create_not_blocked_by_a_warning_only_schema_result() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    schema = kurta()
    schema["required"] = [*schema["required"], "brand"]
    prepared = await ready(schema, enforced=False)
    sent = dataclasses.replace(_five(), product_type_schema=prepared)
    found = schema_errors(built(sent, prepared)[1])
    assert found and all(e.severity is Severity.WARNING for e in found)
    await adapter.create_listing(sent)
    assert (SELLER, sent.seller_sku) in app.state.fake.spapi_items


async def test_update_with_warning_only_schema_result_is_sent() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    await adapter.create_listing(sent)
    schema = kurta()
    schema["required"] = [*schema["required"], "brand"]
    prepared = await ready(schema, enforced=False)
    body = sent.listing
    changed = body.model_copy(
        update={"content": body.content.model_copy(update={"title": "Another new title"})}
    )
    update = dataclasses.replace(
        sent, listing=changed, idempotency_key="update-w", product_type_schema=prepared
    )
    result = await adapter.update_listing(update, fields=["content.title"])
    after = app.state.fake.spapi_items[(SELLER, sent.seller_sku)]["attributes"]
    assert after["item_name"][0]["value"] == "Another new title"
    assert {f.field: f.status for f in result.fields}["content.title"] == "succeeded"


def test_update_build_leaves_out_every_schema_item() -> None:
    # a mismatched product type is a schema WARNING in build(); schema=False drops it too
    sent = amazon_item(listing())
    other = dataclasses.replace(sent, product_type_schema=None)
    assert mapping.build(other, schema=False)[1] == mapping.build(other)[1]


@pytest.mark.parametrize(("kind", "ok", "bad"), [("props", 127, 128), ("allOf", 127, 128)])
async def test_depth_boundary_both_sides(kind: str, ok: int, bad: int) -> None:
    fine = await ready({"type": "object", **_nested(ok, kind)})
    assert [i.error_code for i in schema_check.check_item({}, fine, {})] == []
    deep = await ready({"type": "object", **_nested(bad, kind)})
    items = schema_check.check_item({}, deep, {})
    assert [(i.field, i.error_code) for i in items] == [("listing", ErrorCode.SCHEMA_INVALID)]


async def test_very_deep_items_chain_fails_closed() -> None:
    # 5000 nested "items" objects pass the refresh parser; the check must still fail closed
    deep = await ready(_nested(5000, "items"))
    items = schema_check.check_item({}, deep, {})
    assert [(i.field, i.error_code) for i in items] == [("listing", ErrorCode.SCHEMA_INVALID)]


async def test_deep_schema_through_build_is_one_blocking_error() -> None:
    prepared = await ready({"type": "object", **_nested(300, "props")})
    sent = amazon_item(listing())
    found = schema_errors(built(sent, prepared)[1])
    assert [(e.field, e.error_code, e.severity) for e in found] == [
        ("listing", ErrorCode.SCHEMA_INVALID, Severity.ERROR)
    ]
