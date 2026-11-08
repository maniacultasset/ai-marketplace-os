"""SI11 T6 (AC2, AC3; P:A2, D5; CP-D4): the dry run's issues live in their own channel. A status
read or a write result never closes a preview row; a later preview of the same listing closes the
ones it no longer sees; another tenant's sync never touches them."""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import generated
from .test_issue_sync import _adapter_report, _issues

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


async def _adapter_sync(
    world: GenWorld, made: Any, report: Any, *, cause: str = "publish", **kw: Any
) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.issues import sync_issues

    async with tenant_uow(world.tenant("acme")) as uow:
        listing = await uow.listings.get(made.listing_id)
        return await sync_issues(
            uow,
            listing,
            "amazon_adapter",
            report,
            run_id=None,
            cause=cause,
            critical_keys=frozenset(),
            **kw,
        )


def _by_channel(world: GenWorld, listing_id: Any) -> dict[str, str]:
    return {
        ("preview" if r["dedupe_key"].endswith("|preview") else "live"): r["status"]
        for r in _issues(world, listing_id)
    }


async def test_preview_row_survives_live_syncs(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    await _adapter_sync(gen_world, made, _adapter_report("content.title"), channel="preview")
    # a clean status read and a clean write result of the same field
    await _adapter_sync(gen_world, made, _adapter_report(), cause="sync")
    await _adapter_sync(gen_world, made, _adapter_report(), scope_fields=["content.title"])
    assert _by_channel(gen_world, made.listing_id) == {"preview": "open"}


async def test_live_row_survives_a_clean_preview(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    await _adapter_sync(gen_world, made, _adapter_report("content.title"))
    result = await _adapter_sync(
        gen_world, made, _adapter_report(), channel="preview", cause="revalidate"
    )
    assert result.closed == 0
    assert _by_channel(gen_world, made.listing_id) == {"live": "open"}


async def test_clean_preview_closes_the_preview_row(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    await _adapter_sync(gen_world, made, _adapter_report("content.title"), channel="preview")
    result = await _adapter_sync(
        gen_world, made, _adapter_report(), channel="preview", cause="revalidate"
    )
    assert result.closed == 1
    [row] = _issues(gen_world, made.listing_id)
    assert (row["status"], row["resolution"]) == ("resolved", "revalidated_clean")


async def test_open_errors_by_channel(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow

    made = await generated(gen_world)
    await _adapter_sync(gen_world, made, _adapter_report("content.title"), channel="preview")
    await _adapter_sync(gen_world, made, _adapter_report("content.bullets"))
    async with tenant_uow(gen_world.tenant("acme")) as uow:
        preview = await uow.listing_issues.open_errors(
            made.listing_id, ["amazon_adapter"], channel="preview"
        )
        live = await uow.listing_issues.open_errors(
            made.listing_id, ["amazon_adapter"], channel="live"
        )
        both = await uow.listing_issues.open_errors(made.listing_id, ["amazon_adapter"])
    assert [r.field_path for r in preview] == ["content.title"]
    assert [r.field_path for r in live] == ["content.bullets"]
    assert len(both) == 2


async def test_other_tenant_never_closes_a_preview_row(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow

    made = await generated(gen_world)
    await _adapter_sync(gen_world, made, _adapter_report("content.title"), channel="preview")
    async with tenant_uow(gen_world.tenant("bharat")) as uow:
        closed = await uow.listing_issues.close_missing(
            made.listing_id, "amazon_adapter", [], "revalidated_clean", channel="preview"
        )
    assert closed == 0
    assert _by_channel(gen_world, made.listing_id) == {"preview": "open"}
