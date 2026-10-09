"""S18 T4 (AC 1, 10, 12; DATABASE.md:795-823): `ValidationRunRepository` and
`ReviewDecisionRepository` as `app_rw`."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .jobs_support import insert_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
DIGEST = hashlib.sha256(b"facts").digest()
TEMPLATE = uuid.uuid4()


@pytest.fixture
def world(products_world: Any) -> Any:
    return products_world


def _uow(tenant: uuid.UUID, user: uuid.UUID | None = None) -> Any:
    from app.repositories.uow import tenant_uow

    return tenant_uow(tenant, user_id=user)


def _template(conn: Any) -> tuple[uuid.UUID, int]:
    from .ops_support import category_template

    return category_template(conn), 1


def _result(tenant: uuid.UUID, listing: uuid.UUID, version: uuid.UUID | None, **kw: Any) -> Any:
    from mkos_schemas.enums import ErrorSource, Marketplace
    from mkos_schemas.errors import ErrorReport
    from mkos_schemas.validation import ValidationResult

    report = kw.pop("report", ErrorReport(status="passed", errors=[]))
    return ValidationResult(
        tenant_id=tenant,
        listing_id=listing,
        listing_version_id=version,
        marketplace=Marketplace.AMAZON,
        source=kw.pop("source", ErrorSource.RULES_ENGINE),
        passed=report.passed,
        attempt=kw.pop("attempt", 1),
        rules_version=kw.pop("rules_version", 7),
        report=report,
        created_at=datetime.now(UTC),
    )


async def test_vr_insert_rules_engine_needs_pins(world: Any) -> None:
    from app.core.errors import ConflictError

    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    with pytest.raises(ConflictError):
        async with _uow(acme) as uow:
            await uow.validation_runs.insert(
                _result(acme, listing, None),
                template_id=None,
                template_version=None,
                facts_digest=None,
            )


@pytest.mark.parametrize(("attempt", "ok"), [(4, True), (5, False)])
async def test_vr_attempt_4_ok_5_refused(world: Any, attempt: int, ok: bool) -> None:
    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    template, tv = _template(world.conn)

    async def write() -> None:
        async with _uow(acme) as uow:
            await uow.validation_runs.insert(
                _result(acme, listing, None, attempt=attempt),
                template_id=template,
                template_version=tv,
                facts_digest=DIGEST,
            )

    if ok:
        await write()
    else:
        with pytest.raises(Exception):  # noqa: B017 - the CHECK, as ConflictError
            await write()


@pytest.mark.parametrize("changed", ["none", "rules", "template", "template_version", "digest"])
async def test_vr_find_equal_pins_hit_and_miss(world: Any, changed: str) -> None:
    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    version = f.version(world.conn, acme, listing)
    template, tv = _template(world.conn)
    async with _uow(acme) as uow:
        run = await uow.validation_runs.insert(
            _result(acme, listing, version),
            template_id=template,
            template_version=tv,
            facts_digest=DIGEST,
        )
    pins: dict[str, Any] = {
        "rules_version": 7,
        "template_id": template,
        "template_version": tv,
        "facts_digest": DIGEST,
    }
    if changed == "rules":
        pins["rules_version"] = 8
    elif changed == "template":
        pins["template_id"] = uuid.uuid4()
    elif changed == "template_version":
        pins["template_version"] = tv + 1
    elif changed == "digest":
        pins["facts_digest"] = hashlib.sha256(b"other").digest()
    async with _uow(acme) as uow:
        found = await uow.validation_runs.find_equal_pins(version, **pins)
    assert (found is not None and found.id == run.id) is (changed == "none")


async def test_vr_app_rw_cannot_update(world: Any) -> None:
    from sqlalchemy import text

    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    run = f.validation_run(world.conn, acme, listing)
    with pytest.raises(Exception) as caught:
        async with _uow(acme) as uow:
            await uow.session.execute(
                text("UPDATE validation_runs SET passed = false WHERE id = :r"), {"r": run}
            )
    assert "permission denied" in str(caught.value) or "append-only" in str(caught.value)


async def test_vr_summary_for(world: Any) -> None:
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import ErrorSource
    from mkos_schemas.errors import ErrorItem, ErrorReport

    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    version = f.version(world.conn, acme, listing)
    template, tv = _template(world.conn)
    failed = ErrorReport(
        status="failed",
        errors=[
            ErrorItem(
                field="content.title",
                error_code=ErrorCode.FIELD_TOO_LONG,
                message="m",
                source=ErrorSource.RULES_ENGINE,
                retryable=False,
            )
        ],
    )
    async with _uow(acme) as uow:
        await uow.validation_runs.insert(
            _result(acme, listing, version, report=failed),
            template_id=template,
            template_version=tv,
            facts_digest=DIGEST,
        )
        summary = await uow.validation_runs.summary_for(version)
    assert summary is not None
    assert (summary.passed, summary.blocking_count, len(summary.stages)) == (False, 1, 1)


# ---- review_decisions ----


def _generate_job(conn: Any, tenant: uuid.UUID, listing: uuid.UUID) -> uuid.UUID:
    return insert_job(
        conn,
        tenant,
        kind="listing_generate",
        entity_type="listing",
        entity_id=listing,
        listing_id=listing,
    )


async def test_rd_regenerate_requested_needs_job_id(world: Any) -> None:
    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    expect = world.conn.execute
    with pytest.raises(Exception, match="ck_review_decisions_regeneration_job"):
        expect(
            "INSERT INTO review_decisions (tenant_id, listing_id, decision, decided_by_user_id) "
            "VALUES (%s, %s, 'regenerate_requested', %s)",
            [acme, listing, world.user("owner@acme.test")],
        )


async def test_rd_null_version_allowed(world: Any) -> None:
    acme = world.tenant("acme")
    operator = world.user("operator@acme.test")
    listing = f.listing(world.conn, acme)
    job = _generate_job(world.conn, acme, listing)
    async with _uow(acme, operator) as uow:
        decision = await uow.review_decisions.insert_regenerate_requested(
            listing, None, "shorter title", job
        )
    row = world.conn.execute(
        "SELECT listing_version_id, decided_by_user_id, instructions FROM review_decisions "
        "WHERE id = %s",
        [decision],
    ).fetchone()
    assert row == (None, operator, "shorter title")


async def test_rd_latest_rejects_5_newest_first(world: Any) -> None:
    acme = world.tenant("acme")
    listing = f.listing(world.conn, acme)
    version = f.version(world.conn, acme, listing)
    base = datetime(2026, 10, 1, tzinfo=UTC)
    for n in range(7):
        f.review_decision(
            world.conn,
            acme,
            listing,
            version,
            decision="reject",
            reason_code="other",
            feedback=f"feedback {n}",
            decided_at=base + timedelta(minutes=n),
        )
    async with _uow(acme) as uow:
        got = await uow.review_decisions.latest_reject_feedback(listing, 5)
    assert got == [f"feedback {n}" for n in (6, 5, 4, 3, 2)]


async def test_rd_instructions_for_job(world: Any) -> None:
    acme = world.tenant("acme")
    operator = world.user("operator@acme.test")
    listing = f.listing(world.conn, acme)
    job = _generate_job(world.conn, acme, listing)
    async with _uow(acme, operator) as uow:
        await uow.review_decisions.insert_regenerate_requested(listing, None, "use silk", job)
    async with _uow(acme) as uow:
        assert await uow.review_decisions.instructions_for_job(job) == "use silk"
        assert await uow.review_decisions.instructions_for_job(uuid.uuid4()) is None


async def test_rd_other_tenant_absent(world: Any) -> None:
    bharat = world.tenant("bharat")
    listing = f.listing(world.conn, bharat)
    version = f.version(world.conn, bharat, listing)
    f.review_decision(
        world.conn, bharat, listing, version, decision="reject", reason_code="other", feedback="b"
    )
    async with _uow(world.tenant("acme")) as uow:
        assert await uow.review_decisions.latest_reject_feedback(listing) == []
