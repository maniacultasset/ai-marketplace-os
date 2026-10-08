"""S18 T15 (DW1, DW3, AC 2, 6; D7): the generation terminal writer, as app_rw.

A version is saved only while the listing is still `generating` and this job is its newest
`listing_generate` job. A second call, or an older job, writes nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, ImageSlot, Marketplace, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.images import ImageCheck, ImageReport
from mkos_schemas.listing import CanonicalListing, ListingContent

from .conftest import LOCAL_HOSTS
from .generation_support import CATEGORY, GenWorld, gen_world, make_generating
from .jobs_support import insert_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
OPERATOR = "operator@acme.test"


def _passed() -> ErrorReport:
    return ErrorReport(status="passed", errors=[])


def _blocking() -> ErrorReport:
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.FIELD_TOO_LONG,
        errors=[
            ErrorItem(
                field="content.title",
                error_code=ErrorCode.FIELD_TOO_LONG,
                severity=Severity.ERROR,
                message="too long",
                source=ErrorSource.RULES_ENGINE,
                retryable=False,
            )
        ],
    )


def _record(world: GenWorld, job_id: uuid.UUID, listing_id: uuid.UUID) -> Any:
    from decimal import Decimal

    from mkos_schemas.agent import AgentRunRecord

    now = datetime.now(UTC)
    return AgentRunRecord(
        id=uuid.uuid4(),
        tenant_id=world.tenant("acme"),
        listing_id=listing_id,
        job_id=job_id,
        request_id="req-t15",
        agent_name="content",
        trigger="generate",
        model_used="stub",
        cost_usd=Decimal("0.100000"),
        status="completed",
        started_at=now,
        created_at=now,
    )


def _body(world: GenWorld, product_id: uuid.UUID) -> CanonicalListing:
    return CanonicalListing(
        product_id=product_id,
        marketplace=Marketplace.AMAZON,
        category=CATEGORY,
        content=ListingContent(
            title="Silk kurta", bullets=["soft weave"], description="A silk kurta."
        ),
    )


def _pins(world: GenWorld) -> Any:
    from app.services.generation_persist import Pins

    row = world.conn.execute(
        "SELECT id, version FROM category_templates "
        "WHERE marketplace = 'amazon' AND category = %s AND is_current",
        [CATEGORY],
    ).fetchone()
    assert row is not None
    return Pins(
        rules_version=1,
        template_id=row[0],
        template_version=row[1],
        facts_digest=b"\x11" * 32,
    )


def _plan(world: GenWorld, accepted: Any, **kw: Any) -> Any:
    from app.services.generation_persist import PersistPlan
    from app.services.generation_result import VALIDATED
    from app.services.pipeline_deps import RulesCall

    listing_id = accepted.listing_id
    calls = kw.pop(
        "rules_calls",
        (RulesCall(attempt=1, report=_passed(), duration_ms=4),),
    )
    return PersistPlan(
        tenant_id=world.tenant("acme"),
        job_id=accepted.job_id,
        listing_id=listing_id,
        marketplace=Marketplace.AMAZON,
        request_id="req-t15",
        created_by=world.user(OPERATOR),
        move=kw.pop("move", VALIDATED),
        pins=_pins(world),
        rules_calls=calls,
        records=kw.pop("records", (_record(world, accepted.job_id, listing_id),)),
        listing=kw.pop("listing", _body(world, world.product("acme", "KURTA-002"))),
        recheck=kw.pop("recheck", _passed()),
        attempts_used=kw.pop("attempts_used", len(calls)),
        **kw,
    )


async def _persist(world: GenWorld, plan: Any) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation_persist import make_persist

    async with tenant_uow(world.tenant("acme"), user_id=world.user(OPERATOR)) as uow:
        await make_persist(plan)(uow)


def _versions(world: GenWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return world.conn.execute(
        "SELECT version, origin, parent_version_id, attempts_used, generated_by_user_id, "
        "job_id, image_reports_json FROM listing_versions WHERE listing_id = %s ORDER BY version",
        [listing_id],
    ).fetchall()


def _runs(world: GenWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return world.conn.execute(
        "SELECT attempt, listing_version_id FROM validation_runs WHERE listing_id = %s "
        "ORDER BY attempt",
        [listing_id],
    ).fetchall()


def _finish(world: GenWorld, job_id: uuid.UUID) -> None:
    """The first generate job is still `queued`. A second live job for the listing is refused
    until this one leaves the live set."""
    with world.conn.transaction():
        world.conn.execute("SET LOCAL session_replication_role = replica")
        world.conn.execute(
            "UPDATE job_runs SET status = 'succeeded', finished_at = now() WHERE id = %s",
            [job_id],
        )


def _agents(world: GenWorld, job_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return world.conn.execute(
        "SELECT listing_version_id FROM agent_runs WHERE job_id = %s",
        [job_id],
    ).fetchall()


async def test_persist_happy_rows(gen_world: GenWorld) -> None:
    accepted = await make_generating(gen_world)
    await _persist(gen_world, _plan(gen_world, accepted))
    versions = _versions(gen_world, accepted.listing_id)
    assert len(versions) == 1
    version, origin, parent, attempts, generated_by, job_id, _images = versions[0]
    assert (version, origin, parent, attempts, generated_by, job_id) == (
        1,
        "generation",
        None,
        1,
        gen_world.user(OPERATOR),
        accepted.job_id,
    )
    runs = _runs(gen_world, accepted.listing_id)
    assert [attempt for attempt, _version in runs] == [1, 2]
    assert runs[0][1] is None
    assert runs[1][1] is not None
    agents = _agents(gen_world, accepted.job_id)
    assert len(agents) == 1 and agents[0][0] == runs[1][1]
    row = gen_world.listing(accepted.listing_id)
    assert row["status"] == "validated"
    assert row["current_version_id"] == runs[1][1]


async def test_persist_regeneration_origin_and_parent(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_regeneration

    accepted = await make_generating(gen_world)
    await _persist(gen_world, _plan(gen_world, accepted))
    _finish(gen_world, accepted.job_id)
    parent = gen_world.listing(accepted.listing_id)["current_version_id"]
    principal = gen_world.principal(OPERATOR)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        again = await request_regeneration(
            uow, principal, str(uuid.uuid4()), accepted.listing_id, "shorter title"
        )
    await _persist(gen_world, _plan(gen_world, again, attempts_used=2))
    versions = _versions(gen_world, accepted.listing_id)
    assert [(row[0], row[1], row[2]) for row in versions] == [
        (1, "generation", None),
        (2, "regeneration", parent),
    ]


async def test_persist_failed_no_version_runs_kept_version_null(gen_world: GenWorld) -> None:
    from app.services.generation_result import FAILED

    accepted = await make_generating(gen_world)
    await _persist(gen_world, _plan(gen_world, accepted, move=FAILED, listing=None))
    assert _versions(gen_world, accepted.listing_id) == []
    runs = _runs(gen_world, accepted.listing_id)
    assert len(runs) == 1 and runs[0][1] is None
    agents = _agents(gen_world, accepted.job_id)
    assert len(agents) == 1 and agents[0][0] is None
    assert gen_world.listing(accepted.listing_id)["status"] == "generating"


async def test_terminal_write_once(gen_world: GenWorld) -> None:
    accepted = await make_generating(gen_world)
    plan = _plan(gen_world, accepted)
    await _persist(gen_world, plan)
    await _persist(gen_world, plan)
    assert len(_versions(gen_world, accepted.listing_id)) == 1
    assert len(_runs(gen_world, accepted.listing_id)) == 2
    assert len(_agents(gen_world, accepted.job_id)) == 1


async def test_persist_old_job_not_owner_writes_nothing(gen_world: GenWorld) -> None:
    from datetime import timedelta

    accepted = await make_generating(gen_world)
    _finish(gen_world, accepted.job_id)
    insert_job(
        gen_world.conn,
        gen_world.tenant("acme"),
        kind="listing_generate",
        entity_type="listing",
        entity_id=accepted.listing_id,
        listing_id=accepted.listing_id,
        queued_at=datetime.now(UTC) + timedelta(seconds=5),
    )
    await _persist(gen_world, _plan(gen_world, accepted))
    assert _versions(gen_world, accepted.listing_id) == []
    assert _runs(gen_world, accepted.listing_id) == []
    assert _agents(gen_world, accepted.job_id) == []
    assert gen_world.listing(accepted.listing_id)["status"] == "generating"


async def test_persist_attempt_4_recheck(gen_world: GenWorld) -> None:
    from app.services.pipeline_deps import RulesCall

    accepted = await make_generating(gen_world)
    calls = tuple(RulesCall(attempt=n, report=_passed(), duration_ms=1) for n in (1, 2, 3))
    await _persist(
        gen_world,
        _plan(gen_world, accepted, rules_calls=calls, recheck=_blocking(), attempts_used=3),
    )
    runs = _runs(gen_world, accepted.listing_id)
    assert [attempt for attempt, _version in runs] == [1, 2, 3, 4]
    assert [version for _attempt, version in runs[:3]] == [None, None, None]
    assert runs[3][1] is not None
    assert _versions(gen_world, accepted.listing_id)[0][3] == 3


async def test_persist_image_reports_vision_warn(gen_world: GenWorld) -> None:
    accepted = await make_generating(gen_world)
    image_id = uuid.uuid4()
    report = ImageReport(
        image_id=image_id,
        marketplace=Marketplace.AMAZON,
        slot=ImageSlot.MAIN,
        pass_=False,
        checks=[ImageCheck(check="watermark", method="vision", passed=False)],
        processed_at=datetime.now(UTC),
    )
    await _persist(gen_world, _plan(gen_world, accepted, image_reports=(report,)))
    stored = _versions(gen_world, accepted.listing_id)[0][6]
    assert stored[0]["image_id"] == str(image_id)
    assert stored[0]["checks"][0]["method"] == "vision"
    assert stored[0]["pass"] is False
    assert gen_world.listing(accepted.listing_id)["status"] == "validated"
