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


# ---- SI4 A18 (AC14): the fix trail and the questions stay inside their tenant ----


async def _generate(world: GenWorld, email: str, instructions: str | None) -> uuid.UUID:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    register_bodies()
    accepted = await make_generating(world, email=email, instructions=instructions)
    await run_job(str(accepted.job_id))
    return accepted.listing_id  # type: ignore[no-any-return]


def _require_fiber(world: GenWorld) -> None:
    from tests.integration import factories as f

    f.rule(
        world.conn,
        None,
        category="ethnic_wear_kurta_sets",
        field="attributes.fiber_composition",
        rule_type="required_attribute",
        subject="fiber_composition",
        rule_def=f.rule_def(
            "required_attribute",
            attribute="fiber_composition",
            error_code="MISSING_ATTRIBUTE",
            message="The {attribute} is required.",
            policy_ref=f.POLICY_REF,
        ),
    )


def _drop_fiber(monkeypatch: pytest.MonkeyPatch, drop: list[bool]) -> None:
    from app.workers import generation

    async def run(ctx: object, deps: object) -> object:
        from mkos_pipeline import run_listing_pipeline

        result = await run_listing_pipeline(ctx, deps)  # type: ignore[arg-type]
        if not drop[0] or result.listing is None:
            return result
        attributes = {
            k: v for k, v in result.listing.attributes.items() if k != "fiber_composition"
        }
        listing = result.listing.model_copy(update={"attributes": attributes})
        return result.model_copy(update={"listing": listing})

    monkeypatch.setattr(generation, "run_listing_pipeline", run)


async def test_iso_auto_fix_trail_and_questions_per_tenant(
    world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.listing_issues import IssueFilter
    from app.repositories.uow import tenant_uow

    drop = [False]
    _drop_fiber(monkeypatch, drop)
    fixed = {
        slug: await _generate(world, f"operator@{slug}.test", "stub:long_title")
        for slug in ("acme", "bharat")
    }
    _require_fiber(world)
    drop[0] = True
    asked = {
        slug: await _generate(world, f"operator@{slug}.test", None) for slug in ("acme", "bharat")
    }
    for slug in ("acme", "bharat"):
        tenant = world.tenant(slug)
        assert (
            world.count("listing_issues", "tenant_id = %s AND status = 'auto_fixed'", [tenant]) == 1
        )
        assert (
            world.count("audit_log", "tenant_id = %s AND action = 'listing.auto_fixed'", [tenant])
            == 1
        )
        assert world.count("human_tasks", "tenant_id = %s AND kind = 'missing_fact'", [tenant]) == 1
    for slug, other in (("acme", "bharat"), ("bharat", "acme")):
        principal = world.principal(f"reviewer@{slug}.test")
        async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
            rows, _ = await uow.listing_issues.page(
                IssueFilter(status="auto_fixed"), after=None, limit=200
            )
            assert {r.listing_id for r in rows} == {fixed[slug]}
            tasks, _ = await uow.human_tasks.page("open", after=None, limit=200)
            assert {t.tenant_id for t in tasks} == {principal.tenant_id}
            assert await uow.listing_issues.live_task_ids(asked[other]) == []


async def test_iso_question_and_trail_writes_refuse_other_tenant_ids(
    world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.auto_fix.fixers import FixRecord
    from app.services.issues import record_auto_fixes
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import FixAction

    _require_fiber(world)
    _drop_fiber(monkeypatch, [True])
    a_listing = await _generate(world, "operator@acme.test", None)
    a_task = world.scalar(
        "SELECT human_task_id FROM listing_issues WHERE listing_id = %s", [a_listing]
    )
    assert a_task is not None
    a_product = world.product("acme", "KURTA-002")
    a_issue = world.scalar("SELECT id FROM listing_issues WHERE listing_id = %s", [a_listing])
    before = _hash(world.conn, world.tenant("acme"))
    record = FixRecord(
        field="content.title",
        error_code=ErrorCode.FIELD_TOO_LONG,
        fix_action=FixAction.REFORMAT,
        rule_id=None,
        before_sha="0" * 64,
        after_sha="1" * 64,
        before_size=212,
        after_size=196,
        unit="chars",
        action_taken="Shortened the title at a word boundary.",
    )
    bharat = world.principal("reviewer@bharat.test")
    async with tenant_uow(bharat.tenant_id, user_id=bharat.user_id) as uow:
        assert await uow.human_tasks.cancel_if_closed(a_task) is False
        assert await uow.listing_issues.close_auto_fixed(a_issue, "x" * 10) is False
        assert await uow.listing_issues.task_link_counts(a_task) == (0, 0)
    for attempt in ("fact", "image", "trail"):
        with pytest.raises(Exception):  # noqa: B017 - the FK (tenant, id) or the guard refuses
            async with tenant_uow(bharat.tenant_id, user_id=bharat.user_id) as uow:
                if attempt == "fact":
                    await uow.human_tasks.open_fact_task(
                        a_product,
                        "facts.attributes.fiber_composition",
                        "Add the fiber composition.",
                        "The marketplace needs it.",
                        {"kind": "percent_composition"},
                    )
                elif attempt == "image":
                    image = world.scalar(
                        "INSERT INTO images (tenant_id, product_id, storage_key, slot) "
                        "VALUES (%s, %s, %s, 'main') RETURNING id",
                        [world.tenant("acme"), a_product, f"{uuid.uuid4()}.jpg"],
                    )
                    await uow.human_tasks.open_image_task(
                        a_product, image, "Upload one.", "Failed."
                    )
                else:

                    class _Listing:
                        id = a_listing
                        tenant_id = bharat.tenant_id
                        marketplace = "amazon"

                    await record_auto_fixes(
                        uow,
                        _Listing(),
                        [record],
                        run_id=None,
                        version_id=None,
                        version=1,
                        status="validated",
                        job_id=None,
                        critical_keys=frozenset(),
                    )
    assert _hash(world.conn, world.tenant("acme")) == before
    assert world.scalar("SELECT status FROM human_tasks WHERE id = %s", [a_task]) == "open"
