"""S19 T24 (AC 17; CANON.md:137): issues and tasks as `app_rw` on `mos_iso`. An acme issue sync,
dismissal and inbox read never touch or return bharat's rows."""

from __future__ import annotations

import hashlib
import uuid

import psycopg
import pytest
from tests.integration.generation_support import GenWorld, build_gen_world, make_generating

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
async def world(clean_tenants: None, admin_conn: Conn, monkeypatch: pytest.MonkeyPatch) -> GenWorld:
    return await build_gen_world(admin_conn, monkeypatch)


def _hash(conn: Conn, tenant: uuid.UUID) -> str:
    rows = conn.execute(
        "SELECT t::text FROM listing_issues t WHERE tenant_id = %s ORDER BY t::text", [tenant]
    ).fetchall()
    return hashlib.sha256("\n".join(str(r[0]) for r in rows).encode()).hexdigest()


async def _blocked(world: GenWorld, email: str) -> uuid.UUID:
    """A `stub:blocked` generation: the backend re-check opens error issues."""
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    register_bodies()
    accepted = await make_generating(world, email=email, instructions="stub:blocked")
    await run_job(str(accepted.job_id))
    return accepted.listing_id  # type: ignore[no-any-return]


async def test_iso_issue_sync_touches_no_other_tenant_row(world: GenWorld) -> None:
    bharat = world.tenant("bharat")
    b_listing = await _blocked(world, "operator@bharat.test")
    assert world.count("listing_issues", "listing_id = %s", [b_listing]) >= 1
    before = _hash(world.conn, bharat)
    await _blocked(world, "operator@acme.test")
    assert _hash(world.conn, bharat) == before


async def test_iso_issue_and_task_reads_other_tenant_empty(world: GenWorld) -> None:
    from app.core.errors import NotFoundError
    from app.repositories.listing_issues import IssueFilter
    from app.repositories.uow import tenant_uow
    from app.services.issues import dismiss_issue

    b_listing = await _blocked(world, "operator@bharat.test")
    issue = world.scalar("SELECT id FROM listing_issues WHERE listing_id = %s", [b_listing])
    world.conn.execute(
        "INSERT INTO human_tasks (tenant_id, kind, product_id, target_path, question, reason, "
        "expected_answer, resume_from) VALUES (%s, 'missing_fact', %s, 'facts.fabric', "
        "'What is the fabric?', 'The listing needs it.', '{\"kind\": \"text\"}', 'generate')",
        [world.tenant("bharat"), world.product("bharat", "KURTA-002")],
    )
    principal = world.principal("reviewer@acme.test")
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, _ = await uow.listing_issues.page(IssueFilter(), after=None, limit=200)
        assert b_listing not in {r.listing_id for r in rows}
        assert await uow.listing_issues.lock(issue) is None
        assert await uow.listing_issues.latest_closed(b_listing, "x") is None
        tasks, _ = await uow.human_tasks.page("open", after=None, limit=200)
        assert tasks == []
        with pytest.raises(NotFoundError):
            await dismiss_issue(uow, principal, issue)
    assert world.scalar("SELECT status FROM listing_issues WHERE id = %s", [issue]) == "open"
