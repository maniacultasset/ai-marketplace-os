"""S20 T11, T12 (AC1, AC2, AC4, AC6, AC9, AC10; D4, D5, D9; C2, C3, C6, C7, C8, C11, C16):
`AmazonAdapter` over the in-process fake SP-API (no network)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, FixAction, RetryClass, Severity
from tests.adapters.amazon_support import (
    SELLER,
    FakeTokens,
    amazon_adapter,
    amazon_family,
    amazon_item,
    listing,
)
from tests.support.fake_marketplace import build_fake
from tests.support.fault_transport import FaultTransport


def _five(**over: Any) -> Any:
    """A standalone listing that sends exactly five paths: three content fields, color and
    fabric_type (no keywords, no images)."""
    base = listing()
    body = base.model_copy(
        update={
            "attributes": {"color": "navy blue", "fabric_type": "cotton"},
            "keywords": base.keywords.model_copy(update={"search_terms": [], "hidden_terms": []}),
            "images": [],
        }
    )
    return amazon_item(body, **over)


def _issue(code: str, attribute: str | None, severity: str = "ERROR", message: str = "Bad.") -> dict[str, Any]:  # fmt: skip
    out: dict[str, Any] = {"code": code, "message": message, "severity": severity}
    if attribute is not None:
        out["attributeNames"] = [attribute]
    return out


def _fields(result: Any) -> dict[str, str]:
    return {f.field: f.status for f in result.fields}


class SkuFault(httpx.AsyncBaseTransport):
    """Answers (or raises) for one SKU, delegates the rest; counts requests in flight."""

    def __init__(self, inner: httpx.AsyncBaseTransport, sku: str, *, status: int | None = None, exc: Exception | None = None) -> None:  # fmt: skip
        self.inner, self.sku, self.status, self.exc = inner, sku, status, exc

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if unquote(request.url.path).endswith("/" + self.sku):
            if self.exc is not None:
                raise self.exc
            body = {"errors": [{"code": "Throttled", "message": "slow down"}]}
            return httpx.Response(self.status or 429, json=body, request=request)
        return await self.inner.handle_async_request(request)


# ---- T11: create, families, outcomes ----


async def test_amazon_partial_result() -> None:
    app = build_fake()
    sent = _five()
    app.state.fake.spapi_issues[sent.seller_sku] = [
        _issue("90220", "color", message="Color is invalid."),
        _issue("8541", "fabric_type", message="Fabric is invalid."),
    ]
    result = await amazon_adapter(app).create_listing(sent)
    assert result.status == "partially_published"
    statuses = _fields(result)
    assert sorted(statuses.values()) == ["failed", "failed", "succeeded", "succeeded", "succeeded"]
    failed = {f.field: f.error for f in result.fields if f.status == "failed"}
    assert set(failed) == {"attributes.color", "attributes.fabric_type"}
    color = failed["attributes.color"]
    assert color is not None and color.raw_code == "90220" and color.message == "Color is invalid."
    assert color.source == "amazon_adapter" and color.marketplace == "amazon"
    assert result.error is not None and result.error.error_code is ErrorCode.API_REJECTED_PARTIAL
    assert result.submission_ref and result.raw_response_digest
    (item,) = result.items
    assert item.seller_sku == sent.seller_sku and item.marketplace_state == "processing"


async def test_amazon_invalid_named_failed_rest_skipped() -> None:
    app = build_fake()
    sent = _five()
    app.state.fake.spapi_invalid.add(sent.seller_sku)
    app.state.fake.spapi_issues[sent.seller_sku] = [_issue("90220", "color")]
    result = await amazon_adapter(app).create_listing(sent)
    assert result.status == "failed"
    statuses = _fields(result)
    assert statuses.pop("attributes.color") == "failed"
    assert set(statuses.values()) == {"skipped"}
    assert result.items[0].marketplace_state == "rejected"
    assert app.state.fake.spapi_items == {}


async def test_amazon_warning_issue_succeeded() -> None:
    app = build_fake()
    sent = _five()
    app.state.fake.spapi_issues[sent.seller_sku] = [
        _issue("W1", "color", "WARNING"),
        _issue("I1", "item_name", "INFO"),
    ]
    result = await amazon_adapter(app).create_listing(sent)
    assert result.status == "published"
    color = next(f for f in result.fields if f.field == "attributes.color")
    assert color.status == "succeeded" and color.error is not None
    assert color.error.severity is Severity.WARNING
    title = next(f for f in result.fields if f.field == "content.title")
    assert title.error is None  # INFO is dropped


async def test_amazon_issue_without_attribute_to_listing() -> None:
    app = build_fake()
    sent = _five()
    app.state.fake.spapi_issues[sent.seller_sku] = [_issue("5665", None, message="Brand not approved.")]  # fmt: skip
    result = await amazon_adapter(app).create_listing(sent)
    listing_level = next(f for f in result.fields if f.field == "listing")
    assert listing_level.status == "failed" and listing_level.error is not None
    assert listing_level.error.raw_code == "5665"
    assert result.status == "partially_published"


async def test_amazon_issue_field_hint_from_error_map() -> None:
    from app.adapters.error_map import ErrorMapRow

    row = ErrorMapRow("amazon", "code", "18027", 1, ErrorCode.MISSING_ATTRIBUTE, ErrorClass.MEDIA_ERROR, RetryClass.USER_ACTION, Severity.ERROR, False, None, "images[0]", "Main image missing on {marketplace}.", "Upload a main image.")  # fmt: skip
    app = build_fake()
    sent = _five()
    app.state.fake.spapi_issues[sent.seller_sku] = [_issue("18027", None)]
    result = await amazon_adapter(app, rows=[row]).create_listing(sent)
    hinted = next(f for f in result.fields if f.field == "images[0]")
    assert hinted.status == "failed" and hinted.error is not None
    assert hinted.error.message.startswith("Main image missing on amazon.")


async def test_amazon_mapped_row_message() -> None:
    from app.adapters.error_map import ErrorMapRow

    row = ErrorMapRow("amazon", "code", "8541", 1, ErrorCode.VALUE_NOT_ALLOWED, ErrorClass.INVALID_VALUE, RetryClass.USER_ACTION, Severity.ERROR, True, FixAction.REMAP_VALUE, None, "{field} is not allowed on {marketplace}.", "Pick an allowed value.")  # fmt: skip
    app = build_fake()
    sent = _five()
    app.state.fake.spapi_issues[sent.seller_sku] = [_issue("8541", "color", message="Raw text.")]
    result = await amazon_adapter(app, rows=[row]).create_listing(sent)
    error = next(f.error for f in result.fields if f.field == "attributes.color")
    assert error is not None
    assert (
        error.message == "attributes.color is not allowed on amazon. Marketplace message: Raw text."
    )
    assert (
        error.suggestion == "Pick an allowed value."
        and error.error_code is ErrorCode.VALUE_NOT_ALLOWED
    )
    assert error.fix_action is FixAction.REMAP_VALUE  # reported, never run
    assert app.state.fake.submissions == 1


async def test_amazon_unknown_error() -> None:
    app = build_fake()
    sent = _five()
    app.state.fake.spapi_issues[sent.seller_sku] = [_issue("777", "color", message="Odd thing.")]
    result = await amazon_adapter(app).create_listing(sent)
    error = next(f.error for f in result.fields if f.field == "attributes.color")
    assert error is not None and error.error_code is ErrorCode.MARKETPLACE_UNKNOWN_ERROR
    assert error.raw_code == "777" and error.message == "Odd thing."
    assert error.error_class is ErrorClass.UNKNOWN and error.retry_class is RetryClass.USER_ACTION


async def test_amazon_family_one_call() -> None:
    app = build_fake()
    sent = amazon_family()
    result = await amazon_adapter(app).create_listing(sent)
    assert result.status == "published"
    assert [i.seller_sku for i in result.items] == [u.seller_sku for u in sent.units()]
    assert len(result.items) == 5
    assert [c["method"] for c in app.state.fake.calls] == ["PUT"] * 5
    assert result.submission_ref == "fake-submission-000001"  # the parent's


async def test_amazon_family_parent_invalid_children_skipped() -> None:
    app = build_fake()
    sent = amazon_family()
    app.state.fake.spapi_invalid.add(sent.seller_sku)
    result = await amazon_adapter(app).create_listing(sent)
    assert result.status == "failed"
    assert len(app.state.fake.calls) == 1
    for child in result.items[1:]:
        assert {f.status for f in child.fields} == {"skipped"}


async def test_amazon_family_child_5xx_ambiguous_with_completed() -> None:
    from app.adapters.base import MarketplaceAmbiguous

    app = build_fake()
    sent = amazon_family()
    third = sent.units()[3]
    app.state.fake.spapi_after_write[third.seller_sku] = 500
    with pytest.raises(MarketplaceAmbiguous) as caught:
        await amazon_adapter(app).create_listing(sent)
    assert [i.seller_sku for i in caught.value.completed] == [
        u.seller_sku for u in sent.units()[:3]
    ]
    assert caught.value.report.error_code is ErrorCode.MARKETPLACE_UNCONFIRMED
    assert len(app.state.fake.calls) == 4  # child 4 was never sent


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ({"status": 429}, ErrorCode.RATE_LIMITED),
        ({"status": 401}, ErrorCode.AUTH_EXPIRED),
        ({"exc": httpx.ConnectError("down")}, ErrorCode.MARKETPLACE_UNAVAILABLE),
    ],
    ids=["429_budget", "auth_expired", "connect_error"],
)
async def test_amazon_family_child_failure_not_definitive(fault: dict[str, Any], code: ErrorCode) -> None:  # fmt: skip
    app = build_fake()
    sent = amazon_family()
    units = sent.units()
    transport = SkuFault(httpx.ASGITransport(app), units[3].seller_sku, **fault)
    result = await amazon_adapter(app, transport=transport).create_listing(sent)
    assert result.status == "partially_published"
    assert result.error is not None and result.error.error_code is code
    assert [i.seller_sku for i in result.items] == [u.seller_sku for u in units]
    applied = result.items[:3]
    assert all(f.status != "failed" for i in applied for f in i.fields)
    child3 = result.items[3]
    assert {f.status for f in child3.fields} == {"failed"}
    assert {f.error.error_code for f in child3.fields if f.error} == {code}
    assert {f.status for f in result.items[4].fields} == {"skipped"}


async def test_amazon_standalone_429_budget_raises_rate_limited() -> None:
    from app.adapters.base import RateLimited

    app = build_fake()
    app.state.fake.spapi_429 = 10
    with pytest.raises(RateLimited) as caught:
        await amazon_adapter(app).create_listing(_five())
    assert caught.value.completed == ()


async def test_amazon_family_one_in_flight() -> None:
    app = build_fake()
    transport = FaultTransport(httpx.ASGITransport(app))
    await amazon_adapter(app, transport=transport).create_listing(amazon_family())
    assert transport.sent == 5 and transport.max_in_flight == 1


async def test_amazon_blocking_mapping_error_no_http() -> None:
    app = build_fake()
    sent = _five(template=None)
    result = await amazon_adapter(app).create_listing(sent)
    assert result.status == "failed" and app.state.fake.calls == []
    assert result.error is not None and result.error.error_code is ErrorCode.PUBLISH_FAILED
    assert [e.error_code for e in result.error.errors] == [ErrorCode.CATEGORY_NOT_MAPPED]
    missing = await amazon_adapter(app).create_listing(amazon_item(listing(fabric_type=None)))
    assert missing.status == "failed" and app.state.fake.calls == []
    assert _fields(missing)["attributes.fabric_type"] == "failed"


@pytest.mark.parametrize("sku", [".", ".."])
async def test_amazon_dot_sku_failed_no_http(sku: str) -> None:
    app = build_fake()
    result = await amazon_adapter(app).create_listing(_five(sku=sku))
    assert result.status == "failed" and app.state.fake.calls == []
    assert result.error is not None
    assert [e.error_code for e in result.error.errors] == [ErrorCode.VALUE_NOT_ALLOWED]


async def test_amazon_get_states() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    await adapter.create_listing(sent)
    got = await adapter.get_listing(sent.seller_sku, listing_id=sent.listing_id)
    assert (got.status, got.marketplace_state, got.listing_id) == ("published", "processing", sent.listing_id)  # fmt: skip
    assert got.external_id and got.external_id.startswith("B0")
    app.state.fake.spapi_summary[sent.seller_sku] = "BUYABLE"
    got = await adapter.get_listing(sent.seller_sku)
    assert got.marketplace_state == "live"


async def test_amazon_get_unknown_raises() -> None:
    from app.adapters.base import ListingNotFound

    with pytest.raises(ListingNotFound):
        await amazon_adapter(build_fake()).get_listing("NOPE")


async def test_amazon_performance_range() -> None:
    adapter = amazon_adapter(build_fake())
    with pytest.raises(ValueError, match="before"):
        await adapter.get_performance(uuid.uuid4(), date(2026, 1, 2), date(2026, 1, 1))
    with pytest.raises(NotImplementedError):
        await adapter.get_performance(uuid.uuid4(), date(2026, 1, 1), date(2026, 1, 2))


async def test_amazon_publish_is_create() -> None:
    app = build_fake()
    result = await amazon_adapter(app).publish_listing(_five())
    assert result.status == "published" and [c["method"] for c in app.state.fake.calls] == ["PUT"]


# ---- T12: update, replay, auth ----


async def test_amazon_update_patches_only_given_fields() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five()
    await adapter.create_listing(sent)
    before = dict(app.state.fake.spapi_items[(SELLER, sent.seller_sku)]["attributes"])
    body = sent.listing
    changed = body.model_copy(
        update={
            "content": body.content.model_copy(
                update={"title": "A new title", "description": "New"}
            ),
            "attributes": {**body.attributes, "color": "maroon"},
        }
    )
    retry = dataclasses.replace(sent, listing=changed, idempotency_key="retry-1")
    result = await adapter.update_listing(retry, fields=["content.title", "attributes.color"])
    assert _fields(result) == {"attributes.color": "succeeded", "content.title": "succeeded"}
    after = app.state.fake.spapi_items[(SELLER, sent.seller_sku)]["attributes"]
    changed_names = {k for k in after if after[k] != before.get(k)}
    assert changed_names == {"item_name", "color"}
    assert after["product_description"] == before["product_description"]


async def test_amazon_update_unknown_path_rejected() -> None:
    adapter = amazon_adapter(build_fake())
    with pytest.raises(ValueError, match="does not carry"):
        await adapter.update_listing(_five(), fields=["attributes.pattern"])
    with pytest.raises(ValueError, match="legal field paths"):
        await adapter.update_listing(_five(), fields=["content.nope"])


async def test_amazon_update_empty_fields_no_call() -> None:
    app = build_fake()
    with pytest.raises(ValueError, match="non-empty"):
        await amazon_adapter(app).update_listing(_five(), fields={})
    assert app.state.fake.calls == []


async def test_amazon_update_unknown_sku_not_found() -> None:
    from app.adapters.base import ListingNotFound

    with pytest.raises(ListingNotFound):
        await amazon_adapter(build_fake()).update_listing(_five(), fields=["content.title"])


async def test_amazon_update_shared_attribute_sends_full_value() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    base = listing()
    words = {"search_terms": ["kurta"], "hidden_terms": ["indigo"]}
    body = base.model_copy(update={"keywords": base.keywords.model_copy(update=words)})
    sent = amazon_item(body)
    await adapter.create_listing(sent)
    changed = body.model_copy(
        update={"keywords": body.keywords.model_copy(update={"search_terms": ["cotton kurta"]})}
    )
    retry = dataclasses.replace(sent, listing=changed, idempotency_key="retry-kw")
    result = await adapter.update_listing(retry, fields=["keywords.search_terms"])
    assert _fields(result) == {"keywords.hidden_terms": "succeeded", "keywords.search_terms": "succeeded"}  # fmt: skip
    stored = app.state.fake.spapi_items[(SELLER, sent.seller_sku)]["attributes"]
    assert stored["generic_keyword"][0]["value"] == "cotton kurta indigo"


async def test_amazon_update_family_child_only() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = amazon_family()
    await adapter.create_listing(sent)
    child = sent.units()[2]
    result = await adapter.update_listing(sent, fields={child.seller_sku: ["content.title"]})
    assert [i.seller_sku for i in result.items] == [child.seller_sku]
    assert [c["method"] for c in app.state.fake.calls][-1:] == ["PATCH"]
    assert len(app.state.fake.calls) == 6


async def test_amazon_replay_same_outcome() -> None:
    app = build_fake()
    adapter = amazon_adapter(app)
    sent = _five(key="same-key")
    app.state.fake.spapi_reject["color"] = ("90220", "Color is invalid.")
    first = await adapter.create_listing(sent)
    again = await adapter.create_listing(sent)
    assert [(f.field, f.status) for f in again.fields] == [
        (f.field, f.status) for f in first.fields
    ]
    assert again.status == first.status == "partially_published"
    assert list(app.state.fake.spapi_items) == [(SELLER, sent.seller_sku)]


async def test_amazon_auth_refresh_once_then_ok() -> None:
    app = build_fake()
    tokens = FakeTokens(app)
    app.state.fake.spapi_auth_mode, app.state.fake.spapi_auth_times = "expired", 1
    result = await amazon_adapter(app, tokens=tokens).create_listing(_five())
    assert result.status == "published" and tokens.refreshes == 1


async def test_amazon_auth_second_401_auth_expired() -> None:
    from app.adapters.base import AuthExpired

    app = build_fake()
    tokens = FakeTokens(app)
    app.state.fake.spapi_auth_mode, app.state.fake.spapi_auth_times = "expired", 5
    with pytest.raises(AuthExpired) as caught:
        await amazon_adapter(app, tokens=tokens).create_listing(_five())
    assert caught.value.report.error_code is ErrorCode.AUTH_EXPIRED and tokens.refreshes == 1


async def test_amazon_auth_refresh_rejected_auth_expired() -> None:
    from app.adapters.base import AuthExpired

    app = build_fake()
    tokens = FakeTokens(app, refuse_refresh=True)
    app.state.fake.spapi_auth_mode, app.state.fake.spapi_auth_times = "expired", 1
    with pytest.raises(AuthExpired):
        await amazon_adapter(app, tokens=tokens).create_listing(_five())
    assert app.state.fake.spapi_items == {}


async def test_amazon_auth_403_after_fresh_permission_denied() -> None:
    from app.adapters.base import PermissionDenied

    app = build_fake()
    tokens = FakeTokens(app)
    app.state.fake.spapi_auth_mode, app.state.fake.spapi_auth_times = "denied", 5
    with pytest.raises(PermissionDenied) as caught:
        await amazon_adapter(app, tokens=tokens).create_listing(_five())
    assert caught.value.report.error_code is ErrorCode.ACCOUNT_PERMISSION_DENIED


async def test_amazon_auth_refresh_called_once() -> None:
    app = build_fake()
    tokens = FakeTokens(app)
    adapter = amazon_adapter(app, tokens=tokens)
    app.state.fake.spapi_auth_mode, app.state.fake.spapi_auth_times = "expired", 1
    await adapter.create_listing(amazon_family())
    assert tokens.refreshes == 1
    assert len(app.state.fake.calls) == 6  # one refused try, then five accepted PUTs
