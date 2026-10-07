"""S17b T7 (DW1; AC8-AC10; PLAN D5): the `image_compliance` job body through the real wrapper,
on a fake object store (the MinIO leg is T16's flow)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.adapters.storage import StorageUnavailable
from app.services import image_runner
from app.services.image_runner import CheckOutcome
from mkos_schemas.images import ImageReport

from .conftest import LOCAL_HOSTS
from .images_support import (
    FakeStorage,
    enqueue_image_job,
    image_db_row,
    image_row,
    images_db,
    picture,
    product,
    run_image_job,
    use_storage,
)
from .jobs_support import JobWorld, count, job_row

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_storage(monkeypatch, fake)
    return fake


async def checked(
    jw: JobWorld, store: FakeStorage, data: bytes, slot: str = "main", **kw: Any
) -> tuple[uuid.UUID, uuid.UUID]:
    acme = jw.tenant("acme")
    p = product(jw.conn, acme)
    image_id = image_row(jw.conn, acme, p, slot=slot)
    key = image_db_row(jw.conn, image_id)["storage_key"]
    store.objects[key] = (data, "image/jpeg", datetime.now(UTC))
    job_id = await enqueue_image_job(acme, image_id)
    await run_image_job(job_id, **kw)
    return image_id, job_id


async def test_image_job_writes_reports_both_marketplaces(
    images_db: JobWorld, store: FakeStorage
) -> None:
    data = picture(1000, 1000)
    image_id, job_id = await checked(images_db, store, data)
    row = image_db_row(images_db.conn, image_id)
    assert set(row["image_report_json"]) == {"amazon", "flipkart"}
    for market, report in row["image_report_json"].items():
        assert "pass" in report and "pass_" not in report
        # the wire model the exported ImageReport.schema.json is generated from (schemas-drift)
        assert ImageReport.model_validate(report).marketplace.value == market
    assert (row["mime_type"], row["width_px"], row["height_px"]) == ("image/jpeg", 1000, 1000)
    assert row["content_hash"] == hashlib.sha256(data).hexdigest()
    job = job_row(images_db.conn, job_id)
    assert job["status"] == "succeeded"
    assert job["result_json"] == {
        "image_id": str(image_id),
        "compliance_status": row["compliance_status"],
    }


async def test_image_job_800_white_jpeg(images_db: JobWorld, store: FakeStorage) -> None:
    image_id, _ = await checked(images_db, store, picture(800, 800))
    row = image_db_row(images_db.conn, image_id)
    amazon, flipkart = row["image_report_json"]["amazon"], row["image_report_json"]["flipkart"]
    assert amazon["pass"] is False and [i["code"] for i in amazon["issues"]] == ["RESOLUTION_LOW"]
    assert flipkart["pass"] is True and [i["code"] for i in flipkart["issues"]] == [
        "RESOLUTION_LOW"
    ]
    assert row["compliance_status"] == "failed"


def _bomb() -> bytes:
    from tests.image_fixtures import png_header_only

    return png_header_only(30_000, 30_000)


@pytest.mark.parametrize(
    ("case", "data", "code"),
    [
        ("corrupt", b"\xff\xd8\xff" + b"\x00" * 400, "CORRUPT"),
        ("truncated", picture(1000, 1000)[:600], "CORRUPT"),
        ("webp", picture(1000, 1000, fmt="WEBP"), "FORMAT_UNSUPPORTED"),
        ("pixel_bomb", _bomb(), "FILE_TOO_LARGE"),
    ],
    ids=["corrupt", "truncated", "webp", "pixel_bomb"],
)
async def test_image_job_hostile(
    images_db: JobWorld, store: FakeStorage, case: str, data: bytes, code: str
) -> None:
    image_id, job_id = await checked(images_db, store, data, max_runs=3)
    row = image_db_row(images_db.conn, image_id)
    for report in row["image_report_json"].values():
        assert report["pass"] is False, case
        assert code in [i["code"] for i in report["issues"]], case
    job = job_row(images_db.conn, job_id)
    assert (job["status"], job["attempt"]) == ("succeeded", 1)
    assert row["mime_type"] is None if case != "webp" else True


async def test_image_job_child_timeout_corrupt_not_retried(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_runner, "CHILD_MODULE", "tests.support.image_child_fakes")
    monkeypatch.setattr(image_runner, "CHILD_TIMEOUT_S", 1)
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    image_id = uuid.UUID(f"00000001-0000-4000-8000-{uuid.uuid4().hex[:12]}")  # the fake's slow mode
    image_row(images_db.conn, acme, p, slot="main", image_id=image_id)
    key = image_db_row(images_db.conn, image_id)["storage_key"]
    store.objects[key] = (b"x", "image/jpeg", datetime.now(UTC))
    job_id = await enqueue_image_job(acme, image_id)
    assert await run_image_job(job_id, max_runs=3) == 1
    row = image_db_row(images_db.conn, image_id)
    assert row["compliance_status"] == "failed"
    assert all(r["issues"][0]["code"] == "CORRUPT" for r in row["image_report_json"].values())
    assert job_row(images_db.conn, job_id)["status"] == "succeeded"


async def test_image_job_storage_error_retries_then_dead_letter(
    images_db: JobWorld, store: FakeStorage
) -> None:
    store.fail = {"get": StorageUnavailable("down")}
    image_id, job_id = await checked(images_db, store, picture(10, 10), max_runs=5)
    job = job_row(images_db.conn, job_id)
    assert store.calls.count("get") == 3
    assert (job["status"], job["error_code"]) == ("dead_letter", "RETRY_EXHAUSTED")
    row = image_db_row(images_db.conn, image_id)
    assert (row["compliance_status"], row["image_report_json"]) == ("pending", None)


async def test_image_job_object_missing_failed(images_db: JobWorld, store: FakeStorage) -> None:
    acme = images_db.tenant("acme")
    image_id = image_row(images_db.conn, acme, product(images_db.conn, acme))
    job_id = await enqueue_image_job(acme, image_id)
    await run_image_job(job_id, max_runs=3)
    job = job_row(images_db.conn, job_id)
    assert (job["status"], job["attempt"]) == ("failed", 1)
    assert image_db_row(images_db.conn, image_id)["compliance_status"] == "pending"


async def test_runner_rejects_foreign_key_prefix(images_db: JobWorld, store: FakeStorage) -> None:
    """A row of tenant A whose key sits under tenant B's prefix (raw migrator insert: the API never
    mints one) fails the job before any read."""
    from app.services.image_keys import image_object_key

    acme, bharat = images_db.tenant("acme"), images_db.tenant("bharat")
    p = product(images_db.conn, acme)
    image_id = uuid.uuid4()
    foreign = image_object_key(bharat, p, image_id)
    image_row(images_db.conn, acme, p, image_id=image_id, key=foreign)
    store.objects[foreign] = (
        picture(1000, 1000),
        "image/jpeg",
        datetime.now(UTC),
    )
    job_id = await enqueue_image_job(acme, image_id)
    await run_image_job(job_id, max_runs=3)
    job = job_row(images_db.conn, job_id)
    assert job["status"] == "failed"
    assert job["error_report_json"]["errors"][0]["message"] == "image key outside the tenant prefix"
    assert "get" not in store.calls


async def test_image_job_rerun_identical_except_processed_at(
    images_db: JobWorld, store: FakeStorage
) -> None:
    image_id, _ = await checked(images_db, store, picture(800, 800))
    first = image_db_row(images_db.conn, image_id)
    job2 = await enqueue_image_job(images_db.tenant("acme"), image_id)
    await run_image_job(job2)
    second = image_db_row(images_db.conn, image_id)

    def strip(row: dict[str, Any]) -> dict[str, Any]:
        return {
            m: {k: v for k, v in r.items() if k != "processed_at"}
            for m, r in row["image_report_json"].items()
        }

    assert strip(first) == strip(second)
    for col in (
        "compliance_status",
        "content_hash",
        "mime_type",
        "width_px",
        "height_px",
        "storage_key",
        "slot",
    ):
        assert first[col] == second[col]


async def test_image_job_deleted_image_skipped(images_db: JobWorld, store: FakeStorage) -> None:
    acme = images_db.tenant("acme")
    image_id = image_row(images_db.conn, acme, product(images_db.conn, acme))
    images_db.conn.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [image_id])
    job_id = await enqueue_image_job(acme, image_id)
    await run_image_job(job_id)
    job = job_row(images_db.conn, job_id)
    assert job["status"] == "succeeded" and job["result_json"]["skipped"] == "image_gone"
    assert store.calls == []


async def test_image_job_misconfigured_rule_failed_not_corrupt(
    images_db: JobWorld, store: FakeStorage
) -> None:
    # rules rows are immutable: a version-2 row of the same scope wins and lacks its rgb
    images_db.conn.execute(
        "INSERT INTO rules (id, tenant_id, marketplace, category, field, rule_type, subject, pack, "
        "severity, version, enabled, rule_def) SELECT gen_random_uuid(), tenant_id, marketplace, "
        "category, field, rule_type, subject, pack, severity, 2, true, "
        "jsonb_set(rule_def, '{params}', '{\"min_px\": 5}') FROM rules "
        "WHERE subject = 'main:background' AND marketplace = 'amazon'"
    )
    image_id, job_id = await checked(images_db, store, picture(1000, 1000), max_runs=3)
    job = job_row(images_db.conn, job_id)
    assert (job["status"], job["attempt"]) == ("failed", 1)
    assert job["error_report_json"]["errors"][0]["message"] == "image rule misconfigured"
    assert image_db_row(images_db.conn, image_id)["image_report_json"] is None


async def test_rules_seed_images_load_rules(images_db: JobWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.rules_loader import load_rules
    from mkos_schemas.enums import Marketplace

    acme = images_db.tenant("acme")
    async with tenant_uow(acme) as uow:
        for market, n in ((Marketplace.AMAZON, 6), (Marketplace.FLIPKART, 7)):
            loaded = await load_rules(uow, tenant_id=acme, marketplace=market, category="kurtas")
            assert len([r for r in loaded.ruleset.rules if r.field == "images"]) == n


async def test_image_job_no_transaction_open_during_get_and_check(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def idle_in_tx() -> int:
        return count(
            images_db.conn,
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_rw' "
            "AND datname = current_database() AND state LIKE 'idle in transaction%%'",
        )

    real_get = store.get

    async def get(*a: Any, **k: Any) -> bytes:
        seen.append(idle_in_tx())
        return await real_get(*a, **k)

    real_run = image_runner.run_checks_isolated

    async def run(*a: Any, **k: Any) -> CheckOutcome:
        seen.append(idle_in_tx())
        return await real_run(*a, **k)

    monkeypatch.setattr(store, "get", get)
    monkeypatch.setattr(image_runner, "run_checks_isolated", run)
    await checked(images_db, store, picture(500, 500))
    assert seen == [0, 0]


async def test_image_job_writes_no_agent_runs(images_db: JobWorld, store: FakeStorage) -> None:
    await checked(images_db, store, picture(1000, 1000))
    assert count(images_db.conn, "SELECT count(*) FROM agent_runs") == 0


async def test_image_job_without_creator_succeeds(images_db: JobWorld, store: FakeStorage) -> None:
    image_id, job_id = await checked(images_db, store, picture(600, 600))
    assert job_row(images_db.conn, job_id)["created_by"] is None
    assert job_row(images_db.conn, job_id)["status"] == "succeeded"
    assert image_db_row(images_db.conn, image_id)["image_report_json"] is not None


async def test_image_job_content_hash_matches_object(
    images_db: JobWorld, store: FakeStorage
) -> None:
    data = picture(700, 700, fmt="PNG")
    image_id, _ = await checked(images_db, store, data)
    key = image_db_row(images_db.conn, image_id)["storage_key"]
    assert (
        image_db_row(images_db.conn, image_id)["content_hash"]
        == hashlib.sha256(store.objects[key][0]).hexdigest()
    )
