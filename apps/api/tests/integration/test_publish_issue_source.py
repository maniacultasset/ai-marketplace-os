"""SI4 A15 (AC12; CONTEXT C:118 defect): a publish outcome's issues carry the marketplace's own
adapter source, so a Flipkart result never writes or closes `amazon_adapter` rows."""

from __future__ import annotations

import uuid

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .flipkart_publish_support import FkWorld, fkp, pub
from .publish_support import PubWorld, approved, publish_and_run, retry, run_job
from .test_flipkart_publish import FAILED_PATHS, _family, _partial

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["fkp", "pub"]


def _sources(world: PubWorld, listing_id: uuid.UUID) -> list[tuple[str, str, str]]:
    return world.rows(
        "SELECT source, field_path, status FROM listing_issues WHERE listing_id = %s "
        "ORDER BY field_path, created_at",
        [listing_id],
    )


async def test_a_flipkart_field_error_is_a_flipkart_issue(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    made = await _family(fkp)
    await _partial(api, fkp, made)
    rows = _sources(fkp.pub, made.listing_id)
    assert {field for _source, field, _status in rows} == set(FAILED_PATHS)
    assert {source for source, *_ in rows} == {"flipkart_adapter"}


async def test_an_amazon_field_error_is_an_amazon_issue(
    api: httpx.AsyncClient, pub: PubWorld
) -> None:
    made = await approved(pub)
    pub.fake.set_fail_fields({"content.title": ("BadTitle", "bad")})
    await publish_and_run(api, pub, made)
    assert [(s, f) for s, f, _st in _sources(pub, made.listing_id)] == [
        ("amazon_adapter", "content.title")
    ]


async def test_a_flipkart_retry_closes_only_flipkart_rows(
    api: httpx.AsyncClient, fkp: FkWorld
) -> None:
    made = await _family(fkp)
    await _partial(api, fkp, made)
    tenant = fkp.pub.world.tenant("acme")
    stray = f.listing_issue(
        fkp.pub.conn,
        tenant,
        made.listing_id,
        marketplace="flipkart",
        source="amazon_adapter",
        field_path="content.title",
        error_code="MARKETPLACE_UNKNOWN_ERROR",
        error_class="unknown",
        retry_class="user_action",
        dedupe_key="amazon_adapter|content.title|MARKETPLACE_UNKNOWN_ERROR|stray",
    )
    response = await retry(api, fkp.pub, made)
    assert response.status_code == 202, response.text
    await run_job(uuid.UUID(response.json()["job_id"]))
    flipkart = [r for r in _sources(fkp.pub, made.listing_id) if r[0] == "flipkart_adapter"]
    assert flipkart and all(status == "resolved" for *_x, status in flipkart)
    assert fkp.pub.scalar("SELECT status FROM listing_issues WHERE id = %s", [stray]) == "open"
