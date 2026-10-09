"""S16 fix round 1 (tester R1): `PATCH /api/v1/tenants/me` checks the merged profile, the patch
overlaid on the stored row, against `MAX_PROFILE_CHUNKS` before any UPDATE. Per-field caps alone
let three accepted PATCHes build a 701-chunk profile that the `brand_embed` job then refuses."""

from __future__ import annotations

import uuid

import httpx
import pytest

from .auth_support import PREFIX
from .brand_support import FakeEmbedder, chunk_rows
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, as_member, count, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
URL = f"{PREFIX}/tenants/me"
GLOSSARY = {f"term{i:03d}": f"pref{i:03d}" for i in range(500)}  # schema max 500
GUIDELINES = [f"Guideline number {i}." for i in range(100)]  # schema max 100


def _tone(bullets: int) -> str:
    return "\n".join(f"- t{i:03d}" for i in range(bullets))  # 101 bullets: 706 chars, under 2000


async def _patch(api: httpx.AsyncClient, jw: JobWorld, body: object) -> httpx.Response:
    headers = as_member(jw, "owner@acme.test")
    tag = (await api.get(URL, headers=headers)).headers["ETag"]
    return await api.patch(URL, json=body, headers={**headers, "If-Match": tag})


def _profile_jobs(jw: JobWorld) -> int:
    return count(
        jw.conn, "SELECT count(*) FROM job_runs WHERE payload_json->>'source' = 'brand_profile'"
    )


def _assert_profile_422(response: httpx.Response) -> None:
    assert response.status_code == 422, response.text[:200]
    error = response.json()["error"]
    assert error["type"] == "VALIDATION_FAILED"
    assert error["details"][0]["loc"] == ["body", "profile"]


async def _embed_profile(jw: JobWorld, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    from app.services import brand_embed
    from app.workers.registry import JobContext
    from mkos_schemas.enums import JobKind

    monkeypatch.setattr(brand_embed, "embedder", lambda: FakeEmbedder())
    acme = jw.tenant("acme")
    ctx = JobContext(
        uuid.uuid4(), acme, JobKind.BRAND_EMBED, "req-cap", {"source": "brand_profile"}, 1
    )
    await brand_embed.run_brand_embed(ctx)
    return [r for r in chunk_rows(jw.conn, acme) if r["source_type"] == "brand_profile"]


@pytest.mark.endpoint("tenants_me_update", case="validation")
async def test_tenants_me_update_merged_profile_over_cap_one_patch_422(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    before = (await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))).json()
    body = {"glossary": GLOSSARY, "guidelines": GUIDELINES, "tone": _tone(101)}
    _assert_profile_422(await _patch(api, jobs_db, body))
    after = (await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))).json()
    assert after == before
    assert _profile_jobs(jobs_db) == 0


@pytest.mark.endpoint("tenants_me_update", case="validation")
async def test_tenants_me_update_merged_profile_over_cap_across_patches_422(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for body in ({"glossary": GLOSSARY}, {"guidelines": GUIDELINES}):
        response = await _patch(api, jobs_db, body)
        assert response.status_code == 200, response.text[:200]
    _assert_profile_422(await _patch(api, jobs_db, {"tone": _tone(101)}))
    stored = (await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))).json()
    assert stored["tone"] != _tone(101)
    # what was accepted still embeds
    assert len(await _embed_profile(jobs_db, monkeypatch)) == 600


@pytest.mark.endpoint("tenants_me_update", case="success")
async def test_tenants_me_update_merged_profile_at_cap_embeds(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for body in ({"glossary": GLOSSARY}, {"guidelines": GUIDELINES}, {"tone": _tone(100)}):
        response = await _patch(api, jobs_db, body)
        assert response.status_code == 200, response.text[:200]
    assert len(await _embed_profile(jobs_db, monkeypatch)) == 700
    # clearing the tone with null shrinks the merged profile, so a longer tone fits after it
    response = await _patch(api, jobs_db, {"tone": None, "guidelines": GUIDELINES[:50]})
    assert response.status_code == 200, response.text[:200]
    response = await _patch(api, jobs_db, {"tone": _tone(101)})
    assert response.status_code == 200, response.text[:200]
