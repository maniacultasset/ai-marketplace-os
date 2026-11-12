"""The owner's one-off Amazon dry run (`app.workers.amazon_preview`, `app.cli ops amazon-preview`):
its settings guard, the preview-only transport and adapter wrapper, the enqueue guard, the boot
refusal of every role but the shell, and the STOP judgement. Nothing here opens a database."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from app.workers.amazon_preview import (
    OneoffReport,
    PreviewOnlyTransport,
    PreviewWriteRefused,
    SkuRead,
    TransportLog,
    _judge,
    _PreviewOnly,
    live_skus,
    settings_refusal,
)
from tests.adapters.amazon_support import SELLER, amazon_adapter, amazon_item, spapi_client
from tests.support.fake_marketplace import build_fake

ITEMS = f"http://fake.test/listings/2021-08-01/items/{SELLER}"


def _settings(**over: Any) -> Any:
    from app.core.config import Settings

    base = {
        "amazon_preview_oneoff": True,
        "amazon_validation_preview_enabled": True,
        "amazon_publish_enabled": True,
        "amazon_catalog_precheck_enabled": False,
    }
    return Settings.model_construct(**{**base, **over})


# ---- guard 1: the settings ----


def test_settings_allow_the_one_off_with_every_switch() -> None:
    assert settings_refusal(_settings(), precheck=False) is None
    assert settings_refusal(_settings(amazon_catalog_precheck_enabled=True), precheck=True) is None


@pytest.mark.parametrize(
    ("over", "precheck", "named"),
    [
        ({"amazon_preview_oneoff": False}, False, "AMAZON_PREVIEW_ONEOFF"),
        ({"amazon_validation_preview_enabled": False}, False, "AMAZON_VALIDATION_PREVIEW_ENABLED"),
        ({"amazon_publish_enabled": False}, False, "AMAZON_PUBLISH_ENABLED"),
        ({}, True, "AMAZON_CATALOG_PRECHECK_ENABLED"),
        ({"amazon_catalog_precheck_enabled": True}, False, "AMAZON_CATALOG_PRECHECK_ENABLED"),
    ],
)
def test_settings_refuse_without_a_switch(over: dict[str, Any], precheck: bool, named: str) -> None:
    refusal = settings_refusal(_settings(**over), precheck=precheck)
    assert refusal is not None and named in refusal


@pytest.mark.parametrize("role", ["api", "worker", "migrate", "seed"])
def test_no_role_but_the_shell_boots_with_the_one_off_switch(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("APP_ROLE", role)
    monkeypatch.setenv("AMAZON_PREVIEW_ONEOFF", "1")
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as info:
        get_settings()
    assert "refuses AMAZON_PREVIEW_ONEOFF" in str(info.value.code)
    monkeypatch.setenv("APP_ROLE", "shell")
    get_settings.cache_clear()
    assert get_settings().amazon_preview_oneoff is True
    get_settings.cache_clear()


# ---- the transport: GET and the VALIDATION_PREVIEW PUT only ----


class Inner(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return httpx.Response(200, json={"status": "VALID", "issues": []})


async def _send(method: str, url: str) -> tuple[Inner, TransportLog, BaseException | None]:
    inner, seen = Inner(), TransportLog()
    transport = PreviewOnlyTransport(inner, seen)
    async with httpx.AsyncClient(transport=transport) as client:
        try:
            await client.request(method, url, content=b"{}" if method != "GET" else None)
        except PreviewWriteRefused as exc:
            return inner, seen, exc
    return inner, seen, None


@pytest.mark.parametrize(
    ("method", "url", "kind"),
    [
        ("GET", f"{ITEMS}/SKU-1?marketplaceIds=A21TJRUUN4KGV", "read"),
        ("PUT", f"{ITEMS}/SKU-1?marketplaceIds=A21TJRUUN4KGV&mode=VALIDATION_PREVIEW", "preview"),
    ],
)
async def test_transport_passes_a_get_and_the_preview_put(method: str, url: str, kind: str) -> None:
    inner, seen, refused = await _send(method, url)
    assert refused is None and len(inner.sent) == 1
    assert seen.calls == [(method, kind)] and seen.refused == []


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("PUT", f"{ITEMS}/SKU-1?marketplaceIds=A21TJRUUN4KGV"),
        ("PUT", f"{ITEMS}/SKU-1?mode=VALIDATION_PREVIEW&mode=VALIDATION_PREVIEW"),
        ("PUT", f"{ITEMS}/SKU-1?mode=validation_preview"),
        ("PUT", f"{ITEMS}/SKU-1?mode=OTHER"),
        ("PUT", f"{ITEMS}?mode=VALIDATION_PREVIEW"),
        ("PUT", f"{ITEMS}/SKU-1/extra?mode=VALIDATION_PREVIEW"),
        ("PUT", "http://fake.test/feeds/2021-06-30/feeds?mode=VALIDATION_PREVIEW"),
        ("PATCH", f"{ITEMS}/SKU-1?mode=VALIDATION_PREVIEW"),
        ("POST", f"{ITEMS}/SKU-1?mode=VALIDATION_PREVIEW"),
        ("DELETE", f"{ITEMS}/SKU-1?marketplaceIds=A21TJRUUN4KGV"),
    ],
)
async def test_transport_refuses_anything_else_unsent(method: str, url: str) -> None:
    inner, seen, refused = await _send(method, url)
    assert isinstance(refused, PreviewWriteRefused)
    assert inner.sent == [] and seen.calls == [] and seen.refused == [method]


async def test_the_real_adapter_sends_only_the_preview_put_and_gets() -> None:
    """The Amazon adapter over the fake marketplace, through the one-off's transport and wrapper:
    the dry run of a family and a read of each SKU reach Amazon as PUT ...mode=VALIDATION_PREVIEW
    and GET only, and nothing is stored."""
    from app.workers.readers import ReaderView

    fake = build_fake()
    recorder = Recorder(httpx.ASGITransport(fake))
    seen = TransportLog()
    adapter = amazon_adapter(fake, transport=PreviewOnlyTransport(recorder, seen))
    item = amazon_item(sku="KTEST-KURTA-1")
    verdict = await _PreviewOnly(adapter).preview_remote(item)
    for unit in item.units():
        assert await ReaderView(adapter).get_listing(unit.seller_sku) is None
    await adapter.aclose()
    assert verdict.status in ("clean", "issues") and not verdict.applied
    assert recorder.sent, "nothing reached the fake"
    for request in recorder.sent:
        if request.method == "PUT":
            assert request.url.params.get_list("mode") == ["VALIDATION_PREVIEW"]
        else:
            assert request.method == "GET"
            assert "mode" not in request.url.params
    assert {m for m, _ in seen.calls} == {"PUT", "GET"} and seen.refused == []


class Recorder(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.sent: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return await self.inner.handle_async_request(request)


async def test_a_put_through_the_client_is_refused_before_it_is_sent() -> None:
    """Belt and braces: even a caller that reached the client's `put` sends nothing."""
    inner, seen = Inner(), TransportLog()
    client = spapi_client(build_fake(), transport=PreviewOnlyTransport(inner, seen))
    with pytest.raises(PreviewWriteRefused):
        await client.put("SKU-1", {"productType": "KURTA", "attributes": {}})
    with pytest.raises(PreviewWriteRefused):
        await client.patch("SKU-1", {"patches": []})
    await client.aclose()
    assert inner.sent == [] and seen.refused == ["PUT", "PATCH"]


