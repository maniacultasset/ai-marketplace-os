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


async def test_iso_another_tenants_reject_is_404_and_the_marker_holds(world: PhotoWorld) -> None:
    """SI9 T10: tenant B's PATCH on tenant A's product is 404; A's marker is unchanged."""
    from app.core.errors import NotFoundError
    from app.services import product_edit

    pw = world
    pw.conn.execute(
        "UPDATE products SET photo_rejections = %s WHERE id = %s",
        [Jsonb({"neckline": "v-neck"}), pw.product_id],
    )
    them = pw.world.principal("operator@bharat.test")
    with pytest.raises(NotFoundError):
        await product_edit.update_product(
            them, pw.product_id, {"attributes": {"neckline": None}}, "W/x"
        )
    row = pw.conn.execute(
        "SELECT photo_rejections FROM products WHERE id = %s", [pw.product_id]
    ).fetchone()
    assert row is not None and row[0] == {"neckline": "v-neck"}


async def test_iso_a_reject_revalidates_none_of_another_tenants_listings(
    world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SI9 T11: the re-validate on a reject reads only the product's own (tenant's) listings."""
    from app.services import product_edit, products

    pw = world
    bharat = pw.world.tenant("bharat")
    runs = pw.conn.execute(
        "SELECT count(*) FROM validation_runs WHERE tenant_id = %s", [bharat]
    ).fetchone()
    spy_pipeline(pw, monkeypatch)
    await generate(pw, "photo_ok")
    principal = pw.world.principal("operator@acme.test")
    view = await products.get_product(principal, pw.product_id)
    await product_edit.update_product(
        principal, pw.product_id, {"attributes": {"neckline": None}}, view.etag
    )
    after = pw.conn.execute(
        "SELECT count(*) FROM validation_runs WHERE tenant_id = %s", [bharat]
    ).fetchone()
    assert after == runs


async def test_iso_photo_read_never_crosses_tenants(
    world: PhotoWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SI9 T13: tenant B never reads A's product, and B's own product never shows A's job."""
    from app.core.errors import NotFoundError
    from app.services import products

    pw = world
    spy_pipeline(pw, monkeypatch)
    await generate(pw, "photo_ok")
    them = pw.world.principal("operator@bharat.test")
    with pytest.raises(NotFoundError):
        await products.get_product(them, pw.product_id)
    theirs = await products.get_product(them, pw.world.product("bharat", "KURTA-002"))
    assert theirs.photo_read is None
