"""S22 re-check (F2): colourway images a Flipkart child sends come only from the approved
version, under drift after approval, a legacy version body and a second tenant."""

from __future__ import annotations

import uuid

import pytest

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .family_support import family
from .seam_support import CATEGORY, Conn
from .test_adapter_input import CONTENT, _colourways, _fk_listing, _full_body, _source

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


def _refs(*ids: uuid.UUID) -> dict[uuid.UUID, str]:
    return {i: f"image:{i}" for i in ids}


async def test_colour_images_stable_across_post_approval_uploads(
    products_world: World, auth_db: Conn
) -> None:
    """Preview, publish and replay of one version send the same colourway images: a colourway
    image uploaded after approval (even marked main) changes nothing."""
    from app.services.adapter_input import build_adapter_listing, colour_image_ids

    acme = products_world.tenant("acme")
    style, _ = family(auth_db, acme, "FK-RC1", category=CATEGORY)
    cw = _colourways(auth_db, style)
    style_image = f.image(auth_db, acme, style)
    navy = f.image(auth_db, acme, style, colourway_id=cw["navy blue"], position=4)
    listing_id = _fk_listing(auth_db, acme, style, _full_body(style_image, navy))
    before = await _source(products_world, listing_id)
    first = build_adapter_listing(before, image_refs=_refs(style_image, navy), idempotency_key="k")

    late = f.image(auth_db, acme, style, colourway_id=cw["navy blue"], slot="main", position=0)
    late_maroon = f.image(auth_db, acme, style, colourway_id=cw["maroon"], slot="main")
    after = await _source(products_world, listing_id)
    assert late in after.colour_images["navy blue"]  # the raw live read does see it
    assert colour_image_ids(after) == {navy}
    replay = build_adapter_listing(
        after, image_refs=_refs(style_image, navy, late, late_maroon), idempotency_key="k"
    )
    assert dict(replay.colour_image_refs) == dict(first.colour_image_refs)
    assert dict(replay.colour_image_refs) == {"navy blue": (f"image:{navy}",)}


async def test_colour_image_deleted_after_approval_not_sent(
    products_world: World, auth_db: Conn
) -> None:
    from app.services.adapter_input import build_adapter_listing, colour_image_ids

    acme = products_world.tenant("acme")
    style, _ = family(auth_db, acme, "FK-RC2", category=CATEGORY)
    cw = _colourways(auth_db, style)
    style_image = f.image(auth_db, acme, style)
    navy = f.image(auth_db, acme, style, colourway_id=cw["navy blue"], slot="main")
    listing_id = _fk_listing(auth_db, acme, style, _full_body(style_image, navy))
    auth_db.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [navy])
    source = await _source(products_world, listing_id)
    assert colour_image_ids(source) == set()
    built = build_adapter_listing(source, image_refs=_refs(style_image), idempotency_key="k")
    assert dict(built.colour_image_refs) == {}


async def test_legacy_version_body_sends_no_colour_images(
    products_world: World, auth_db: Conn
) -> None:
    """A version body without `content` carries no images: no colourway image is sent."""
    from app.services.adapter_input import build_adapter_listing, colour_image_ids

    acme = products_world.tenant("acme")
    style, _ = family(auth_db, acme, "FK-RC3", category=CATEGORY)
    cw = _colourways(auth_db, style)
    navy = f.image(auth_db, acme, style, colourway_id=cw["navy blue"], slot="main")
    source = await _source(products_world, _fk_listing(auth_db, acme, style, dict(CONTENT)))
    assert navy in source.colour_images["navy blue"]
    assert colour_image_ids(source) == set()
    built = build_adapter_listing(source, image_refs=_refs(navy), idempotency_key="k")
    assert dict(built.colour_image_refs) == {}


async def test_other_tenant_colour_image_in_version_never_sent(
    products_world: World, auth_db: Conn
) -> None:
    """A version naming another tenant's colourway image id gains nothing: the colourway set is
    read under the caller's tenant only (invariant 5)."""
    from app.services.adapter_input import colour_image_ids

    acme = products_world.tenant("acme")
    other = products_world.tenant("bharat")
    their_style, _ = family(auth_db, other, "FK-RC4B", category=CATEGORY)
    their = f.image(
        auth_db, other, their_style, colourway_id=_colourways(auth_db, their_style)["navy blue"]
    )
    style, _ = family(auth_db, acme, "FK-RC4A", category=CATEGORY)
    style_image = f.image(auth_db, acme, style)
    source = await _source(
        products_world, _fk_listing(auth_db, acme, style, _full_body(style_image, their))
    )
    assert their not in colour_image_ids(source)
    assert all(their not in ids for ids in source.colour_images.values())