def test_the_wrapper_exposes_no_write() -> None:
    wrapper = _PreviewOnly(MagicMock())
    for name in ("create_listing", "update_listing", "publish_listing", "get_listing", "put"):
        assert not hasattr(wrapper, name), name
    with pytest.raises(AttributeError):
        wrapper.anything = 1  # type: ignore[attr-defined]


def test_only_test_skus_pass() -> None:
    assert live_skus(["KTEST-1", "KTEST-KURTA-M"]) == []
    assert live_skus(["KTEST-1", "KURTA-1", "ktest-2", " KTEST-3", "XKTEST-4"]) == [
        "KURTA-1",
        "ktest-2",
        " KTEST-3",
        "XKTEST-4",
    ]


async def test_the_wrapper_refuses_a_built_item_with_a_live_sku_unsent() -> None:
    """The second KTEST check: the item as built, right before the adapter is called."""
    from app.workers.amazon_preview import NotEligible

    adapter = MagicMock()
    with pytest.raises(NotEligible, match="not test SKUs"):
        await _PreviewOnly(adapter).preview_remote(amazon_item(sku="KURTA-LIVE-1"))
    assert adapter.mock_calls == []


# ---- the enqueue guard ----


async def test_no_enqueue_refuses_every_kind_before_the_session() -> None:
    from app.repositories.jobs import EnqueueRefused, EnqueueSpec, JobRunRepository, no_enqueue
    from mkos_schemas.enums import JobKind

    session = MagicMock()
    repo = JobRunRepository(session, uuid.uuid4())
    for kind in JobKind:
        spec = EnqueueSpec(
            kind=kind, entity_type="listing", entity_id=uuid.uuid4(), created_by=None
        )
        with no_enqueue("test"), pytest.raises(EnqueueRefused):
            await repo.enqueue(spec)
    assert session.mock_calls == []


