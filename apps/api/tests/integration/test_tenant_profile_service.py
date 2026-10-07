"""S16 T14 (AC5; CP3, CP6): `app.services.tenants`, as app_rw (the HTTP cases are in
`test_tenants_me_update_api.py`)."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import psycopg
import pytest

from .auth_support import World, audit_rows
from .brand_support import Conn, FakeEmbedder, chunk_rows, principal
from .conftest import LOCAL_HOSTS
from .jobs_support import count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _jobs(conn: Conn) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT id, status FROM job_runs WHERE payload_json->>'source' = 'brand_profile' ORDER BY queued_at"
    ).fetchall()


async def _update(world: World, fields: dict[str, Any]) -> Any:
    from app.services import tenants

    _, tag = await tenants.get_profile(principal(world))
    return await tenants.update_profile(principal(world), fields, tag)


async def test_get_profile_maps_nulls(products_world: World, auth_db: Conn) -> None:
    from app.services import tenants

    view, tag = await tenants.get_profile(principal(products_world))
    assert (view.tone, view.guidelines, view.glossary) == (None, [], {})
    assert tag.startswith('"') and tag.endswith('"')


async def test_update_while_running_enqueues_new(products_world: World, auth_db: Conn) -> None:
    from .brand_support import finish_jobs

    await _update(products_world, {"tone": "One."})
    auth_db.execute(
        "UPDATE job_runs SET status = 'running', started_at = now(), lease_expires_at = now() + interval '1 hour'"
    )
    await _update(products_world, {"tone": "Two."})
    assert [s for _, s in _jobs(auth_db)] == ["running", "queued"]
    finish_jobs(auth_db, "succeeded")


async def test_update_audit_failure_rolls_back(
    products_world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.identity import AuditRepository

    async def boom(self: Any, **_: Any) -> None:
        raise RuntimeError("audit down")

    monkeypatch.setattr(AuditRepository, "write", boom)
    with pytest.raises(RuntimeError):
        await _update(products_world, {"tone": "Never stored."})
    row = auth_db.execute(
        "SELECT tone FROM tenants WHERE id = %s", [products_world.tenant("acme")]
    ).fetchone()
    assert row == (None,) and _jobs(auth_db) == []


async def test_update_audit_changed_keys_only(products_world: World, auth_db: Conn) -> None:
    (name,) = auth_db.execute("SELECT name FROM tenants WHERE id = %s", [products_world.tenant("acme")]).fetchone()  # type: ignore[misc]
    await _update(products_world, {"name": name, "tone": "Warm."})
    before, after = auth_db.execute("SELECT before, after FROM audit_log WHERE action = 'tenant.profile_updated'").fetchone()  # type: ignore[misc]
    assert before == {"tone": None} and after == {"tone": "Warm."}


async def test_etag_changes_on_update(products_world: World, auth_db: Conn) -> None:
    from app.services import tenants

    _, first = await tenants.get_profile(principal(products_world))
    _, returned = await _update(products_world, {"name": "New name"})
    _, second = await tenants.get_profile(principal(products_world))
    assert first != second == returned


async def test_update_secret_guideline_refused_before_tx(
    products_world: World, auth_db: Conn
) -> None:
    from app.core.errors import InputError
    from app.services import tenants

    with pytest.raises(InputError) as info:
        await tenants.update_profile(
            principal(products_world), {"guidelines": ["ok", '{"password": "p"}']}, '"x"'
        )
    assert info.value.field == "guidelines[1]"
    assert audit_rows(auth_db, "tenant.profile_updated") == []


async def test_update_coalesce_race_claim_before_commit(
    products_world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PATCH holds the queued profile job's row lock; a worker's claim waits for the commit,
    and the body then embeds the new values."""
    from app.services import brand_embed
    from app.services.brand_embed import run_brand_embed
    from app.workers.registry import JobContext
    from mkos_schemas.enums import JobKind

    fake = FakeEmbedder()
    monkeypatch.setattr(brand_embed, "embedder", lambda: fake)
    acme = products_world.tenant("acme")
    await _update(products_world, {"guidelines": ["Old."]})
    ((job_id, _),) = _jobs(auth_db)
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    order: list[str] = []

    def claim() -> None:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, false)", [str(acme)])
            conn.execute(
                "UPDATE job_runs SET status = 'running', started_at = now(), lease_expires_at = now() + interval '1 hour' WHERE id = %s AND status = 'queued'",
                [job_id],
            )
            order.append("claimed")

    started = asyncio.Event()

    async def slow_patch() -> None:
        await _update(products_world, {"guidelines": ["New."]})
        order.append("committed")

    from app.repositories.jobs import JobRunRepository

    has_queued = JobRunRepository.has_queued

    async def hold(self: Any, *args: Any, **kwargs: Any) -> bool:
        found = await has_queued(self, *args, **kwargs)
        started.set()
        await asyncio.sleep(0.5)  # the claim must wait on the row lock meanwhile
        return found

    monkeypatch.setattr(JobRunRepository, "has_queued", hold)
    patch = asyncio.create_task(slow_patch())
    await started.wait()
    claimer = asyncio.create_task(asyncio.to_thread(claim))
    await patch
    await claimer
    assert order == ["committed", "claimed"]
    assert (
        count(
            auth_db, "SELECT count(*) FROM job_runs WHERE payload_json->>'source' = 'brand_profile'"
        )
        == 1
    )
    ctx = JobContext(job_id, acme, JobKind.BRAND_EMBED, "req-race", {"source": "brand_profile"}, 1)
    await run_brand_embed(ctx)
    assert [r["text"] for r in chunk_rows(auth_db, acme)] == ["New."]
