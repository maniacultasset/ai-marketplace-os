"""SI11 test world: the publish world (`publish_support.pub`) with the marketplace dry run on and
the in-process `FakeAdapter` as the previewer."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from .publish_support import PubWorld, build_pub_world


@pytest.fixture
async def prev(auth_db: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PubWorld]:
    from app.core.config import get_settings
    from app.workers import previewers

    monkeypatch.setenv("AMAZON_VALIDATION_PREVIEW_ENABLED", "true")
    get_settings.cache_clear()
    async with build_pub_world(auth_db, monkeypatch) as pw:

        async def previewer(tenant_id: uuid.UUID, account_id: uuid.UUID) -> Any:
            return pw.fake

        previewers.set_previewer_factory(previewer)
        try:
            yield pw
        finally:
            previewers.set_previewer_factory(None)
    get_settings.cache_clear()


def preview_jobs(pw: PubWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    cur = pw.conn.execute(
        "SELECT * FROM job_runs WHERE listing_id = %s AND kind = 'listing_status_sync' "
        "AND payload_json->>'op' = 'preview' ORDER BY queued_at, id",
        [listing_id],
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def preview_rows(pw: PubWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    cur = pw.conn.execute(
        "SELECT * FROM listing_issues WHERE listing_id = %s AND dedupe_key LIKE '%%|preview' "
        "ORDER BY created_at, id",
        [listing_id],
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
