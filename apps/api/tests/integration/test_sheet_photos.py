"""G2: photos from spreadsheet links, as app_rw through the real import and job wrapper. The
fetcher is replaced through its module seam (`app.workers.photo_links.set_fetcher_factory`) and
the object store by an in-memory one, so nothing here opens a link.

- The import writes `queued` links and one download job per product, and never fetches.
- The job stores each photo through the upload path: the image row in the right slot, its
  `image_compliance` check job, the `image.uploaded` audit row with the importer as actor, the
  object under the tenant's key; a live Front is never replaced.
- A colour family's links land on the colourway; size siblings sharing a link fetch it once.
- A re-import is deduped, requeues failures, and never downloads a photo the seller deleted.
- The per-import cap, the 50-photo cap, the upload-room deferral and the sweeper, the terminal
  release, a product deleted on the way.
- A failed link never fails the row; the row and the product show each link's result.
- The retries, the dismiss and the switch.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from PIL import Image

from . import factories as f
from .conftest import LOCAL_HOSTS
from .images_support import FakeStorage
from .imports_support import (
    MAPPED,
    csv_bytes,
    headers,
    import_fixture,
    imports_db,
    row,
    run_job,
)
from .jobs_support import JobWorld, as_member, bad_token, count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]
OPERATOR = "operator@acme.test"
LINK = "https://cdn.example.com/p/{n}.jpg"


def family(n: int, **cells: str) -> dict[str, str]:
    """A family row: every member shares the style's title (`_agree`)."""
    return row(n, product_name="Family Kurta", **cells)


def _png(width: int = 48, height: int = 48) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(out, format="PNG")
    return out.getvalue()


@dataclass
class FakeFetcher:
    answers: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def fetch(self, url: str) -> Any:
        from app.adapters.photo_fetch import FetchOk

        self.calls.append(url)
        found = self.answers.get(url)
        if found is not None:
            return found
        return FetchOk(_png(), "image/png", 48, 48)


@dataclass
class Sheet:
    jw: JobWorld
    fetcher: FakeFetcher
    store: FakeStorage

    @property
    def conn(self) -> Any:
        return self.jw.conn

    @property
    def tenant(self) -> uuid.UUID:
        return self.jw.tenant("acme")


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool, **extra: str) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("SHEET_PHOTOS_ENABLED", "true" if on else "false")
    monkeypatch.setenv("PHOTO_VIEWS_ENABLED", "false")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture
async def sheet(imports_db: JobWorld, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Sheet]:
    from app.workers import photo_links as worker

    fake = FakeFetcher()
    store = FakeStorage()
    worker.set_fetcher_factory(lambda: fake)
    monkeypatch.setattr(worker, "get_storage", lambda: store)
    _switch(monkeypatch, on=True)
    yield Sheet(imports_db, fake, store)
    worker.set_fetcher_factory(None)
    from app.core.config import get_settings

    get_settings.cache_clear()


def _product(sheet: Sheet, sku: str) -> uuid.UUID:
    found = sheet.conn.execute("SELECT id FROM products WHERE sku = %s", [sku]).fetchone()
    assert found is not None, sku
    return uuid.UUID(str(found[0]))


