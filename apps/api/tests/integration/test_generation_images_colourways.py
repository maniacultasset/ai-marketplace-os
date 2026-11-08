"""SI1 T18 (AC11; decision 19; CP15), fix round 1: a style with two colourways through the real
generation worker. The saved version holds the images in decision 19 order, cut at 20; the
Rules Engine re-check judged those pinned images, not the pipeline's; and the adapter input
maps each colour to its own images. Run in the Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]

FAMILY = "KURTA-FAM-01"  # the demo seed's size_color style: navy blue (position 1), maroon (2)


def _image(
    world: GenWorld,
    product_id: uuid.UUID,
    slot: str,
    *,
    colourway: uuid.UUID | None = None,
    position: int = 0,
) -> uuid.UUID:
    acme = world.tenant("acme")
    image_id = uuid.uuid4()
    f.image(
        world.conn,
        acme,
        product_id,
        id=image_id,
        storage_key=f"{acme}/{product_id}/images/{image_id}",
        slot=slot,
        colourway_id=colourway,
        position=position,
        clean_content_hash="a" * 64,
        clean_mime_type="image/jpeg",
        metadata={"vision": {"w": 64, "h": 80}},
    )
    return image_id


def _colourways(world: GenWorld, style: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = world.conn.execute(
        "SELECT colour_key, id FROM product_colourways WHERE style_product_id = %s "
        "AND deleted_at IS NULL",
        [style],
    ).fetchall()
    return {str(k): v for k, v in rows}


async def _generate(
    world: GenWorld, monkeypatch: pytest.MonkeyPatch, sku: str
) -> tuple[Any, list[Any], list[Any]]:
    """Run one generation; returns the accepted item, the listings the pipeline returned and
    the listings the backend's re-check judged."""
    from app.services.generation_result import recheck as real_recheck
    from app.workers import generation, wrapper
    from app.workers.tasks import register_bodies
    from mkos_pipeline import run_listing_pipeline as real_pipeline

    returned: list[Any] = []
    judged: list[Any] = []

    async def pipeline_spy(gctx: Any, deps: Any) -> Any:
        result = await real_pipeline(gctx, deps)
        returned.append(result.listing)
        return result

    def recheck_spy(listing: Any, rules: Any, product: Any) -> Any:
        judged.append(listing)
        return real_recheck(listing, rules, product)

    monkeypatch.setattr(generation, "run_listing_pipeline", pipeline_spy)
    monkeypatch.setattr(generation, "recheck", recheck_spy)
    accepted = await make_generating(world, sku=sku)
    register_bodies()
    await wrapper.run_job(str(accepted.job_id))
    return accepted, returned, judged


def _version(world: GenWorld, job_id: uuid.UUID) -> dict[str, Any]:
    row = world.conn.execute(
        "SELECT content_json FROM listing_versions WHERE job_id = %s", [job_id]
    ).fetchone()
    assert row is not None, "no version saved"
    content: dict[str, Any] = row[0]
    return content


async def test_recheck_judges_the_pinned_images(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CP15 (the T18 guard-must-fail): the stub returns no images on a first generation; the
    backend pins the product's images before `recheck`, so the stored re-check judged exactly
    the images the version holds. Dropping the `replace` before `recheck` turns this red."""
    product_id = gen_world.product("acme", "KURTA-002")
    main = _image(gen_world, product_id, "main")
    second = _image(gen_world, product_id, "secondary")
    accepted, returned, judged = await _generate(gen_world, monkeypatch, "KURTA-002")
    assert returned and returned[0] is not None and list(returned[0].images) == []
    assert len(judged) == 1
    assert [i.image_id for i in judged[0].images] == [main, second]
    stored = _version(gen_world, accepted.job_id)["images"]
    assert [i["image_id"] for i in stored] == [str(main), str(second)]


async def test_style_with_two_colourways_order_cap_and_colour_refs(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC11: 24 images (style 6, navy blue 15, maroon 3) → 20 in decision 19 order, so the
    later colourway's main survives the cut; `colour_image_refs` maps each colour to its own
    images in the saved version."""
    from app.repositories.uow import tenant_uow
    from app.services.adapter_input import build_adapter_listing

    style = gen_world.product("acme", FAMILY)
    assert gen_world.count("images", "product_id = %s", [style]) == 0
    cw = _colourways(gen_world, style)
    assert set(cw) == {"navy blue", "maroon"}
    style_main = _image(gen_world, style, "main")
    style_secs = [_image(gen_world, style, "secondary", position=i) for i in range(5)]
    navy_main = _image(gen_world, style, "main", colourway=cw["navy blue"])
    navy_secs = [
        _image(gen_world, style, "secondary", colourway=cw["navy blue"], position=i)
        for i in range(1, 15)
    ]
    maroon_main = _image(gen_world, style, "main", colourway=cw["maroon"])
    for i in range(1, 3):
        _image(gen_world, style, "secondary", colourway=cw["maroon"], position=i)

    accepted, _, judged = await _generate(gen_world, monkeypatch, FAMILY)
    expected = [style_main, *style_secs, navy_main, maroon_main, *navy_secs[:12]]
    images = _version(gen_world, accepted.job_id)["images"]
    assert [i["image_id"] for i in images] == [str(i) for i in expected]
    assert [i["position"] for i in images] == list(range(20))
    assert [i.image_id for i in judged[0].images] == expected

    listing_id = gen_world.listing_of(style)
    assert listing_id is not None
    async with tenant_uow(gen_world.tenant("acme")) as uow:
        source = await uow.listings.preview_source(listing_id)
        assert source is not None
        facts = await uow.product_facts.resolve_family(source.product.id)
    assert facts is not None
    refs = {i: f"image:{i}" for i in expected}
    built = build_adapter_listing(source, facts=facts, image_refs=refs, idempotency_key="k")
    assert dict(built.colour_image_refs) == {
        "navy blue": tuple(f"image:{i}" for i in (navy_main, *navy_secs[:12])),
        "maroon": (f"image:{maroon_main}",),
    }
