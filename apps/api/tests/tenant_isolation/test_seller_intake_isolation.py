"""SI1 T24 (AC18, AC22; invariant 5): the seller intake reads and writes as `app_rw` on
`mos_iso`. Another tenant's product, image, party and brand row are never read or written, and
a forged party id of another tenant is refused by the composite foreign key."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from tests.integration import factories as f
from tests.integration.auth_support import World, build_world

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def world(clean_tenants: None, admin_conn: Conn) -> World:
    return build_world(admin_conn)


def _image(conn: Conn, tenant: uuid.UUID, **cols: Any) -> tuple[uuid.UUID, uuid.UUID]:
    from app.services.image_keys import image_object_key

    p = f.product(conn, tenant, category="kurtas")
    image_id = uuid.uuid4()
    f.image(
        conn, tenant, p, id=image_id, slot="main", storage_key=image_object_key(tenant, p, image_id),
        **cols,
    )  # fmt: skip
    return p, image_id


async def test_iso_intake_repository_reads(world: World, admin_conn: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    p, image_id = _image(
        admin_conn, bharat, clean_content_hash="a" * 64, clean_mime_type="image/jpeg"
    )
    party = f.party(admin_conn, bharat, legal_name="Bharat Maker Pvt Ltd")
    async with tenant_uow(acme) as uow:
        assert await uow.products.get_for_update(p) is None
        assert await uow.products.child_prices(p) == []
        assert await uow.products.live_child_ids(p) == []
        assert await uow.images.listing_set(p) == []
        assert await uow.images.details_for_product(p) == []
        assert await uow.images.get_any(image_id) is None
        assert await uow.images.set_public_id(image_id, "a" * 26) is None
        assert await uow.images.mark_public_written(image_id, "a" * 26) is None
        assert await uow.images.clear_objects(image_id) is False
        assert await uow.images.deleted_with_objects(10) == []
        assert await uow.images.live_without_derivatives() == []
        assert await uow.parties.get(party) is None
        assert await uow.colourways.colourways_of(p) == []
        assert await uow.listings.any_for_product(p) is False
    row = admin_conn.execute("SELECT public_id FROM images WHERE id = %s", [image_id]).fetchone()
    assert row == (None,)


async def test_iso_brand_profile_only_the_tid_row(world: World, admin_conn: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    theirs = f.party(admin_conn, bharat)
    f.brand_profile(
        admin_conn,
        bharat,
        default_manufacturer_party_id=theirs,
        provenance={"manufacturer": f.fact(value=None)},
    )
    async with tenant_uow(acme) as uow:
        assert await uow.brand_profiles.default_manufacturer() is None
        assert await uow.brand_profiles.get() is None
    stored = admin_conn.execute(
        "SELECT default_manufacturer_party_id FROM brand_profiles WHERE tenant_id = %s", [bharat]
    ).fetchone()
    assert stored == (theirs,)


async def test_iso_forged_party_id_refused(world: World, admin_conn: Conn) -> None:
    from app.core.errors import ConflictError, InputError
    from app.repositories.uow import tenant_uow
    from sqlalchemy.exc import IntegrityError

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    mine = f.product(admin_conn, acme, category="kurtas")
    theirs = f.party(admin_conn, bharat)
    fact = {"source": "seller", "captured_via": "api", "updated_at": "2026-11-06T00:00:00Z"}
    with pytest.raises((IntegrityError, ConflictError, InputError)):
        async with tenant_uow(acme) as uow:
            await uow.facts.set_typed(mine, "manufacturer_party_id", theirs, fact)
    row = admin_conn.execute(
        "SELECT manufacturer_party_id FROM products WHERE id = %s", [mine]
    ).fetchone()
    assert row == (None,)


async def test_iso_public_copy_of_another_tenant(world: World, admin_conn: Conn) -> None:
    from app.services.public_images import PublicImage, ensure_public
    from app.workers.errors import JobFailed

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    _, image_id = _image(admin_conn, bharat)
    with pytest.raises(JobFailed):
        await ensure_public(
            acme, PublicImage(image_id, 0, None, "a" * 64, "image/jpeg", None, None)
        )
    assert admin_conn.execute(
        "SELECT public_id FROM images WHERE id = %s", [image_id]
    ).fetchone() == (None,)
