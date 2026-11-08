"""SI11 T5 (AC1; P:A1, D4; CP-D23, CP-D24): Amazon's dry run (`VALIDATION_PREVIEW`) through
`SpApiClient.put_preview` and `AmazonAdapter.preview_remote`, the `PreviewView`, the limiter
bucket and the setting."""

from __future__ import annotations

import dataclasses
import inspect
import string
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from app.adapters.amazon import constants as c
from app.adapters.amazon.client import PREVIEW_MODE, SpApiClient
from app.adapters.base import RateLimited
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.adapters.amazon_support import (
    CountingLimiter,
    amazon_adapter,
    amazon_family,
    amazon_item,
    seed_error_rows,
    spapi_client,
)
from tests.support.fake_marketplace import build_fake

SAFE_SKU = st.text(alphabet=string.ascii_letters + string.digits + "-_", min_size=1, max_size=40)


class Recorder(httpx.AsyncBaseTransport):
    """Records each request and answers a fixed SP-API reply."""

    def __init__(self, reply: dict[str, Any] | None = None, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.reply = reply if reply is not None else {"sku": "x", "status": "VALID", "issues": []}
        self.status = status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.reply)


def _query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)


# ---- the client: the mode is set by the preview route alone ----


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(sku=SAFE_SKU)
async def test_put_preview_always_carries_the_mode_put_never(sku: str) -> None:
    transport = Recorder()
    client = spapi_client(build_fake(), transport=transport)
    await client.put_preview(sku, {"productType": "KURTA", "attributes": {}})
    await client.put(sku, {"productType": "KURTA", "attributes": {}})
    preview, put = transport.requests
    assert _query(preview)["mode"] == [PREVIEW_MODE]
    assert _query(preview)["marketplaceIds"] == ["A21TJRUUN4KGV"]
    assert preview.method == put.method == "PUT"
    assert "mode" not in _query(put)
    await client.aclose()


def test_no_public_argument_changes_the_mode() -> None:
    for name in ("put", "patch", "get", "put_preview"):
        params = set(inspect.signature(getattr(SpApiClient, name)).parameters)
        assert params <= {"self", "sku", "body"}, name


async def test_preview_uses_its_own_limiter_bucket() -> None:
    limiter = CountingLimiter()
    client = spapi_client(build_fake(), transport=Recorder(), limiter=limiter)
    await client.put_preview("SKU-1", {"productType": "KURTA", "attributes": {}})
    assert limiter.calls == ["put_preview"]
    assert "put_preview" in c.OPERATIONS and c.RATES["put_preview"] == 1
    await client.aclose()


# ---- the adapter over the fake marketplace ----


@pytest.fixture
def fake() -> Any:
    return build_fake()


def _adapter(app: Any) -> Any:
    return amazon_adapter(app, rows=seed_error_rows())


def _no_images(sent: Any) -> Any:
    return dataclasses.replace(sent, image_refs={})


async def test_clean_preview_stores_nothing(fake: Any) -> None:
    adapter = _adapter(fake)
    sent = _no_images(amazon_item())
    result = await adapter.preview_remote(sent)
    assert (result.status, result.applied, result.errors) == ("clean", False, ())
    assert result.per_sku == {sent.seller_sku: "VALID"}
    assert result.scope and not any(p.startswith("images") for p in result.scope)
    assert fake.state.fake.spapi_items == {}
    assert fake.state.fake.spapi_preview_calls == 1
    await adapter.aclose()


async def test_family_sends_parent_then_children_and_maps_a_child_issue(fake: Any) -> None:
    adapter = _adapter(fake)
    sent = _no_images(amazon_family())
    child = sent.units()[2].seller_sku
    fake.state.fake.spapi_scenario[child] = "enum_fold"  # colour outside Red/Blue
    result = await adapter.preview_remote(sent)
    order = [call["path"].rsplit("/", 1)[1] for call in fake.state.fake.calls]
    assert order == [u.seller_sku for u in sent.units()]
    assert result.status == "issues"
    assert result.per_sku[child] == "INVALID"
    (issue,) = result.errors
    assert issue.field == "attributes.color"
    assert issue.error_code.value == "VALUE_NOT_ALLOWED"  # category INVALID_ATTRIBUTE
    assert issue.source.value == "amazon_adapter"
    await adapter.aclose()


async def test_missing_attribute_maps_through_the_code_row(fake: Any) -> None:
    adapter = _adapter(fake)
    sent = _no_images(amazon_item())
    fake.state.fake.spapi_scenario[sent.seller_sku] = "missing_derivable"
    result = await adapter.preview_remote(sent)
    assert result.status == "issues"
    assert [e.error_code.value for e in result.errors] == ["MISSING_ATTRIBUTE"]
    await adapter.aclose()


