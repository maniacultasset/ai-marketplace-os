"""The S21 publish test world: S19's review world (the demo seed as `app_rw`, the stub pipeline)
plus an active Amazon account per tenant and an in-process `FakeAdapter` installed as the publish
job's adapter (no network). Helpers make approved listings, call the publish routes and run jobs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from .generation_support import GenWorld, build_gen_world
from .review_support import OPERATOR, REVIEWER, Made, generated, run_job

PUBLISH_URL = "/api/v1/listings/publish"


@dataclass
class PubWorld:
    world: GenWorld
    fake: Any
    accounts: dict[str, uuid.UUID] = field(default_factory=dict)
    sinks: list[Any] = field(default_factory=list)

    @property
    def conn(self) -> Any:
        return self.world.conn

    def listing(self, listing_id: uuid.UUID) -> dict[str, Any]:
        return self.world.listing(listing_id)

    def rows(self, query: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        return self.world.conn.execute(query, params or []).fetchall()

    def scalar(self, query: str, params: list[Any] | None = None) -> Any:
        return self.world.scalar(query, params)

    def submissions(self, listing_id: uuid.UUID) -> list[dict[str, Any]]:
        cur = self.world.conn.execute(
            "SELECT * FROM listing_submissions WHERE listing_id = %s ORDER BY attempt, created_at",
            [listing_id],
        )
        names = [d.name for d in cur.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def job(self, job_id: uuid.UUID | str) -> dict[str, Any]:
        cur = self.world.conn.execute("SELECT * FROM job_runs WHERE id = %s", [job_id])
        names = [d.name for d in cur.description or ()]
        row = cur.fetchone()
        assert row is not None
        return dict(zip(names, row, strict=True))


def insert_account(world: GenWorld, slug: str, seller: str | None = None) -> uuid.UUID:
    owner = world.user(f"owner@{slug}.test")
    row = world.conn.execute(
        "INSERT INTO marketplace_accounts (tenant_id, marketplace, seller_id, token_enc, "
        "kms_key_version, expires_at, connected_by_user_id) VALUES (%s, 'amazon', %s, %s, 'v1', "
        "now() + interval '1 hour', %s) RETURNING id",
        [world.tenant(slug), seller or f"S21{uuid.uuid4().hex[:8].upper()}", b"sealed", owner],
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


@pytest.fixture
async def pub(auth_db: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PubWorld]:
    from app.adapters.fake import FakeAdapter
    from app.db.queue import get_queue_app
    from app.workers import publish as publish_worker
    from app.workers import readers

    get_queue_app.cache_clear()
    world = await build_gen_world(auth_db, monkeypatch)
    fake = FakeAdapter("amazon")
    pw = PubWorld(world, fake)
    for slug in ("acme", "bharat"):
        pw.accounts[slug] = insert_account(world, slug)

    async def factory(tenant_id: uuid.UUID, account_id: uuid.UUID, sink: Any) -> Any:
        pw.sinks.append(sink)
        return pw.fake

    async def reader(tenant_id: uuid.UUID, account_id: uuid.UUID) -> Any:
        return pw.fake

    publish_worker.set_adapter_factory(factory)
    readers.set_reader_factory(reader)
    try:
        yield pw
    finally:
        publish_worker.set_adapter_factory(None)
        readers.set_reader_factory(None)
        get_queue_app.cache_clear()


async def approved(pw: PubWorld, *, sku: str | None = None, maker: str = OPERATOR) -> Made:
    """A generated listing approved by the reviewer (maker-checker: a second member)."""
    from app.repositories.uow import tenant_uow
    from app.services.review import ReviewItem, approve_batch

    made = await generated(pw.world, maker, sku=sku)
    principal = pw.world.principal(REVIEWER)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        outcome = await approve_batch(
            uow,
            principal,
            [ReviewItem(made.listing_id, made.version_id, "approve")],
            http_key=str(uuid.uuid4()),
        )
    assert outcome.items[0].http_status == 200, outcome.items[0]
    return made


async def post_publish(
    api: Any,
    pw: PubWorld,
    email: str,
    items: list[dict[str, Any]],
    *,
    key: str | None = None,
) -> Any:
    return await api.post(PUBLISH_URL, headers=pw.world.headers(email, key), json={"items": items})


async def publish_one(api: Any, pw: PubWorld, made: Made, email: str = OPERATOR) -> dict[str, Any]:
    response = await post_publish(api, pw, email, [{"listing_id": str(made.listing_id)}])
    assert response.status_code == 202, response.text
    (item,) = response.json()["items"]
    return item  # type: ignore[no-any-return]


async def retry(
    api: Any, pw: PubWorld, made: Made, email: str = OPERATOR, key: str | None = None
) -> Any:
    return await api.post(
        f"/api/v1/listings/{made.listing_id}/publish/retry-fields",
        headers=pw.world.headers(email, key),
    )


async def publish_and_run(api: Any, pw: PubWorld, made: Made) -> dict[str, Any]:
    item = await publish_one(api, pw, made)
    assert item["http_status"] == 202, item
    await run_job(uuid.UUID(item["job_id"]))
    return item


__all__ = ["run_job"]
