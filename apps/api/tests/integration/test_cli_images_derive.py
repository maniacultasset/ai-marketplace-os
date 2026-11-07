"""SI1 T22 (CP6): `images derive --tenant` queues the checks for live images with no clean copy,
within the free live-job cap. Run in the Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

import uuid

import pytest
from app.cli import build_parser, run_images_derive

from .conftest import LOCAL_HOSTS
from .images_support import image_row, images_db, product
from .jobs_support import JobWorld, count, insert_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]


def _checked(jw: JobWorld, tenant: uuid.UUID, n: int, *, clean: bool = False) -> list[uuid.UUID]:
    p = product(jw.conn, tenant)
    ids = []
    for _ in range(n):
        image_id = image_row(jw.conn, tenant, p, slot="secondary")
        jw.conn.execute(
            "UPDATE images SET compliance_status = 'passed', clean_content_hash = %s, "
            "clean_mime_type = %s WHERE id = %s",
            ["e" * 64 if clean else None, "image/jpeg" if clean else None, image_id],
        )
        ids.append(image_id)
    return ids


def _queued(jw: JobWorld, tenant: uuid.UUID) -> int:
    return count(
        jw.conn,
        "SELECT count(*) FROM job_runs WHERE tenant_id = %s AND kind = 'image_compliance' "
        "AND status = 'queued'",
        [tenant],
    )


async def test_queues_only_images_without_derivatives(
    images_db: JobWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant = images_db.tenant("acme")
    _checked(images_db, tenant, 2)
    _checked(images_db, tenant, 3, clean=True)
    assert await run_images_derive(tenant) == 0
    assert "queued=2 remaining=0" in capsys.readouterr().out
    assert _queued(images_db, tenant) == 2


async def test_respects_the_cap(images_db: JobWorld, capsys: pytest.CaptureFixture[str]) -> None:
    tenant = images_db.tenant("acme")
    for _ in range(3):
        insert_job(
            images_db.conn,
            tenant,
            kind="image_compliance",
            entity_type="image",
            entity_id=uuid.uuid4(),
        )
    _checked(images_db, tenant, 25)
    assert await run_images_derive(tenant) == 0
    assert "queued=17 remaining=8" in capsys.readouterr().out


def test_tenant_is_required() -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["images", "derive"])
    assert caught.value.code == 2