async def test_accepted_preview_is_failed_and_applied(
    fake: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from structlog.testing import capture_logs

    adapter = _adapter(fake)
    sent = _no_images(amazon_item())
    fake.state.fake.spapi_preview_reply[sent.seller_sku] = "ACCEPTED"
    with capture_logs() as logs:
        result = await adapter.preview_remote(sent)
    assert (result.status, result.applied) == ("failed", True)
    (line,) = [entry for entry in logs if entry["event"] == "preview.applied"]
    assert line["log_level"] == "error"
    assert set(line) <= {"event", "log_level", "listing_id", "sku_index"}
    await adapter.aclose()


async def test_unreadable_reply_is_applied(fake: Any) -> None:
    adapter = amazon_adapter(fake, rows=seed_error_rows(), transport=Recorder(reply={"x": 1}))
    result = await adapter.preview_remote(_no_images(amazon_item()))
    assert (result.status, result.applied) == ("failed", True)
    await adapter.aclose()


async def test_400_is_failed_not_applied(fake: Any) -> None:
    adapter = amazon_adapter(fake, rows=seed_error_rows(), transport=Recorder({}, status=400))
    result = await adapter.preview_remote(_no_images(amazon_item()))
    assert (result.status, result.applied) == ("failed", False)
    await adapter.aclose()


async def test_429_raises_rate_limited(fake: Any) -> None:
    fake.state.fake.spapi_429 = 10
    adapter = _adapter(fake)
    with pytest.raises(RateLimited):
        await adapter.preview_remote(_no_images(amazon_item()))
    await adapter.aclose()


async def test_local_error_sends_nothing(fake: Any) -> None:
    adapter = _adapter(fake)
    sent = _no_images(amazon_item(template=None))  # not mapped: CATEGORY_NOT_MAPPED
    result = await adapter.preview_remote(sent)
    assert result.status == "failed" and not result.applied
    assert [e.error_code.value for e in result.errors] == ["CATEGORY_NOT_MAPPED"]
    assert fake.state.fake.calls == []
    await adapter.aclose()


async def test_preview_text_is_scrubbed(fake: Any) -> None:
    adapter = _adapter(fake)
    sent = _no_images(amazon_item())
    tokens = adapter._client._tokens
    manufacturer = "Test Mills Pvt Ltd"
    issue = {
        "code": "99300",
        "message": f"Bad colour from {manufacturer} with token {tokens.token}",
        "severity": "ERROR",
        "attributeNames": ["color"],
        "categories": ["INVALID_ATTRIBUTE"],
    }
    fake.state.fake.spapi_reject.clear()
    adapter = amazon_adapter(
        fake,
        rows=seed_error_rows(),
        tokens=tokens,
        transport=Recorder({"sku": sent.seller_sku, "status": "INVALID", "issues": [issue]}),
    )
    result = await adapter.preview_remote(sent)
    (item,) = result.errors
    text = item.model_dump_json()
    assert manufacturer not in text and tokens.token not in text
    await adapter.aclose()


async def test_preview_sends_no_image_and_drops_image_issues(fake: Any) -> None:
    sent = _no_images(amazon_item())
    transport = Recorder(
        {
            "sku": sent.seller_sku,
            "status": "INVALID",
            "issues": [
                {
                    "code": "99310",
                    "message": "Main image missing.",
                    "severity": "ERROR",
                    "attributeNames": [c.MAIN_IMAGE],
                    "categories": ["MISSING_IMAGE"],
                }
            ],
        }
    )
    adapter = amazon_adapter(fake, rows=seed_error_rows(), transport=transport)
    result = await adapter.preview_remote(sent)
    (request,) = transport.requests
    body = request.content.decode()
    assert c.MAIN_IMAGE not in body and "other_product_image_locator" not in body
    # INVALID only because of the image the preview left out: nothing of ours to fix
    assert all(not e.field.startswith("images") for e in result.errors)
    await adapter.aclose()


# ---- the view and the setting ----


def test_preview_view_exposes_only_the_dry_run() -> None:
    from app.workers.previewers import PreviewView

    public = {n for n in dir(PreviewView) if not n.startswith("_")}
    assert public == {"preview_remote", "aclose"}
    assert "__getattr__" not in PreviewView.__dict__


async def test_preview_view_refuses_an_adapter_without_dry_run() -> None:
    from app.adapters.flipkart.adapter import FlipkartAdapter
    from app.workers.previewers import PreviewUnsupported, PreviewView

    assert not hasattr(FlipkartAdapter, "preview_remote")
    with pytest.raises(PreviewUnsupported):
        PreviewView(object())


def test_setting_defaults_off(clean_env: Any) -> None:
    from app.core.config import Settings

    plain = Settings(_env_file=None, app_env="local", jwt_secret="x" * 32)
    assert plain.amazon_validation_preview_enabled is False


def test_env_example_turns_it_on(repo_root: Any) -> None:
    text = (repo_root / ".env.example").read_text(encoding="utf-8")
    assert "AMAZON_VALIDATION_PREVIEW_ENABLED=true" in text.splitlines()