def _links(sheet: Sheet, product_id: uuid.UUID) -> list[dict[str, Any]]:
    cur = sheet.conn.execute(
        "SELECT * FROM photo_links WHERE product_id = %s ORDER BY colourway_id NULLS FIRST, "
        "position",
        [product_id],
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def _fetch_jobs(sheet: Sheet, product_id: uuid.UUID) -> list[uuid.UUID]:
    rows = sheet.conn.execute(
        "SELECT id FROM job_runs WHERE entity_type = 'photo_links' AND entity_id = %s "
        "ORDER BY queued_at, id",
        [product_id],
    ).fetchall()
    return [uuid.UUID(str(r[0])) for r in rows]


def _images(sheet: Sheet, product_id: uuid.UUID) -> list[dict[str, Any]]:
    cur = sheet.conn.execute(
        "SELECT * FROM images WHERE product_id = %s AND deleted_at IS NULL ORDER BY created_at",
        [product_id],
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


async def _import(sheet: Sheet, api: httpx.AsyncClient, rows: list[dict[str, str]]) -> uuid.UUID:
    import_id, job_id = await import_fixture(sheet.jw, api, csv_bytes(rows))
    await run_job(job_id)
    return import_id


async def _download(sheet: Sheet, product_id: uuid.UUID) -> None:
    for job_id in _fetch_jobs(sheet, product_id):
        status = sheet.conn.execute(
            "SELECT status FROM job_runs WHERE id = %s", [job_id]
        ).fetchone()
        if status is not None and status[0] == "queued":
            await run_job(job_id)


# ---- the import stays offline ----


async def test_the_import_writes_queued_links_and_one_job_per_product(
    sheet: Sheet, api: httpx.AsyncClient
) -> None:
    rows = [
        row(1, additional_images=f"{LINK.format(n='1b')}|{LINK.format(n='1c')}"),
        row(2),
    ]
    await _import(sheet, api, rows)
    assert sheet.fetcher.calls == []  # the import never opens a link
    first, second = _product(sheet, "SKU-00001"), _product(sheet, "SKU-00002")
    links = _links(sheet, first)
    assert [(lk["position"], lk["source_column"], lk["status"]) for lk in links] == [
        (1, "main_image", "queued"),
        (2, "additional_images", "queued"),
        (3, "additional_images", "queued"),
    ]
    assert all(lk["requested_by"] == sheet.jw.world.user(OPERATOR) for lk in links)
    assert len(_links(sheet, second)) == 1
    assert len(_fetch_jobs(sheet, first)) == 1 and len(_fetch_jobs(sheet, second)) == 1
    job = sheet.conn.execute(
        "SELECT kind, payload_json FROM job_runs WHERE id = %s", [_fetch_jobs(sheet, first)[0]]
    ).fetchone()
    assert job is not None and job[0] == "image_compliance"
    assert job[1]["photo_links"] == str(first)


async def test_switch_off_writes_nothing(
    sheet: Sheet, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=False)
    await _import(sheet, api, [row(1)])
    product = _product(sheet, "SKU-00001")
    assert _links(sheet, product) == []
    assert _fetch_jobs(sheet, product) == []
    stored = sheet.conn.execute(
        "SELECT source_image_urls FROM products WHERE id = %s", [product]
    ).fetchone()
    assert stored is not None and stored[0] == [LINK.format(n=1)]  # kept as before


# ---- the job stores through the upload path ----


async def test_the_job_stores_each_photo_as_an_upload(sheet: Sheet, api: httpx.AsyncClient) -> None:
    await _import(sheet, api, [row(1, additional_images=LINK.format(n="1b"))])
    product = _product(sheet, "SKU-00001")
    await _download(sheet, product)
    assert sheet.fetcher.calls == [LINK.format(n=1), LINK.format(n="1b")]
    images = _images(sheet, product)
    assert [(i["slot"], i["role"], i["origin"]) for i in images] == [
        ("main", None, "seller"),
        ("secondary", None, "seller"),
    ]
    assert all(i["source_url"] is None for i in images)  # a link never reaches the image row
    for image in images:
        assert str(image["storage_key"]).startswith(f"{sheet.tenant}/")
        assert image["storage_key"] in sheet.store.objects
        checks = count(
            sheet.conn,
            "SELECT count(*) FROM job_runs WHERE entity_type = 'image' AND entity_id = %s "
            "AND kind = 'image_compliance'",
            [image["id"]],
        )
        assert checks == 1
        audit = sheet.conn.execute(
            "SELECT actor_type, actor_user_id FROM audit_log WHERE action = 'image.uploaded' "
            "AND entity_id = %s",
            [image["id"]],
        ).fetchone()
        assert audit == ("user", sheet.jw.world.user(OPERATOR))
    links = _links(sheet, product)
    assert [lk["status"] for lk in links] == ["saved", "saved"]
    assert {lk["image_id"] for lk in links} == {i["id"] for i in images}


async def test_a_live_front_is_kept_and_the_link_becomes_an_extra(
    sheet: Sheet, api: httpx.AsyncClient
) -> None:
    await _import(sheet, api, [row(1)])
    product = _product(sheet, "SKU-00001")
    seller_main = f.image(sheet.conn, sheet.tenant, product, slot="main")
    await _download(sheet, product)
    images = _images(sheet, product)
    assert [i["id"] for i in images if i["slot"] == "main"] == [seller_main]
    assert [i["slot"] for i in images if i["id"] != seller_main] == ["secondary"]
    detail = await api.get(f"/api/v1/products/{product}", headers=as_member(sheet.jw, OPERATOR))
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["photo_links_pending"] is False
    assert [(p["status"], p["view"]) for p in body["photo_links"]] == [("saved", "extra")]


async def test_a_colour_family_lands_on_its_colourways_and_shares_a_link(
    sheet: Sheet, api: httpx.AsyncClient
) -> None:
    red, blue = LINK.format(n="red"), LINK.format(n="blue")
    rows = [
        family(10, seller_sku="STYLE-1", main_image=""),
        family(11, parent_style_id="STYLE-1", color_name="Red", size_name="S", main_image=red),
        family(12, parent_style_id="STYLE-1", color_name="Red", size_name="M", main_image=red),
        family(13, parent_style_id="STYLE-1", color_name="Blue", size_name="S", main_image=blue),
    ]
    await _import(sheet, api, rows)
    style = _product(sheet, "STYLE-1")
    links = _links(sheet, style)
    assert len(links) == 2  # S and M of Red share one link
    assert all(lk["colourway_id"] is not None for lk in links)
    assert len({lk["colourway_id"] for lk in links}) == 2
    owner_rows = {lk["row_number"] for lk in links}
    assert len(owner_rows) == 2  # the first row that named each link owns it
    await _download(sheet, style)
    assert sorted(sheet.fetcher.calls) == sorted([red, blue])
    mains = [i for i in _images(sheet, style) if i["slot"] == "main"]
    assert len(mains) == 2 and all(i["colourway_id"] is not None for i in mains)


async def test_a_size_family_lands_on_the_style(sheet: Sheet, api: httpx.AsyncClient) -> None:
    shared = LINK.format(n="shared")
    rows = [
        family(20, seller_sku="STYLE-2", main_image=""),
        family(21, parent_style_id="STYLE-2", size_name="S", main_image=shared),
        family(22, parent_style_id="STYLE-2", size_name="M", main_image=shared),
    ]
    await _import(sheet, api, rows)
    links = _links(sheet, _product(sheet, "STYLE-2"))
    assert len(links) == 1 and links[0]["colourway_id"] is None


# ---- re-imports, caps and failures ----


async def test_a_reimport_dedupes_requeues_failures_and_respects_a_deletion(
    sheet: Sheet, api: httpx.AsyncClient
) -> None:
    from app.adapters.photo_fetch import FetchFailure

    bad = LINK.format(n="1b")
    sheet.fetcher.answers[bad] = FetchFailure("not_found", 404)
    rows = [row(1, additional_images=bad)]
    await _import(sheet, api, rows)
    product = _product(sheet, "SKU-00001")
    await _download(sheet, product)
    links = _links(sheet, product)
    assert [lk["status"] for lk in links] == ["saved", "failed"]
    # the seller deletes the saved photo, then imports the same file again
    saved_image = links[0]["image_id"]
    sheet.conn.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [saved_image])
    sheet.fetcher.answers.pop(bad)
    sheet.fetcher.calls.clear()
    await _import(sheet, api, rows)
    again = _links(sheet, product)
    assert len(again) == 2  # deduped
    assert [(lk["status"], lk["problem"]) for lk in again] == [
        ("skipped", "removed_by_seller"),
        ("queued", None),
    ]
    await _download(sheet, product)
    assert sheet.fetcher.calls == [bad]  # the deleted photo is never downloaded again


async def test_a_failed_link_never_fails_the_row_and_the_row_shows_it(
    sheet: Sheet, api: httpx.AsyncClient
) -> None:
    from app.adapters.photo_fetch import FetchFailure

    bad = LINK.format(n="2b")
    sheet.fetcher.answers[bad] = FetchFailure("not_found", 404)
    import_id = await _import(sheet, api, [row(2, additional_images=bad)])
    await _download(sheet, _product(sheet, "SKU-00002"))
    hdrs = as_member(sheet.jw, OPERATOR)
    rows = await api.get(f"/api/v1/imports/{import_id}/rows", headers=hdrs)
    assert rows.status_code == 200, rows.text
    (only,) = rows.json()["data"]
    assert only["status"] == "imported"
    photos = only["photos"]
    assert [(p["position"], p["status"]) for p in photos] == [(1, "saved"), (2, "failed")]
    failed = photos[1]
    assert failed["problem"] == "not_found" and failed["http_status"] == 404
    assert failed["message"] == "The link returned 404. Check the link is right and public."
    assert failed["retryable"] is True
    assert "cdn.example.com" not in rows.text  # the link itself is never returned
    report = await api.get(f"/api/v1/imports/{import_id}", headers=hdrs)
    assert report.json()["photos"] == {"total": 2, "waiting": 0, "saved": 1, "failed": 1}


async def test_the_per_import_cap_skips_the_rest(
    sheet: Sheet, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=True, SHEET_PHOTOS_MAX_PER_IMPORT="2")
    links = "|".join(LINK.format(n=f"3{c}") for c in "bc")
    await _import(sheet, api, [row(3, additional_images=links)])
    product = _product(sheet, "SKU-00003")
    assert [(lk["status"], lk["problem"]) for lk in _links(sheet, product)] == [
        ("queued", None),
        ("queued", None),
        ("skipped", "import_cap"),
    ]


async def test_the_photo_cap_fails_the_link(sheet: Sheet, api: httpx.AsyncClient) -> None:
    await _import(sheet, api, [row(4)])
    product = _product(sheet, "SKU-00004")
    for _ in range(50):
        f.image(sheet.conn, sheet.tenant, product)
    await _download(sheet, product)
    (link,) = _links(sheet, product)
    assert (link["status"], link["problem"]) == ("failed", "photo_limit")
    assert sheet.store.objects == {}  # the downloaded object was removed again


# ---- deferral, sweeper, release, delete ----


async def test_no_upload_room_defers_and_the_sweeper_queues_again(
    sheet: Sheet, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.jobs import EnqueueSpec
    from app.repositories.uow import tenant_uow
    from app.workers import photo_links as worker
    from app.workers.periodic import schedule_photo_links
    from mkos_schemas.enums import JobKind

    await _import(sheet, api, [row(5)])
    product = _product(sheet, "SKU-00005")
    async with tenant_uow(sheet.tenant) as uow:  # the seller's own uploads fill the room
        for _ in range(16):
            await uow.job_runs.enqueue(
                EnqueueSpec(
                    kind=JobKind.IMAGE_COMPLIANCE,
                    entity_type="image",
                    entity_id=uuid.uuid4(),
                    created_by=None,
                    defer=False,
                )
            )
    monkeypatch.setattr(worker, "DEADLINE_S", 1.0)
    monkeypatch.setattr(worker, "LINK_BUDGET_S", 0.5)
    monkeypatch.setattr(worker, "ROOM_POLL_S", 0.01)
    (job,) = _fetch_jobs(sheet, product)
    await run_job(job)
    assert sheet.fetcher.calls == []
    assert [lk["status"] for lk in _links(sheet, product)] == ["queued"]
    status = sheet.conn.execute("SELECT status FROM job_runs WHERE id = %s", [job]).fetchone()
    assert status is not None and status[0] == "partial"
    assert await schedule_photo_links() >= 1
    assert len(_fetch_jobs(sheet, product)) == 2


async def test_the_terminal_hook_releases_claimed_links(
    sheet: Sheet, api: httpx.AsyncClient
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.image_candidate_hooks import on_image_terminal

    await _import(sheet, api, [row(6)])
    product = _product(sheet, "SKU-00006")
    (job,) = _fetch_jobs(sheet, product)
    async with tenant_uow(sheet.tenant) as uow:
        claimed = await uow.photo_links.claim(product, job)
        assert len(claimed) == 1
    async with tenant_uow(sheet.tenant) as uow:
        stale = type("Job", (), {"status": "failed", "entity_type": "photo_links", "id": job,
                                 "tenant_id": sheet.tenant, "payload_json": {}})()  # fmt: skip
        await on_image_terminal(uow.session, stale, None)
        other = type("Job", (), {"status": "failed", "entity_type": "photo_links",
                                 "id": uuid.uuid4(), "tenant_id": sheet.tenant,
                                 "payload_json": {}})()  # fmt: skip
        await on_image_terminal(uow.session, other, None)  # fenced: another job's claim
    assert [lk["status"] for lk in _links(sheet, product)] == ["queued"]


async def test_a_deleted_product_skips_its_links(sheet: Sheet, api: httpx.AsyncClient) -> None:
    await _import(sheet, api, [row(7)])
    product = _product(sheet, "SKU-00007")
    gone = await api.delete(
        f"/api/v1/products/{product}", headers=as_member(sheet.jw, "owner@acme.test")
    )
    assert gone.status_code == 204, gone.text
    assert [(lk["status"], lk["problem"]) for lk in _links(sheet, product)] == [
        ("skipped", "product_gone")
    ]
    for job in _fetch_jobs(sheet, product):
        await run_job(job)
    assert sheet.fetcher.calls == []


async def test_the_capability_flag(sheet: Sheet, api: httpx.AsyncClient) -> None:
    me = await api.get("/api/v1/tenants/me", headers=as_member(sheet.jw, OPERATOR))
    assert me.status_code == 200
    assert me.json()["capabilities"]["sheet_photos_enabled"] is True


# ---- POST /products/{product_id}/photo-links/retry ----


def _failed_link(sheet: Sheet, slug: str = "acme", **columns: Any) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = sheet.jw.tenant(slug)
    product = f.product(sheet.conn, tenant, category=MAPPED[0])
    values: dict[str, Any] = {
        "status": "failed",
        "problem": "not_found",
        "http_status": 404,
        "requested_by": sheet.jw.world.user(f"operator@{slug}.test"),
        **columns,
    }
    return product, f.photo_link(sheet.conn, tenant, product, **values)


def _status(sheet: Sheet, link: uuid.UUID) -> str:
    found = sheet.conn.execute("SELECT status FROM photo_links WHERE id = %s", [link]).fetchone()
    assert found is not None
    return str(found[0])


def _retry(
    api: httpx.AsyncClient, sheet: Sheet, product: Any, *, email: str = OPERATOR, key: str = ""
) -> Any:
    return api.post(
        f"/api/v1/products/{product}/photo-links/retry",
        headers=headers(sheet.jw, email, key or None),
    )


@pytest.mark.endpoint("products_photo_links_retry", case="success")
async def test_product_retry_requeues_and_replays(sheet: Sheet, api: httpx.AsyncClient) -> None:
    product, link = _failed_link(sheet)
    key = str(uuid.uuid4())
    first = await _retry(api, sheet, product, key=key)
    assert first.status_code == 202, first.text
    assert _status(sheet, link) == "queued"
    assert first.json()["poll_url"] == f"/api/v1/jobs/{first.json()['job_id']}"
    assert _fetch_jobs(sheet, product) == [uuid.UUID(first.json()["job_id"])]
    replay = await _retry(api, sheet, product, key=key)
    assert replay.status_code == 202 and replay.json() == first.json()
    await run_job(uuid.UUID(first.json()["job_id"]))
    assert _status(sheet, link) == "saved"


async def test_product_retry_is_501_while_off(
    sheet: Sheet, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    product, link = _failed_link(sheet)
    _switch(monkeypatch, on=False)
    response = await _retry(api, sheet, product)
    assert response.status_code == 501, response.text
    assert response.json()["error"]["type"] == "NOT_IMPLEMENTED"
    assert _status(sheet, link) == "failed"


@pytest.mark.endpoint("products_photo_links_retry", case="conflict")
async def test_product_retry_with_nothing_to_retry_is_409(
    sheet: Sheet, api: httpx.AsyncClient
) -> None:
    product, _link = _failed_link(sheet, status="queued", problem=None, http_status=None)
    response = await _retry(api, sheet, product)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"


@pytest.mark.endpoint("products_photo_links_retry", case="validation")
async def test_product_retry_bad_id_422(sheet: Sheet, api: httpx.AsyncClient) -> None:
    assert (await _retry(api, sheet, "not-a-uuid")).status_code == 422


@pytest.mark.endpoint("products_photo_links_retry", case="not_found")
async def test_product_retry_unknown_404(sheet: Sheet, api: httpx.AsyncClient) -> None:
    assert (await _retry(api, sheet, uuid.uuid4())).status_code == 404


@pytest.mark.endpoint("products_photo_links_retry", case="wrong_tenant")
async def test_product_retry_other_tenant_404(sheet: Sheet, api: httpx.AsyncClient) -> None:
    product, link = _failed_link(sheet, "bharat")
    assert (await _retry(api, sheet, product)).status_code == 404
    assert _status(sheet, link) == "failed"


@pytest.mark.endpoint("products_photo_links_retry", case="wrong_role")
async def test_product_retry_reviewer_403(sheet: Sheet, api: httpx.AsyncClient) -> None:
    product, link = _failed_link(sheet)
    response = await _retry(api, sheet, product, email="reviewer@acme.test")
    assert response.status_code == 403
    assert _status(sheet, link) == "failed"


@pytest.mark.endpoint("products_photo_links_retry", case="missing_auth")
async def test_product_retry_missing_auth_401(sheet: Sheet, api: httpx.AsyncClient) -> None:
    response = await api.post(
        f"/api/v1/products/{uuid.uuid4()}/photo-links/retry",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 401


@pytest.mark.endpoint("products_photo_links_retry", case="invalid_token", variant="expired")
@pytest.mark.endpoint("products_photo_links_retry", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("products_photo_links_retry", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_product_retry_invalid_token_401(
    sheet: Sheet, api: httpx.AsyncClient, variant: str
) -> None:
    hdrs = {**bad_token(sheet.jw, variant), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"/api/v1/products/{uuid.uuid4()}/photo-links/retry", headers=hdrs)
    assert response.status_code == 401


# ---- POST /imports/{import_id}/photo-links/retry ----


def _import_retry(
    api: httpx.AsyncClient, sheet: Sheet, import_id: Any, *, email: str = OPERATOR, key: str = ""
) -> Any:
    return api.post(
        f"/api/v1/imports/{import_id}/photo-links/retry",
        headers=headers(sheet.jw, email, key or None),
    )


async def _failed_import(sheet: Sheet, api: httpx.AsyncClient) -> uuid.UUID:
    from app.adapters.photo_fetch import FetchFailure

    for n in (8, 9):
        sheet.fetcher.answers[LINK.format(n=n)] = FetchFailure("timeout")
    import_id = await _import(sheet, api, [row(8), row(9)])
    for sku in ("SKU-00008", "SKU-00009"):
        await _download(sheet, _product(sheet, sku))
    sheet.fetcher.answers.clear()
    return import_id


@pytest.mark.endpoint("imports_photo_links_retry", case="success")
async def test_import_retry_requeues_every_failed_link(
    sheet: Sheet, api: httpx.AsyncClient
) -> None:
    import_id = await _failed_import(sheet, api)
    key = str(uuid.uuid4())
    response = await _import_retry(api, sheet, import_id, key=key)
    assert response.status_code == 202, response.text
    assert response.json() == {"queued": 2, "products": 2}
    replay = await _import_retry(api, sheet, import_id, key=key)
    assert replay.status_code == 202 and replay.json() == response.json()
    for sku in ("SKU-00008", "SKU-00009"):
        product = _product(sheet, sku)
        assert [lk["status"] for lk in _links(sheet, product)] == ["queued"]
        assert len(_fetch_jobs(sheet, product)) == 2


@pytest.mark.endpoint("imports_photo_links_retry", case="conflict")
async def test_import_retry_with_nothing_to_retry_is_409(
    sheet: Sheet, api: httpx.AsyncClient
) -> None:
    import_id = await _import(sheet, api, [row(1)])
    response = await _import_retry(api, sheet, import_id)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"


@pytest.mark.endpoint("imports_photo_links_retry", case="validation")
async def test_import_retry_bad_id_422(sheet: Sheet, api: httpx.AsyncClient) -> None:
    assert (await _import_retry(api, sheet, "not-a-uuid")).status_code == 422


@pytest.mark.endpoint("imports_photo_links_retry", case="not_found")
async def test_import_retry_unknown_404(sheet: Sheet, api: httpx.AsyncClient) -> None:
    assert (await _import_retry(api, sheet, uuid.uuid4())).status_code == 404


@pytest.mark.endpoint("imports_photo_links_retry", case="wrong_tenant")
async def test_import_retry_other_tenant_404(sheet: Sheet, api: httpx.AsyncClient) -> None:
    import_id = await _failed_import(sheet, api)
    response = await _import_retry(api, sheet, import_id, email="operator@bharat.test")
    assert response.status_code == 404
    assert [lk["status"] for lk in _links(sheet, _product(sheet, "SKU-00008"))] == ["failed"]


@pytest.mark.endpoint("imports_photo_links_retry", case="wrong_role")
async def test_import_retry_reviewer_403(sheet: Sheet, api: httpx.AsyncClient) -> None:
    response = await _import_retry(api, sheet, uuid.uuid4(), email="reviewer@acme.test")
    assert response.status_code == 403


@pytest.mark.endpoint("imports_photo_links_retry", case="missing_auth")
async def test_import_retry_missing_auth_401(sheet: Sheet, api: httpx.AsyncClient) -> None:
    response = await api.post(
        f"/api/v1/imports/{uuid.uuid4()}/photo-links/retry",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 401


@pytest.mark.endpoint("imports_photo_links_retry", case="invalid_token", variant="expired")
@pytest.mark.endpoint("imports_photo_links_retry", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("imports_photo_links_retry", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_import_retry_invalid_token_401(
    sheet: Sheet, api: httpx.AsyncClient, variant: str
) -> None:
    hdrs = {**bad_token(sheet.jw, variant), "Idempotency-Key": str(uuid.uuid4())}
    response = await api.post(f"/api/v1/imports/{uuid.uuid4()}/photo-links/retry", headers=hdrs)
    assert response.status_code == 401


# ---- DELETE /photo-links/{link_id} ----


def _dismiss(api: httpx.AsyncClient, sheet: Sheet, link: Any, email: str = OPERATOR) -> Any:
    return api.delete(f"/api/v1/photo-links/{link}", headers=as_member(sheet.jw, email))


@pytest.mark.endpoint("photo_links_dismiss", case="success")
async def test_dismiss_hides_a_failed_link(sheet: Sheet, api: httpx.AsyncClient) -> None:
    product, link = _failed_link(sheet)
    response = await _dismiss(api, sheet, link)
    assert response.status_code == 204, response.text
    assert _status(sheet, link) == "dismissed"
    detail = await api.get(f"/api/v1/products/{product}", headers=as_member(sheet.jw, OPERATOR))
    assert detail.json()["photo_links"] == []


@pytest.mark.endpoint("photo_links_dismiss", case="conflict")
async def test_dismiss_a_waiting_link_is_409(sheet: Sheet, api: httpx.AsyncClient) -> None:
    _product_id, link = _failed_link(sheet, status="queued", problem=None, http_status=None)
    response = await _dismiss(api, sheet, link)
    assert response.status_code == 409
    assert _status(sheet, link) == "queued"


@pytest.mark.endpoint("photo_links_dismiss", case="validation")
async def test_dismiss_bad_id_422(sheet: Sheet, api: httpx.AsyncClient) -> None:
    assert (await _dismiss(api, sheet, "not-a-uuid")).status_code == 422


@pytest.mark.endpoint("photo_links_dismiss", case="not_found")
async def test_dismiss_unknown_404(sheet: Sheet, api: httpx.AsyncClient) -> None:
    assert (await _dismiss(api, sheet, uuid.uuid4())).status_code == 404


@pytest.mark.endpoint("photo_links_dismiss", case="wrong_tenant")
async def test_dismiss_other_tenant_404(sheet: Sheet, api: httpx.AsyncClient) -> None:
    _product_id, link = _failed_link(sheet, "bharat")
    assert (await _dismiss(api, sheet, link)).status_code == 404
    assert _status(sheet, link) == "failed"


@pytest.mark.endpoint("photo_links_dismiss", case="wrong_role")
async def test_dismiss_reviewer_403(sheet: Sheet, api: httpx.AsyncClient) -> None:
    _product_id, link = _failed_link(sheet)
    assert (await _dismiss(api, sheet, link, "reviewer@acme.test")).status_code == 403
    assert _status(sheet, link) == "failed"


@pytest.mark.endpoint("photo_links_dismiss", case="missing_auth")
async def test_dismiss_missing_auth_401(sheet: Sheet, api: httpx.AsyncClient) -> None:
    assert (await api.delete(f"/api/v1/photo-links/{uuid.uuid4()}")).status_code == 401


@pytest.mark.endpoint("photo_links_dismiss", case="invalid_token", variant="expired")
@pytest.mark.endpoint("photo_links_dismiss", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("photo_links_dismiss", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_dismiss_invalid_token_401(
    sheet: Sheet, api: httpx.AsyncClient, variant: str
) -> None:
    response = await api.delete(
        f"/api/v1/photo-links/{uuid.uuid4()}", headers=bad_token(sheet.jw, variant)
    )
    assert response.status_code == 401
