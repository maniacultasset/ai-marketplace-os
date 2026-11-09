"""Photo rules 2.1.0 (prod demo 9 Nov): a photo re-checked under the new rules reaches the
listing's issues and questions on its next check, without a regenerate; one warning no longer
blocks because another check of the photo failed; `images recheck` queues the photos the old
rules judged."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.cli import run_images_recheck
from mkos_schemas.codes import ImageIssueCode
from mkos_schemas.enums import ImageSlot, Marketplace, Severity
from mkos_schemas.images import ImageCheck, ImageIssue, ImageReport

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .images_support import image_row, images_db, product
from .jobs_support import JobWorld, count
from .review_support import OPERATOR, Made, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world", "images_db"]
CHECKS = {
    "BACKGROUND_OFF": "background",
    "RESOLUTION_LOW": "resolution",
    "WRONG_ASPECT": "aspect_ratio",
}


def _report(image: uuid.UUID, issues: dict[str, Severity | None]) -> str:
    """A stored Amazon report: `issues` code -> severity (None: a report the old rules wrote)."""
    failed = any(s is None or s is Severity.ERROR for s in issues.values())
    report = ImageReport(
        image_id=image,
        marketplace=Marketplace.AMAZON,
        slot=ImageSlot.MAIN,
        pass_=not failed,
        checks=[ImageCheck(check=CHECKS[c], method="deterministic", passed=False) for c in issues],
        issues=[
            ImageIssue(
                code=ImageIssueCode(c),
                check=CHECKS[c],
                suggestion="Fix the photo.",
                severity=s,
            )
            for c, s in issues.items()
        ],
        processed_at=datetime(2026, 11, 9, tzinfo=UTC),
    )
    return json.dumps({"amazon": report.model_dump(mode="json", by_alias=True)})


def _store(conn: Any, image: uuid.UUID, issues: dict[str, Severity | None]) -> None:
    status = "failed" if '"pass": false' in _report(image, issues) else "passed"
    conn.execute(
        "UPDATE images SET image_report_json = %s::jsonb, compliance_status = %s WHERE id = %s",
        [_report(image, issues), status, image],
    )


def _photo_rows(world: GenWorld, listing: uuid.UUID) -> dict[str, tuple[str, str]]:
    """Open `vision_model` rows: marketplace code -> (severity, status)."""
    rows = world.conn.execute(
        "SELECT marketplace_code, severity, status FROM listing_issues "
        "WHERE listing_id = %s AND source = 'vision_model' AND status = 'open'",
        [listing],
    ).fetchall()
    return {code: (severity, status) for code, severity, status in rows}


def _task(world: GenWorld, image: uuid.UUID) -> tuple[str, str | None]:
    row = world.conn.execute(
        "SELECT status, cancelled_reason FROM human_tasks WHERE image_id = %s", [image]
    ).fetchone()
    assert row is not None
    return row[0], row[1]


async def _check_again(world: GenWorld, made: Made) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.listing_validate import validate_listing

    principal = world.principal(OPERATOR)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        await validate_listing(uow, principal, made.listing_id, None)


async def test_a_rechecked_photo_reaches_the_listing_on_check_again(gen_world: GenWorld) -> None:
    sku = "KURTA-002"
    image = f.image(
        gen_world.conn, gen_world.tenant("acme"), gen_world.product("acme", sku), slot="main"
    )
    # the demo photo under the old rules: three checks, every one blocking
    _store(
        gen_world.conn, image, dict.fromkeys(("BACKGROUND_OFF", "RESOLUTION_LOW", "WRONG_ASPECT"))
    )
    made = await generated(gen_world, sku=sku)
    assert _photo_rows(gen_world, made.listing_id) == {
        "BACKGROUND_OFF": ("error", "open"),
        "RESOLUTION_LOW": ("error", "open"),
        "WRONG_ASPECT": ("error", "open"),
    }
    assert _task(gen_world, image)[0] == "open"

    # the re-check under the new rules: the background blocks, a small photo only warns
    _store(
        gen_world.conn,
        image,
        {"BACKGROUND_OFF": Severity.ERROR, "RESOLUTION_LOW": Severity.WARNING},
    )
    await _check_again(gen_world, made)
    assert _photo_rows(gen_world, made.listing_id) == {
        "BACKGROUND_OFF": ("error", "open"),
        "RESOLUTION_LOW": ("warning", "open"),
    }
    assert _task(gen_world, image)[0] == "open"  # the background still needs the seller

    # the seller fixed the background: nothing blocks, the question closes
    _store(gen_world.conn, image, {})
    await _check_again(gen_world, made)
    assert _photo_rows(gen_world, made.listing_id) == {}
    assert _task(gen_world, image) == ("cancelled", "issues_closed")
    # a check that finds the same photo verdict writes nothing new
    await _check_again(gen_world, made)
    assert _photo_rows(gen_world, made.listing_id) == {}


def _queued(jw: JobWorld, tenant: uuid.UUID) -> int:
    return count(
        jw.conn,
        "SELECT count(*) FROM job_runs WHERE tenant_id = %s AND kind = 'image_compliance' "
        "AND status = 'queued'",
        [tenant],
    )


async def test_recheck_queues_only_photos_the_old_rules_judged(
    images_db: JobWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant = images_db.tenant("acme")
    p = product(images_db.conn, tenant)
    old = image_row(images_db.conn, tenant, p, slot="main")
    new = image_row(images_db.conn, tenant, p, slot="secondary")
    clean = image_row(images_db.conn, tenant, p, slot="lifestyle")
    _store(images_db.conn, old, {"RESOLUTION_LOW": None})
    _store(images_db.conn, new, {"RESOLUTION_LOW": Severity.WARNING})
    _store(images_db.conn, clean, {})
    assert await run_images_recheck(tenant) == 0
    assert "queued=1 remaining=0" in capsys.readouterr().out
    assert _queued(images_db, tenant) == 1
    # a second run while the job is queued queues nothing again
    assert await run_images_recheck(tenant) == 0
    assert "queued=0 remaining=0" in capsys.readouterr().out
    assert _queued(images_db, tenant) == 1


async def test_recheck_with_a_staff_email_writes_one_audit_row(
    images_db: JobWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant = images_db.tenant("acme")
    p = product(images_db.conn, tenant)
    old = image_row(images_db.conn, tenant, p, slot="main")
    _store(images_db.conn, old, {"WRONG_ASPECT": None})
    assert await run_images_recheck(tenant, "ops@kuinn.test") == 0
    assert "queued=1 remaining=0" in capsys.readouterr().out
    rows = images_db.conn.execute(
        "SELECT actor_type, actor_email, entity_type, entity_id, reason FROM audit_log "
        "WHERE tenant_id = %s AND action = 'images.recheck'",
        [tenant],
    ).fetchall()
    assert rows == [
        ("staff", "ops@kuinn.test", "tenant", tenant, "photo_rules_recheck queued=1 remaining=0")
    ]
