"""S16 tester round 2: attacks on the merged-profile cap of `PATCH /api/v1/tenants/me` (fix F2).

A stored profile already over `MAX_PROFILE_CHUNKS` (written before the fix, or by the migrator)
must not lock the owner out: a name-only PATCH still lands, a voice PATCH that stays over the cap is
refused with no audit row and no job, and one that brings it under the cap lands and embeds."""

from __future__ import annotations

import json
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
GLOSSARY = {f"term{i:03d}": f"pref{i:03d}" for i in range(500)}
GUIDELINES = [f"Guideline number {i}." for i in range(100)]
OVER_TONE = "\n".join(f"- t{i:03d}" for i in range(150))  # 500 + 100 + 150 = 750 chunks


def _store_over_cap(jw: JobWorld) -> None:
    jw.conn.execute(
        "UPDATE tenants SET tone = %s, guidelines = %s::jsonb, glossary = %s::jsonb WHERE id = %s",
        [OVER_TONE, json.dumps(GUIDELINES), json.dumps(GLOSSARY), jw.tenant("acme")],
    )


async def _patch(api: httpx.AsyncClient, jw: JobWorld, body: object) -> httpx.Response:
    headers = as_member(jw, "owner@acme.test")
    tag = (await api.get(URL, headers=headers)).headers["ETag"]
    return await api.patch(URL, json=body, headers={**headers, "If-Match": tag})


def _profile_jobs(jw: JobWorld) -> int:
    return count(
        jw.conn, "SELECT count(*) FROM job_runs WHERE payload_json->>'source' = 'brand_profile'"
    )


def _audits(jw: JobWorld) -> int:
    return count(jw.conn, "SELECT count(*) FROM audit_log WHERE action = 'tenant.profile_updated'")


@pytest.mark.endpoint("tenants_me_update", case="success")
async def test_over_cap_stored_profile_name_only_patch_lands(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    _store_over_cap(jobs_db)
    response = await _patch(api, jobs_db, {"name": "Acme Renamed"})
    assert response.status_code == 200, response.text[:200]
    assert response.json()["name"] == "Acme Renamed"
    assert _profile_jobs(jobs_db) == 0  # no voice field changed


@pytest.mark.endpoint("tenants_me_update", case="validation")
async def test_over_cap_stored_profile_voice_patch_still_over_refused_no_side_effects(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    _store_over_cap(jobs_db)
    audits = _audits(jobs_db)
    # shrinks the profile by 10 chunks (750 -> 740) but stays over the cap
    response = await _patch(api, jobs_db, {"guidelines": GUIDELINES[:90]})
    assert response.status_code == 422, response.text[:200]
    error = response.json()["error"]
    assert error["type"] == "VALIDATION_FAILED"
    assert error["details"][0]["loc"] == ["body", "profile"]
    assert _audits(jobs_db) == audits
    assert _profile_jobs(jobs_db) == 0
    stored = (await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))).json()
    assert stored["guidelines"] == GUIDELINES


@pytest.mark.endpoint("tenants_me_update", case="success")
async def test_over_cap_stored_profile_voice_patch_under_cap_lands_and_embeds(
    jobs_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import brand_embed
    from app.workers.registry import JobContext
    from mkos_schemas.enums import JobKind

    _store_over_cap(jobs_db)
    response = await _patch(api, jobs_db, {"tone": None})  # 750 -> 600
    assert response.status_code == 200, response.text[:200]
    assert _profile_jobs(jobs_db) == 1
    monkeypatch.setattr(brand_embed, "embedder", lambda: FakeEmbedder())
    acme = jobs_db.tenant("acme")
    ctx = JobContext(
        uuid.uuid4(), acme, JobKind.BRAND_EMBED, "req-r2", {"source": "brand_profile"}, 1
    )
    await brand_embed.run_brand_embed(ctx)
    rows = [r for r in chunk_rows(jobs_db.conn, acme) if r["source_type"] == "brand_profile"]
    assert len(rows) == 600


@pytest.mark.endpoint("tenants_me_update", case="validation")
async def test_merged_cap_checked_after_etag_stale_wins(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    _store_over_cap(jobs_db)
    headers = as_member(jobs_db, "owner@acme.test")
    response = await api.patch(
        URL, json={"tone": "- one more"}, headers={**headers, "If-Match": f'"{uuid.uuid4()}"'}
    )
    assert response.status_code == 409, response.text[:200]
    assert response.json()["error"]["type"] == "STALE_VERSION"
