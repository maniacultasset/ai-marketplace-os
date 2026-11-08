"""SI7 T17 (AC3, AC12, AC13): the candidate flow end to end over HTTP, against the compose MinIO,
the real killable children and the stub pipeline: upload a main, see the `fixes` it offers, ask
for a candidate, run its job, list it with signed previews, accept it, and see the product's new
main with the old one kept as an additional image."""

from __future__ import annotations

import io
import uuid
from typing import Any

import httpx
import pytest
from PIL import Image as PILImage
from PIL import ImageDraw

from .candidates_support import edit_on
from .conftest import LOCAL_HOSTS
from .images_support import headers, images_db, product, run_image_job, upload_file
from .jobs_support import JobWorld, as_member, count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["edit_on", "images_db"]
CANARY_KEY = f"sk-or-canary-{uuid.uuid4().hex}"


async def _check_storage() -> None:
    from app.adapters.storage import get_storage, images_bucket
    from app.core.config import get_settings

    try:
        get_storage()._client.head_bucket(Bucket=images_bucket())
    except Exception:
        pytest.fail(f"MinIO not reachable on {get_settings().s3_endpoint_url}")


def _photo(bg: tuple[int, int, int], size: tuple[int, int] = (900, 1200)) -> bytes:
    img = PILImage.new("RGB", size, bg)
    w, h = size
    ImageDraw.Draw(img).rectangle((w // 5, h // 5, 4 * w // 5, 4 * h // 5), fill=(40, 70, 160))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


async def _product(api: httpx.AsyncClient, jw: JobWorld, p: uuid.UUID) -> dict[str, Any]:
    response = await api.get(f"/api/v1/products/{p}", headers=as_member(jw, "operator@acme.test"))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _upload_main(api: httpx.AsyncClient, jw: JobWorld, p: uuid.UUID, data: bytes) -> str:
    uploaded = await upload_file(api, headers(jw), p, data)
    assert uploaded.status_code == 202, uploaded.text
    await run_image_job(uuid.UUID(uploaded.json()["job_id"]))
    return str(uploaded.json()["image_id"])


async def _make(
    api: httpx.AsyncClient, jw: JobWorld, p: uuid.UUID, kind: str, source: str
) -> dict[str, Any]:
    made = await api.post(
        f"/api/v1/products/{p}/image-candidates",
        json={"kind": kind, "source_image_id": source},
        headers=headers(jw),
    )
    assert made.status_code == 202, made.text
    job = await api.get(made.json()["poll_url"], headers=as_member(jw, "operator@acme.test"))
    assert job.status_code == 200 and job.json()["status"] == "queued"
    await run_image_job(uuid.UUID(made.json()["job_id"]))
    done = await api.get(made.json()["poll_url"], headers=as_member(jw, "operator@acme.test"))
    assert done.json()["status"] == "succeeded", done.text
    listed = await api.get(
        f"/api/v1/products/{p}/image-candidates", headers=as_member(jw, "operator@acme.test")
    )
    (item,) = [i for i in listed.json()["items"] if i["id"] == made.json()["candidate_id"]]
    found: dict[str, Any] = item
    return found


async def _accept_and_check(
    api: httpx.AsyncClient, jw: JobWorld, p: uuid.UUID, item: dict[str, Any], old_main: str
) -> str:
    accepted = await api.post(f"/api/v1/image-candidates/{item['id']}/accept", headers=headers(jw))
    assert accepted.status_code == 202, accepted.text
    await run_image_job(uuid.UUID(accepted.json()["job_id"]))
    details = {d["image_id"]: d for d in (await _product(api, jw, p))["image_details"]}
    new_id = str(accepted.json()["image_id"])
    assert details[new_id]["slot"] == "main" and details[new_id]["parent_image_id"] == old_main
    assert details[old_main]["slot"] == "secondary"
    return new_id


async def test_fix_flow_over_http(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    await _check_storage()
    p = product(images_db.conn, images_db.tenant("acme"))
    main = await _upload_main(api, images_db, p, _photo((245, 245, 245)))
    details = {d["image_id"]: d for d in (await _product(api, images_db, p))["image_details"]}
    assert details[main]["fixes"] == ["fix"]
    item = await _make(api, images_db, p, "fix", main)
    assert item["status"] == "ready", item["failure_reason"]
    assert (
        "X-Amz-Signature" in item["preview_url"] and "X-Amz-Signature" in item["source_preview_url"]
    )
    async with httpx.AsyncClient() as client:
        preview = await client.get(item["preview_url"])
    assert preview.status_code == 200 and preview.content[:3] == b"\xff\xd8\xff"
    new_id = await _accept_and_check(api, images_db, p, item, main)
    details = {d["image_id"]: d for d in (await _product(api, images_db, p))["image_details"]}
    assert details[new_id]["origin"] == "rendered"
    assert count(images_db.conn, "SELECT count(*) FROM agent_runs") == 0


async def test_white_background_flow_over_http(
    images_db: JobWorld,
    api: httpx.AsyncClient,
    edit_on: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.core.config import get_settings
    from app.core.logging import configure_logging

    await _check_storage()
    edit_on.setenv("LLM_GATEWAY", "openrouter")
    edit_on.setenv("OPENROUTER_API_KEY", CANARY_KEY)
    get_settings.cache_clear()
    configure_logging(get_settings())
    p = product(images_db.conn, images_db.tenant("acme"))
    grey = (200, 200, 200)
    main = await _upload_main(api, images_db, p, _photo(grey, (1600, 1600)))
    details = {d["image_id"]: d for d in (await _product(api, images_db, p))["image_details"]}
    assert details[main]["fixes"] == ["white_background"]
    # the stub echoes the grey photo: the backend's own checks call it not white
    item = await _make(api, images_db, p, "white_background", main)
    assert (item["status"], item["failure_reason"]) == ("failed", "not_white")
    assert count(images_db.conn, "SELECT count(*) FROM agent_runs") == 1
    off_white = await _upload_main(api, images_db, p, _photo((240, 240, 240), (1600, 1600)))
    item = await _make(api, images_db, p, "white_background", off_white)
    assert item["status"] == "ready", item["failure_reason"]
    assert item["model"] == "google/gemini-2.5-flash-image" and item["cost_usd"] == "0.039000"
    new_id = await _accept_and_check(api, images_db, p, item, off_white)
    details = {d["image_id"]: d for d in (await _product(api, images_db, p))["image_details"]}
    assert details[new_id]["origin"] == "generated"
    assert count(images_db.conn, "SELECT count(*) FROM agent_runs") == 2
    for query in (
        "SELECT string_agg(t::text, '|') FROM agent_runs t",
        "SELECT string_agg(t::text, '|') FROM job_runs t",
        "SELECT string_agg(t::text, '|') FROM image_candidates t",
    ):
        row = images_db.conn.execute(query).fetchone()
        assert row is not None and CANARY_KEY not in (row[0] or "")
    logged = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    assert CANARY_KEY not in logged
    get_settings.cache_clear()
