"""SI11 T10 (AC6; P:B3, D8): the repair store. `insert` is a replay-safe no-op on either unique
index, `retire` retires once, another tenant's unit of work sees none of it, and the publish job
retires a repair whose seller value changed (`source_changed`) and keeps a matching one."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, pub, publish_and_run

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


def _new(listing_id: uuid.UUID, **over: Any) -> Any:
    from app.repositories.repairs import NewRepair

    values: dict[str, Any] = {
        "listing_id": listing_id,
        "marketplace": "amazon",
        "field_path": "attributes.color",
        "attribute": "color",
        "rule": "enum_fold",
        "issue_key": f"amazon_adapter|attributes.color|VALUE_NOT_ALLOWED|99300|{uuid.uuid4()}",
        "source_digest": bytes(32),
        "to_digest": bytes([1]) * 32,
        "value_json": [{"value": "Red"}],
    }
    return NewRepair(**{**values, **over})


async def test_insert_replay_retire_and_scope(pub: PubWorld) -> None:
    from app.repositories.uow import tenant_uow

    made = await approved(pub)
    new = _new(made.listing_id)
    async with tenant_uow(pub.world.tenant("acme")) as uow:
        first = await uow.listing_repairs.insert(new)
        assert first is not None
        assert await uow.listing_repairs.insert(new) is None  # the same key and rule: a replay
        other_key = _new(made.listing_id)  # a second live row for the same attribute
        assert await uow.listing_repairs.insert(other_key) is None
        assert [r.id for r in await uow.listing_repairs.live_for_listing(made.listing_id)] == [
            first
        ]
    async with tenant_uow(pub.world.tenant("bharat")) as uow:
        assert await uow.listing_repairs.live_for_listing(made.listing_id) == []
        assert await uow.listing_repairs.retire([first], "superseded") == 0
    async with tenant_uow(pub.world.tenant("acme")) as uow:
        assert await uow.listing_repairs.retire([first], "superseded") == 1
        assert await uow.listing_repairs.retire([first], "superseded") == 0
        assert await uow.listing_repairs.live_for_listing(made.listing_id) == []
        with pytest.raises(ValueError, match="retired reason"):
            await uow.listing_repairs.retire([first], "bored")


async def test_publish_retires_a_stale_repair(api: httpx.AsyncClient, pub: PubWorld) -> None:
    from app.adapters.amazon.repairs import digest
    from app.repositories.uow import tenant_uow

    made = await approved(pub)
    async with tenant_uow(pub.world.tenant("acme")) as uow:
        source = await uow.listings.preview_source(made.listing_id)
        assert source is not None and source.version is not None
        colour = source.version.content.get("attributes", {}).get("color")
        stale = await uow.listing_repairs.insert(
            _new(made.listing_id, source_digest=digest("a value the seller has since changed"))
        )
        kept = await uow.listing_repairs.insert(
            _new(
                made.listing_id,
                attribute="size",
                field_path="attributes.size",
                source_digest=digest(source.version.content.get("attributes", {}).get("size")),
            )
        )
    del colour
    await publish_and_run(api, pub, made)
    rows = dict(
        pub.rows(
            "SELECT id, retired_reason FROM listing_repairs WHERE listing_id = %s",
            [made.listing_id],
        )
    )
    assert rows[stale] == "source_changed"
    assert rows[kept] is None