# ---- the STOP judgement ----


def _report(verdict: Any = None) -> OneoffReport:
    report = OneoffReport(uuid.uuid4(), "validated", False, verdict=verdict)
    report.skus = [SkuRead("SKU-1", "404")]
    return report


def _verdict(**over: Any) -> Any:
    from app.adapters.base import RemotePreview

    return RemotePreview(**{"status": "clean", "per_sku": {"SKU-1": "VALID"}, **over})


def test_judge_passes_a_clean_preview_with_every_sku_absent() -> None:
    report = _report(_verdict())
    _judge(report, {"SKU-1": "404"}, {"SKU-1": "VALID"})
    assert report.stop == [] and report.exit_code == 0
    assert "STOP" not in report.render()


@pytest.mark.parametrize(
    ("after", "replies", "over"),
    [
        ({"SKU-1": "found"}, {"SKU-1": "VALID"}, {}),
        ({"SKU-1": "404"}, {"SKU-1": "ACCEPTED"}, {}),
        ({"SKU-1": "404"}, {"SKU-1": "unexpected"}, {"status": "failed", "applied": True}),
    ],
)
def test_judge_stops_on_a_found_sku_or_an_applied_looking_reply(
    after: dict[str, str], replies: dict[str, str], over: dict[str, Any]
) -> None:
    report = _report(_verdict(per_sku=replies, **over))
    _judge(report, after, replies)
    assert report.stop and report.exit_code == 5
    assert "STOP:" in report.render()


def test_judge_takes_a_4xx_as_refused_not_applied() -> None:
    report = _report(_verdict(status="failed", per_sku={"SKU-1": "400"}))
    _judge(report, {"SKU-1": "404"}, {"SKU-1": "400"})
    assert report.stop == []


def test_render_prints_each_issue_and_sku() -> None:
    from app.adapters.amazon.mapping import error
    from mkos_schemas.codes import ErrorCode

    issue = error("attributes.fabric_type", ErrorCode.MARKETPLACE_UNKNOWN_ERROR, "Bad fabric.")
    report = _report(_verdict(status="issues", errors=(issue,), per_sku={"SKU-1": "INVALID"}))
    report.skus[0].reply, report.skus[0].after = "INVALID", "404"
    text = report.render()
    assert "verdict: issues (1 issue(s))" in text
    assert '"attribute": "attributes.fabric_type"' in text and '"severity": "error"' in text
    assert "sku SKU-1: before 404, preview reply INVALID, after 404" in text


# ---- the CLI hand-over ----


def test_cli_hands_over_to_the_worker_entrypoint() -> None:
    from app import cli

    tenant, listing = uuid.uuid4(), uuid.uuid4()
    args = cli.build_parser().parse_args(
        [
            "ops",
            "amazon-preview",
            "--tenant-slug",
            "acme",
            "--listing-id",
            str(listing),
            "--staff-email",
            "Staff@Example.com",
            "--precheck",
        ]
    )
    argv = cli.amazon_preview_argv(args, tenant)
    assert argv[1:] == [
        "-m",
        "app.workers.amazon_preview",
        "--tenant-id",
        str(tenant),
        "--listing-id",
        str(listing),
        "--staff-email",
        "staff@example.com",
        "--precheck",
    ]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["ops", "amazon-preview", "--tenant-slug", "acme", "--staff-email", "s@b.test"]
        )
