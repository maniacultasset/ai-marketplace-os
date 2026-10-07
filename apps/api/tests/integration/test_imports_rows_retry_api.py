"""S13 B14 (AC 19; PD10, PD13; DATABASE.md:1481): `POST /api/v1/imports/{import_id}/rows/retry`,
as app_rw: a new `bulk_import` job that re-runs only the failed rows from their stored cells,
parented to the import's newest job, under the tenant import lock and the live-import cap."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import httpx
import pytest
from app.adapters.storage import StorageUnavailable
from app.services.imports import runner
from app.workers.errors import RetryLater
from mkos_schemas.errors import ErrorReport
from mkos_schemas.jobs import JobAccepted

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .imports_support import (
    FIXTURES,
    UNMAPPED,
    csv_bytes,
    fixture_bytes,
    headers,
    import_fixture,
    import_row,
    imports_db,
    one,
    row,
    row_results,
    run_job,
)
from .jobs_support import JobWorld, as_member, bad_token, count, insert_job, job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]
FIXTURE = "products_50_5_bad.csv"


def url(import_id: uuid.UUID | str) -> str:
    return f"{PREFIX}/imports/{import_id}/rows/retry"


async def partial(
    jw: JobWorld, api: httpx.AsyncClient, slug: str = "acme"
) -> tuple[uuid.UUID, uuid.UUID]:
    import_id, job_id = await import_fixture(
        jw, api, fixture_bytes(FIXTURE), email=f"operator@{slug}.test"
    )
    await run_job(job_id)
    assert job_row(jw.conn, job_id)["status"] == "partial"
    return import_id, job_id


async def retry(
    jw: JobWorld, api: httpx.AsyncClient, import_id: uuid.UUID, email: str = "operator@acme.test",
    key: str | None = None,
) -> httpx.Response:  # fmt: skip
    return await api.post(url(import_id), headers=headers(jw, email, key))


async def load_templates() -> None:
    """`templates load` as the migrator (ALEMBIC_DATABASE_URL is the admin login here)."""
    from app.cli import run_templates_load
    from app.core.config import get_settings

    get_settings.cache_clear()
    assert await run_templates_load(FIXTURES / "templates_missing_category.json") == 0
    get_settings.cache_clear()


# ---- the case matrix ----


@pytest.mark.endpoint("imports_rows_retry", case="success")
@pytest.mark.parametrize("role", ["owner", "operator"])
async def test_imports_rows_retry_success(
    imports_db: JobWorld, api: httpx.AsyncClient, role: str
) -> None:
    import_id, first = await partial(imports_db, api)
    response = await retry(imports_db, api, import_id, f"{role}@acme.test")
    assert response.status_code == 202, response.text
    accepted = JobAccepted.model_validate(response.json())
    assert accepted.poll_url == f"/api/v1/jobs/{accepted.job_id}"
    job = job_row(imports_db.conn, accepted.job_id)
    assert (job["kind"], job["status"], job["parent_job_id"], job["payload_json"]) == (
        "bulk_import", "queued", first, {"retry": True},
    )  # fmt: skip
    imp = import_row(imports_db.conn, import_id)
    assert (imp["job_id"], imp["status"], imp["finished_at"]) == (accepted.job_id, "queued", None)


@pytest.mark.endpoint("imports_rows_retry", case="validation")
@pytest.mark.parametrize("case", ["bad_uuid", "no_key"])
async def test_imports_rows_retry_validation(
    imports_db: JobWorld, api: httpx.AsyncClient, case: str
) -> None:
    if case == "bad_uuid":
        response = await api.post(url("nope"), headers=headers(imports_db))
    else:
        response = await api.post(
            url(uuid.uuid4()), headers=as_member(imports_db, "operator@acme.test")
        )
    assert response.status_code == 422


@pytest.mark.endpoint("imports_rows_retry", case="missing_auth")
async def test_imports_rows_retry_missing_auth(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    response = await api.post(url(uuid.uuid4()), headers={"Idempotency-Key": "k-1"})
    assert response.status_code == 401


@pytest.mark.endpoint("imports_rows_retry", case="invalid_token", variant="expired")
@pytest.mark.endpoint("imports_rows_retry", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("imports_rows_retry", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_imports_rows_retry_invalid_token(
    imports_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    hdrs = {**bad_token(imports_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    assert (await api.post(url(uuid.uuid4()), headers=hdrs)).status_code == 401


@pytest.mark.endpoint("imports_rows_retry", case="wrong_role")
@pytest.mark.parametrize("role", ["reviewer", "admin"])
async def test_imports_rows_retry_wrong_role(
    imports_db: JobWorld, api: httpx.AsyncClient, role: str
) -> None:
    import_id, _ = await partial(imports_db, api)
    response = await retry(imports_db, api, import_id, f"{role}@acme.test")
    assert response.status_code == 403


@pytest.mark.endpoint("imports_rows_retry", case="wrong_tenant")
async def test_imports_rows_retry_wrong_tenant(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    theirs, their_job = await partial(imports_db, api, "bharat")
    response = await retry(imports_db, api, theirs)
    assert response.status_code == 404
    assert import_row(imports_db.conn, theirs)["job_id"] == their_job


@pytest.mark.endpoint("imports_rows_retry", case="not_found")
async def test_imports_rows_retry_not_found(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await retry(imports_db, api, uuid.uuid4())
    assert response.status_code == 404


@pytest.mark.endpoint("imports_rows_retry", case="conflict")
@pytest.mark.parametrize("case", ["live_job", "no_failed_rows", "key_reuse"])
async def test_imports_rows_retry_conflict(
    imports_db: JobWorld, api: httpx.AsyncClient, case: str
) -> None:
    if case == "live_job":
        import_id, _ = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))
        response = await retry(imports_db, api, import_id)
        expected = "INVALID_STATUS_TRANSITION"
    elif case == "no_failed_rows":
        from .imports_support import csv_bytes, row

        import_id, job_id = await import_fixture(imports_db, api, csv_bytes([row(1)]))
        await run_job(job_id)
        response = await retry(imports_db, api, import_id)
        expected = "INVALID_STATUS_TRANSITION"
    else:
        import_id, _ = await partial(imports_db, api)
        other, _ = await partial(imports_db, api)
        key = str(uuid.uuid4())
        assert (await retry(imports_db, api, import_id, key=key)).status_code == 202
        response = await retry(imports_db, api, other, key=key)
        expected = "IDEMPOTENCY_KEY_REUSED"
    assert response.status_code == 409, response.text
    assert response.json()["error"]["type"] == expected


# ---- the named cases ----


async def test_imports_rows_retry_after_template_load(
    imports_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_id, _ = await partial(imports_db, api)
    await load_templates()
    accepted = JobAccepted.model_validate((await retry(imports_db, api, import_id)).json())
    await run_job(accepted.job_id)
    assert job_row(imports_db.conn, accepted.job_id)["status"] == "partial"
    imp = import_row(imports_db.conn, import_id)
    assert (imp["status"], imp["total_rows"], imp["imported_rows"], imp["failed_rows"]) == (
        "partial", 50, 46, 4,
    )  # fmt: skip
    assert (
        one(
            imports_db.conn,
            "SELECT status FROM import_rows WHERE import_id = %s AND row_number = 43",
            [import_id],
        )
        == "imported"
    )


async def test_imports_rows_retry_only_failed_rows_rerun(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id, _ = await partial(imports_db, api)
    tenant = imports_db.tenant("acme")
    before = imports_db.conn.execute(
        "SELECT sku, updated_at FROM products WHERE tenant_id = %s ORDER BY sku", [tenant]
    ).fetchall()
    rows_before = imports_db.conn.execute(
        "SELECT row_number, job_id FROM import_rows WHERE import_id = %s AND status = 'imported'",
        [import_id],
    ).fetchall()
    accepted = JobAccepted.model_validate((await retry(imports_db, api, import_id)).json())
    ran: list[int] = []
    real = runner._run_batch

    async def spy(run: Any, batch: list[runner.Unit]) -> None:
        ran.extend(n for unit in batch for n in unit.row_numbers)
        await real(run, batch)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner, "_run_batch", spy)
        await run_job(accepted.job_id)
    assert sorted(ran) == [8, 15, 22, 35, 43]
    after = imports_db.conn.execute(
        "SELECT sku, updated_at FROM products WHERE tenant_id = %s ORDER BY sku", [tenant]
    ).fetchall()
    assert after == before
    rows_after = imports_db.conn.execute(
        "SELECT row_number, job_id FROM import_rows WHERE import_id = %s AND status = 'imported'",
        [import_id],
    ).fetchall()
    assert rows_after == rows_before


async def test_imports_rows_retry_parent_job_id_set(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id, first = await partial(imports_db, api)
    accepted = JobAccepted.model_validate((await retry(imports_db, api, import_id)).json())
    assert job_row(imports_db.conn, accepted.job_id)["parent_job_id"] == first
    assert job_row(imports_db.conn, first)["status"] == "partial"  # the old row is untouched


async def test_imports_rows_retry_second_retry_parents_latest(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id, first = await partial(imports_db, api)
    second = JobAccepted.model_validate((await retry(imports_db, api, import_id)).json()).job_id
    await run_job(second)
    third = JobAccepted.model_validate((await retry(imports_db, api, import_id)).json()).job_id
    assert job_row(imports_db.conn, second)["parent_job_id"] == first
    assert job_row(imports_db.conn, third)["parent_job_id"] == second


async def test_imports_rows_retry_two_concurrent_one_job(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id, _ = await partial(imports_db, api)
    before = count(imports_db.conn, "SELECT count(*) FROM job_runs")
    results = await asyncio.gather(
        retry(imports_db, api, import_id), retry(imports_db, api, import_id)
    )
    assert sorted(r.status_code for r in results) == [202, 409]
    assert count(imports_db.conn, "SELECT count(*) FROM job_runs") == before + 1


async def test_rows_retry_after_staff_requeue(
    imports_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import cli

    import_id, job_id = await import_fixture(imports_db, api, fixture_bytes(FIXTURE))

    class Down:
        async def get(self, bucket: str, key: str) -> bytes:
            raise StorageUnavailable("down")

    monkeypatch.setattr(runner, "get_storage", lambda: Down())
    for _ in range(3):
        with contextlib.suppress(RetryLater):
            await run_job(job_id)
    old = job_row(imports_db.conn, job_id)
    assert (old["status"], old["error_code"]) == ("dead_letter", "RETRY_EXHAUSTED")
    assert import_row(imports_db.conn, import_id)["status"] == "failed"
    monkeypatch.undo()
    assert await cli.run_requeue(job_id, "staff@platform.test", "storage was down") == 0
    requeued = one(imports_db.conn, "SELECT id FROM job_runs WHERE parent_job_id = %s", [job_id])
    imp = import_row(imports_db.conn, import_id)
    assert (imp["job_id"], imp["status"]) == (requeued, "queued")
    await run_job(requeued)
    assert import_row(imports_db.conn, import_id)["status"] == "partial"
    response = await retry(imports_db, api, import_id)
    assert response.status_code == 202, response.text
    assert (
        job_row(imports_db.conn, uuid.UUID(response.json()["job_id"]))["parent_job_id"] == requeued
    )


async def test_imports_rows_retry_cap_429(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    import_id, _ = await partial(imports_db, api)
    for _ in range(2):
        insert_job(imports_db.conn, imports_db.tenant("acme"))
    response = await retry(imports_db, api, import_id)
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "RATE_LIMITED"
    assert import_row(imports_db.conn, import_id)["status"] == "partial"


# ---- a rows retry of a partly imported family (round-1 F3) ----


def row_codes(conn: Any, import_id: uuid.UUID) -> dict[int, tuple[str, list[str]]]:
    out: dict[int, tuple[str, list[str]]] = {}
    for number, status, _sku, _pid, report in row_results(conn, import_id):
        codes = (
            [e.error_code.value for e in ErrorReport.model_validate(report).errors]
            if status == "failed"
            else []
        )
        out[number] = (status, codes)
    return out


def family_state(conn: Any, tenant: uuid.UUID) -> list[tuple[Any, ...]]:
    found: list[tuple[Any, ...]] = conn.execute(
        "SELECT sku, title, category, variation_theme, size_run, parent_product_id, updated_at "
        "FROM products WHERE tenant_id = %s AND (sku = 'S' OR parent_product_id IS NOT NULL) "
        "ORDER BY sku",
        [tenant],
    ).fetchall()
    return found


def imported_rows(conn: Any, import_id: uuid.UUID) -> list[tuple[Any, ...]]:
    found: list[tuple[Any, ...]] = conn.execute(
        "SELECT row_number, job_id, product_id FROM import_rows "
        "WHERE import_id = %s AND status = 'imported' AND sku IN ('S', 'C1', 'C2') "
        "ORDER BY row_number",
        [import_id],
    ).fetchall()
    return found


async def retry_and_run(jw: JobWorld, api: httpx.AsyncClient, import_id: uuid.UUID) -> str:
    accepted = JobAccepted.model_validate((await retry(jw, api, import_id)).json())
    await run_job(accepted.job_id)
    status: str = job_row(jw.conn, accepted.job_id)["status"]
    return status


async def test_imports_rows_retry_family_child_keeps_its_mismatch(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """A failed child of a partly imported family is checked against the imported family on a
    rows retry: it never becomes the style's lead, never rewrites the style, and keeps its
    CROSS_FIELD_MISMATCH (invariant 10)."""
    tenant = imports_db.tenant("acme")
    data = csv_bytes(
        [
            row(1, seller_sku="S", product_name="Kurta"),
            row(2, seller_sku="C1", product_name="Kurta", parent_style_id="S", size_name="M"),
            row(3, seller_sku="C2", product_name="Kurta X", parent_style_id="S", size_name="L"),
            row(4, category=UNMAPPED, fabric_type="Silk", color_name="Red"),
        ]
    )
    import_id, job_id = await import_fixture(imports_db, api, data)
    await run_job(job_id)
    first = row_codes(imports_db.conn, import_id)
    assert first == {
        2: ("imported", []),
        3: ("imported", []),
        4: ("failed", ["CROSS_FIELD_MISMATCH"]),
        5: ("failed", ["CATEGORY_NOT_MAPPED"]),
    }
    before = family_state(imports_db.conn, tenant)
    rows_before = imported_rows(imports_db.conn, import_id)
    await load_templates()
    assert await retry_and_run(imports_db, api, import_id) == "partial"
    assert row_codes(imports_db.conn, import_id) == {
        **first,
        5: ("imported", []),
    }
    assert family_state(imports_db.conn, tenant) == before
    assert before[-1][:5] == ("S", "Kurta", "ethnic_wear_kurta_sets", "size", ["m"])
    assert imported_rows(imports_db.conn, import_id) == rows_before


async def test_imports_rows_retry_failed_style_row_never_flattens_its_family(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """A style row that failed while its children were imported is retried inside its family,
    never as a standalone product: the style keeps its theme, size_run and category, and a
    style row that disagrees with the imported family fails CROSS_FIELD_MISMATCH."""
    tenant = imports_db.tenant("acme")
    data = csv_bytes(
        [
            row(1, seller_sku="S", product_name="Kurta", category=UNMAPPED),
            row(2, seller_sku="C1", product_name="Kurta", parent_style_id="S", size_name="M"),
            row(3, seller_sku="C2", product_name="Kurta", parent_style_id="S", size_name="L"),
        ]
    )
    import_id, job_id = await import_fixture(imports_db, api, data)
    await run_job(job_id)
    assert row_codes(imports_db.conn, import_id) == {
        2: ("failed", ["CATEGORY_NOT_MAPPED"]),
        3: ("imported", []),
        4: ("imported", []),
    }
    before = family_state(imports_db.conn, tenant)
    assert before[-1][:5] == ("S", "Kurta", "ethnic_wear_kurta_sets", "size", ["m", "l"])
    rows_before = imported_rows(imports_db.conn, import_id)
    await load_templates()
    assert await retry_and_run(imports_db, api, import_id) == "partial"
    assert row_codes(imports_db.conn, import_id) == {
        2: ("failed", ["CROSS_FIELD_MISMATCH"]),
        3: ("imported", []),
        4: ("imported", []),
    }
    assert family_state(imports_db.conn, tenant) == before
    assert imported_rows(imports_db.conn, import_id) == rows_before
