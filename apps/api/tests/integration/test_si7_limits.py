"""SI7 (AC18, PLAN D21): the image-edit limits keep edits from starving listing generation.

Worker side (T12): the tenant's daily count of AI candidates and the platform's image share are
checked in `reserve`, under the budget locks, before the caps. The create side (T13) is at the
end of this file."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from app.workers import image_edit_seam
from app.workers.budget import IMAGE_EDIT_RESERVATION_USD, BudgetRefused, ImageEditLimits, reserve

from . import factories as f
from .candidates_support import (
    candidate_row,
    edit_on,
    enqueue_candidate,
    photo,
    source_main,
    use_candidate_storage,
)
from .conftest import LOCAL_HOSTS
from .images_support import FakeStorage, images_db, run_image_job
from .jobs_support import JobWorld

__all__ = ["edit_on", "images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_candidate_storage(monkeypatch, fake)
    return fake


def _queued_ai(jw: JobWorld, tenant: uuid.UUID, n: int, **cols: Any) -> None:
    p = f.product(jw.conn, tenant, category="kurtas")
    src = f.image(
        jw.conn, tenant, p, slot="main", storage_key=f"{tenant}/{p}/images/{uuid.uuid4()}"
    )
    for _ in range(n):
        f.image_candidate(jw.conn, tenant, p, src, kind="white_background", **cols)


async def test_the_tenant_limit_is_checked_at_reserve(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    tenant = images_db.tenant("acme")
    _queued_ai(images_db, tenant, 10)  # ten candidates queued past the create check (a race)
    p, src, digest = source_main(images_db.conn, store, tenant, clean=photo())
    candidate, job = await enqueue_candidate(tenant, p, src, digest, kind="white_background")
    calls: list[Any] = []
    edit_on.setattr(image_edit_seam, "call_edit_image", lambda *a: calls.append(a))
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "budget") and calls == []


async def test_nine_others_still_allow_the_tenth(images_db: JobWorld) -> None:
    from app.db.session import unit_of_work
    from app.workers.budget import image_edit_refusal, lock_budget

    tenant = images_db.tenant("acme")
    _queued_ai(images_db, tenant, 9)
    limits = ImageEditLimits(tenant_daily_limit=10, platform_share=Decimal("0.5"))
    async with unit_of_work(tenant) as session:
        await lock_budget(session, tenant)
        day = datetime.now(UTC).date()
        assert (
            await image_edit_refusal(session, tenant, day, limits, IMAGE_EDIT_RESERVATION_USD)
            is None
        )
    _queued_ai(images_db, tenant, 1)
    async with unit_of_work(tenant) as session:
        await lock_budget(session, tenant)
        got = await image_edit_refusal(session, tenant, day, limits, IMAGE_EDIT_RESERVATION_USD)
    assert got == "image_tenant"


async def test_the_platform_share_refuses_another_tenant_but_not_generation(
    images_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "1")
    monkeypatch.setenv("LLM_TENANT_DAILY_BUDGET_USD", "1")
    get_settings.cache_clear()
    acme, bharat = images_db.tenant("acme"), images_db.tenant("bharat")
    f.agent_run(
        images_db.conn,
        acme,
        agent_name="image",
        trigger="vision",
        prompt_version="image_edit/white_bg/v1",
        cost_usd="0.48",
        started_at=datetime.now(UTC),
    )
    limits = ImageEditLimits(tenant_daily_limit=10, platform_share=Decimal("0.5"))
    with pytest.raises(BudgetRefused) as refused:
        await reserve(bharat, uuid.uuid4(), 1, amount=IMAGE_EDIT_RESERVATION_USD, image_edit=limits)
    assert refused.value.scope == "image_global"
    # listing generation keeps the rest of both caps: no refusal (the job row is absent, so
    # the fenced write reports False instead)
    assert await reserve(bharat, uuid.uuid4(), 1) is False
    get_settings.cache_clear()


async def test_fix_and_yesterdays_edits_are_not_counted(images_db: JobWorld) -> None:
    from app.db.session import unit_of_work
    from app.workers.budget import image_edit_refusal, lock_budget

    tenant = images_db.tenant("acme")
    p = f.product(images_db.conn, tenant, category="kurtas")
    src = f.image(images_db.conn, tenant, p, slot="main", storage_key=f"{tenant}/{p}/images/x")
    for _ in range(5):
        f.image_candidate(images_db.conn, tenant, p, src)  # fix
    for _ in range(5):
        f.image_candidate(
            images_db.conn,
            tenant,
            p,
            src,
            kind="white_background",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
    limits = ImageEditLimits(tenant_daily_limit=1, platform_share=Decimal("0.5"))
    async with unit_of_work(tenant) as session:
        await lock_budget(session, tenant)
        day = datetime.now(UTC).date()
        assert (
            await image_edit_refusal(session, tenant, day, limits, IMAGE_EDIT_RESERVATION_USD)
            is None
        )


# ---- create side (T13): a refused create is a `failed/budget` row with no job ----

import httpx  # noqa: E402

from .images_support import headers  # noqa: E402
from .jobs_support import count  # noqa: E402


async def test_the_eleventh_ai_create_of_the_day_is_budget(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    tenant = images_db.tenant("acme")
    _queued_ai(images_db, tenant, 10)
    p, src, _ = source_main(images_db.conn, store, tenant)
    response = await api.post(
        f"/api/v1/products/{p}/image-candidates",
        json={"kind": "white_background", "source_image_id": str(src)},
        headers=headers(images_db),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "failed" and body["job_id"] is None
    row = candidate_row(images_db.conn, uuid.UUID(body["candidate_id"]))
    assert row["failure_reason"] == "budget"
    assert (
        count(images_db.conn, "SELECT count(*) FROM job_runs WHERE entity_id = %s", [row["id"]])
        == 0
    )
    assert count(images_db.conn, "SELECT count(*) FROM agent_runs") == 0
    # the fix of the same photo is never limited
    fix = await api.post(
        f"/api/v1/products/{p}/image-candidates",
        json={"kind": "fix", "source_image_id": str(src)},
        headers=headers(images_db),
    )
    assert fix.json()["status"] == "queued"


async def test_the_platform_share_refuses_a_create(
    images_db: JobWorld,
    api: httpx.AsyncClient,
    store: FakeStorage,
    edit_on: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    edit_on.setenv("LLM_DAILY_BUDGET_USD", "1")
    get_settings.cache_clear()
    f.agent_run(
        images_db.conn,
        images_db.tenant("bharat"),
        agent_name="image",
        trigger="vision",
        prompt_version="image_edit/white_bg/v1",
        cost_usd="0.48",
        started_at=datetime.now(UTC),
    )
    p, src, _ = source_main(images_db.conn, store, images_db.tenant("acme"))
    response = await api.post(
        f"/api/v1/products/{p}/image-candidates",
        json={"kind": "white_background", "source_image_id": str(src)},
        headers=headers(images_db),
    )
    assert response.json()["status"] == "failed" and response.json()["job_id"] is None
