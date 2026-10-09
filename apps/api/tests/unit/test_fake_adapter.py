"""S14b T10 (AC 6; D15): `FakeAdapter` behaviour, `submitted_fields` and the adapter ABC."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from tests.adapters.contract import item, sample_listing

ADAPTERS = Path(__file__).resolve().parents[2] / "app" / "adapters"


def _fake(**fail: tuple[str, str]):  # type: ignore[no-untyped-def]
    from app.adapters.fake import FakeAdapter

    return FakeAdapter("amazon", fail_fields={k.replace("__", "."): v for k, v in fail.items()})


async def test_fake_adapter_partial_raw_error() -> None:
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import ErrorSource

    adapter = _fake(content__title=("8541", "Prohibited character in title"))
    result = await adapter.create_listing(item())
    assert result.status == "partially_published"
    (failed,) = [f for f in result.fields if f.status == "failed"]
    assert failed.error is not None
    assert (failed.error.raw_code, failed.error.message) == (
        "8541",
        "Prohibited character in title",
    )
    assert failed.error.error_code is ErrorCode.MARKETPLACE_UNKNOWN_ERROR
    assert failed.error.source is ErrorSource.AMAZON_ADAPTER
    assert result.error is not None and result.error.error_code is ErrorCode.API_REJECTED_PARTIAL


async def test_fake_adapter_all_failed() -> None:
    from app.adapters.base import submitted_fields
    from app.adapters.fake import FakeAdapter
    from mkos_schemas.codes import ErrorCode

    sent = item()
    adapter = FakeAdapter(
        "amazon", fail_fields=dict.fromkeys(submitted_fields(sent.listing), ("E", "no"))
    )
    result = await adapter.create_listing(sent)
    assert result.status == "failed"
    assert result.error is not None and result.error.error_code is ErrorCode.PUBLISH_FAILED


async def test_fake_adapter_idempotent_replay() -> None:
    adapter = _fake()
    sent = item(key="k1")
    assert await adapter.create_listing(sent) == await adapter.create_listing(sent)
    assert adapter.creates == 1


async def test_fake_adapter_same_key_other_body_replays_first() -> None:
    adapter = _fake()
    first = await adapter.create_listing(item(key="k2", sku="A"))
    other = sample_listing(attributes={"color": "red"})
    again = await adapter.create_listing(item(other, key="k2", sku="B"))
    assert again == first
    assert adapter.creates == 1


async def test_fake_adapter_update_only_given_fields() -> None:
    adapter = _fake()
    sent = item(sku="UPD")
    await adapter.create_listing(sent)
    before = adapter.stored("UPD")
    changed = sample_listing(
        content=sent.listing.content.model_copy(
            update={"title": "Changed", "description": "Changed"}
        )
    )
    result = await adapter.update_listing(item(changed, sku="UPD"), fields=["content.title"])
    assert [f.field for f in result.fields] == ["content.title"]
    after = adapter.stored("UPD")
    assert after["content.title"] == "Changed"
    assert {k: v for k, v in after.items() if k != "content.title"} == {
        k: v for k, v in before.items() if k != "content.title"
    }


async def test_fake_adapter_update_refuses_bad_fields() -> None:
    adapter = _fake()
    sent = item(sku="BAD")
    await adapter.create_listing(sent)
    for fields in ([], ["images"], ["keywords.*"]):
        with pytest.raises(ValueError, match="legal field paths"):
            await adapter.update_listing(item(sku="BAD"), fields=fields)


async def test_fake_adapter_unknown_sku() -> None:
    from app.adapters.base import ListingNotFound

    adapter = _fake()
    with pytest.raises(ListingNotFound):
        await adapter.get_listing("NOPE")
    with pytest.raises(ListingNotFound):
        await adapter.update_listing(item(sku="NOPE"), fields=["content.title"])


async def test_fake_adapter_concurrent_same_key_one_create() -> None:
    adapter = _fake()
    sent = item(key="race")
    results = await asyncio.gather(*(adapter.create_listing(sent) for _ in range(5)))
    assert all(r == results[0] for r in results)
    assert adapter.creates == 1


async def test_fake_adapter_performance_days() -> None:
    from datetime import date
    from uuid import uuid4

    days = await _fake().get_performance(uuid4(), date(2026, 1, 1), date(2026, 1, 3))
    assert [d.date.day for d in days] == [1, 2, 3]
    with pytest.raises(ValueError, match="before"):
        await _fake().get_performance(uuid4(), date(2026, 1, 2), date(2026, 1, 1))


def test_adapter_abc_cannot_instantiate() -> None:
    from app.adapters.base import MarketplaceAdapter

    with pytest.raises(TypeError):
        MarketplaceAdapter()  # type: ignore[abstract]


def test_adapters_import_no_socket_libs() -> None:
    banned = {"socket", "httpx", "requests", "aiohttp", "urllib", "http", "openai", "ssl"}
    for path in sorted(ADAPTERS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not {n.split(".")[0] for n in names} & banned, (path.name, names)


async def test_submitted_fields_all_legal_paths() -> None:
    from app.adapters.base import submitted_fields
    from mkos_schemas.paths import is_valid_field_path

    listing = sample_listing()
    listing = listing.model_copy(
        update={"images": [*listing.images, listing.images[0].model_copy(update={"position": 2})]}
    )
    assert len(listing.images) >= 2
    assert listing.keywords.search_terms and listing.keywords.hidden_terms and listing.attributes
    fields = submitted_fields(listing)
    assert all(is_valid_field_path(f) for f in fields)
    assert "images" not in fields and not any(f.startswith("keywords.*") for f in fields)
    assert {"images[0]", "images[1]", "images[2]", "keywords.search_terms"} <= set(fields)
    result = await _fake().create_listing(item(listing))
    assert [f.field for f in result.fields] == fields


# ---- S20 T2: per-SKU updates, families and preview ----


async def test_fake_adapter_update_per_sku() -> None:
    from tests.adapters.contract import family

    adapter = _fake()
    sent = family()
    await adapter.create_listing(sent)
    child = sent.units()[1]
    changed = sample_listing(content=sent.listing.content.model_copy(update={"title": "New"}))
    update = item(changed, sku=sent.seller_sku)
    update = type(update)(
        listing_id=sent.listing_id,
        seller_sku=sent.seller_sku,
        idempotency_key="upd-sku",
        listing=changed,
        items=sent.items,
        variation_theme=sent.variation_theme,
    )
    result = await adapter.update_listing(update, fields={child.seller_sku: ["content.title"]})
    assert [i.seller_sku for i in result.items] == [child.seller_sku]
    assert adapter.stored(child.seller_sku)["content.title"] == "New"
    assert adapter.stored(sent.seller_sku)["content.title"] == sent.listing.content.title
    with pytest.raises(ValueError, match="unknown seller_sku"):
        await adapter.update_listing(update, fields={"NOT-IN-FAMILY": ["content.title"]})


async def test_fake_adapter_family_items() -> None:
    from tests.adapters.contract import family

    sent = family()
    result = await _fake(content__title=("8541", "bad")).create_listing(sent)
    assert result.status == "partially_published"
    assert [i.seller_sku for i in result.items] == [u.seller_sku for u in sent.units()]
    for one in result.items:
        (failed,) = [f for f in one.fields if f.status == "failed"]
        assert failed.field == "content.title"
    assert len({f.field for f in result.fields}) == len(result.fields)


def test_fake_adapter_preview_no_io() -> None:
    from tests.adapters.contract import family, no_sockets

    adapter = _fake()
    sent = family()
    with no_sockets():
        preview = adapter.preview(sent)
    assert [p.item_role for p in preview.items] == ["parent", "child", "child"]
    assert preview.items[0].payload["content.title"] == sent.listing.content.title
    assert preview.report.passed
