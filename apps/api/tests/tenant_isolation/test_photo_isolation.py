"""SI3 T12 AC15 (CANON.md:137): photo facts as `app_rw` on `mos_iso`. An observation naming another
tenant's image writes nothing, and the skip rule never reads another tenant's photo set."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from tests.integration import factories as f
from tests.integration.photo_support import PhotoWorld, build_photo_world, generate, spy_pipeline

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
async def world(
    clean_tenants: None, admin_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> PhotoWorld:
    return await build_photo_world(admin_conn, monkeypatch)


def _bharat_image(pw: PhotoWorld) -> uuid.UUID:
    bharat = pw.world.tenant("bharat")
    product = pw.world.product("bharat", "KURTA-002")
    image_id = uuid.uuid4()
    f.image(
        pw.conn,
        bharat,
        product,
        id=image_id,
        storage_key=f"{bharat}/{product}/images/{image_id}",
        slot="main",
        clean_content_hash="d" * 64,
        clean_mime_type="image/jpeg",
        metadata={"vision": {"w": 64, "h": 80}},
    )
    return image_id


def _ai(pw: PhotoWorld) -> dict[str, Any]:
    return {k: v for k, v in pw.attributes().items() if v.get("source") == "ai"}


async def test_iso_an_observation_on_another_tenants_image_writes_nothing(
    world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = world
    theirs = _bharat_image(pw)

    async def around(gctx: Any, deps: Any, real: Any) -> Any:
        result = await real(gctx, deps)
        moved = [o.model_copy(update={"image_id": theirs}) for o in result.photo_observations]
        return result.model_copy(update={"photo_observations": moved})

    spy_pipeline(pw, monkeypatch, around)
    accepted = await generate(pw, "photo_ok")
    assert pw.seen[-1].photo_targets
    assert _ai(pw) == {} and pw.tasks() == []
    assert pw.world.listing(accepted.listing_id)["current_version_id"] is not None


async def test_iso_the_skip_rule_never_reads_another_tenants_set(
    world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = world
    bharat = pw.world.tenant("bharat")
    their_product = pw.world.product("bharat", "KURTA-002")
    listing = f.listing(pw.conn, bharat, their_product, category="ethnic_wear_kurta_sets")
    pw.conn.execute(
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, listing_id, payload_json, "
        "correlation_id, status, attempt, started_at, finished_at, result_json) VALUES "
        "(%s, 'listing_generate', 'listing', %s, %s, '{}', gen_random_uuid(), 'succeeded', 1, "
        "now(), now(), %s)",
        [bharat, listing, listing, Jsonb({"photo_set": pw.sent_set()})],
    )
    spy_pipeline(pw, monkeypatch)
    await generate(pw, "photo_ok")
    assert pw.seen[-1].photo_targets != []
    assert _ai(pw)
