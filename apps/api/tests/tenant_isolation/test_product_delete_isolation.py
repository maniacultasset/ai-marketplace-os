"""PD1 (CANON.md:137): the product delete as `app_rw` on `mos_iso`. Every repository write the
delete makes, aimed at another tenant's rows, changes nothing; every read returns nothing; the
service and the bulk form name the foreign product as missing and leave it whole."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import psycopg
import pytest
from tests.integration import factories as f
from tests.integration.auth_support import World, build_world
from tests.integration.family_support import family

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]
TABLES = (
    "products",
    "listings",
    "human_tasks",
    "listing_issues",
    "images",
    "job_runs",
    "image_candidates",
    "variant_matrix_cells",
)


@pytest.fixture
def world(clean_tenants: None, admin_conn: Conn) -> World:
    return build_world(admin_conn)


def _principal(world: World, email: str) -> Any:
    from app.core.rbac import Principal

    slug = email.split("@", 1)[1].split(".", 1)[0]
    return Principal(
        user_id=world.user(email),
        tenant_id=world.tenant(slug),
        role=email.split("@", 1)[0],  # type: ignore[arg-type]
        email=email,
        jti=str(uuid.uuid4()),
    )


def _fingerprint(conn: Conn, tenant: uuid.UUID) -> str:
    out = []
    for table in TABLES:
        rows = conn.execute(
            f"SELECT t::text FROM {table} t WHERE tenant_id = %s ORDER BY t::text",  # noqa: S608
            [tenant],
        ).fetchall()
        out.append(table + ":" + "\n".join(str(r[0]) for r in rows))
    return hashlib.sha256("\n".join(out).encode()).hexdigest()


def _theirs(conn: Conn, world: World) -> dict[str, Any]:
    """bharat's standalone product with a listing, an open issue, an open task, a photo and a
    queued photo check, and a style with its sizes."""
    bharat = world.tenant("bharat")
    product = f.product(conn, bharat, category="t_shirts")
    listing = f.listing(conn, bharat, product)
    issue = f.listing_issue(conn, bharat, listing)
    task = f.human_task(conn, bharat, product_id=product)
    image = f.image(conn, bharat, product)
    job = f.insert(
        conn,
        "job_runs",
        tenant_id=bharat,
        kind="image_compliance",
        entity_type="image",
        entity_id=image,
        max_attempts=1,
        payload_json={},
        correlation_id=uuid.uuid4(),
    )
    style, children = family(conn, bharat, f"ISO-PD1-{uuid.uuid4().hex[:6]}")
    return {
        "product": product,
        "listing": listing,
        "issue": issue,
        "task": task,
        "image": image,
        "job": job,
        "style": style,
        "children": list(children.values()),
    }


async def test_iso_delete_writes_and_reads_touch_no_other_tenant(
    world: World, admin_conn: Conn
) -> None:
    from app.repositories.uow import tenant_uow
    from app.repositories.variants import MatrixCellRepository

    theirs = _theirs(admin_conn, world)
    bharat = world.tenant("bharat")
    before = _fingerprint(admin_conn, bharat)
    acme = world.tenant("acme")
    acme_owner = world.user("owner@acme.test")
    family_ids = [theirs["product"], theirs["style"], *theirs["children"]]
    async with tenant_uow(acme, user_id=acme_owner) as uow:
        assert await uow.products.get_for_update(theirs["product"]) is None
        assert await uow.products.live_child_ids(theirs["style"]) == []
        assert await uow.listings.for_product_for_update(theirs["product"]) == []
        assert await uow.images.live_ids_for_products(family_ids) == []
        assert await uow.image_candidates.ids_for_products(family_ids) == []
        assert (
            await uow.job_runs.live_for_product(
                listing_ids=[theirs["listing"]],
                image_ids=[theirs["image"]],
                candidate_ids=[],
                product_ids=family_ids,
            )
            == []
        )
        assert await uow.listing_issues.open_ids_for_listings([theirs["listing"]]) == []
        assert await uow.human_tasks.cancel_for_products(family_ids) == []
        assert await uow.listings.archive_for_deleted_product([theirs["listing"]], acme_owner) == []
        assert await uow.images.soft_delete_for_products(family_ids) == []
        assert await uow.image_candidates.retire_for_products(family_ids) == []
        cells = MatrixCellRepository(uow.session, acme)
        assert await cells.discontinue_for_style(theirs["style"]) == 0
        assert await uow.products.soft_delete_many([*theirs["children"], theirs["style"]]) == []
        assert await uow.products.soft_delete_many([theirs["product"]]) == []
    assert _fingerprint(admin_conn, bharat) == before


async def test_iso_delete_services_name_a_foreign_product_missing(
    world: World, admin_conn: Conn
) -> None:
    from app.core.errors import NotFoundError
    from app.services import product_delete

    theirs = _theirs(admin_conn, world)
    bharat = world.tenant("bharat")
    before = _fingerprint(admin_conn, bharat)
    acme = _principal(world, "owner@acme.test")
    for product_id in (theirs["product"], theirs["style"]):
        with pytest.raises(NotFoundError):
            await product_delete.delete_product(acme, product_id)
    items = await product_delete.bulk_delete(
        acme,
        str(uuid.uuid4()),
        "/api/v1/products/bulk-delete",
        b"{}",
        [theirs["product"], theirs["style"]],
    )
    assert [i.http_status for i in items] == [404, 404]
    assert all(i.error is not None and i.error.error_code == "NOT_FOUND" for i in items)
    assert _fingerprint(admin_conn, bharat) == before


async def test_iso_hidden_rows_stay_hidden_only_for_their_tenant(
    world: World, admin_conn: Conn
) -> None:
    """acme deletes its own product: bharat's lists are unchanged, and acme's inbox reads never
    return a bharat row either way."""
    from app.repositories.listing_issues import IssueFilter
    from app.repositories.listings import ListingFilter
    from app.repositories.uow import tenant_uow
    from app.services import product_delete

    theirs = _theirs(admin_conn, world)
    acme = world.tenant("acme")
    mine = f.product(admin_conn, acme, category="t_shirts")
    f.listing(admin_conn, acme, mine)
    before = _fingerprint(admin_conn, world.tenant("bharat"))
    await product_delete.delete_product(_principal(world, "owner@acme.test"), mine)
    assert _fingerprint(admin_conn, world.tenant("bharat")) == before
    async with tenant_uow(world.tenant("bharat")) as uow:
        listings, _ = await uow.listings.page(ListingFilter(), after=None, limit=200)
        assert theirs["listing"] in {row.id for row in listings}
        issues, _ = await uow.listing_issues.page(IssueFilter(), after=None, limit=200)
        assert theirs["issue"] in {row.id for row in issues}
        tasks, _ = await uow.human_tasks.page("open", after=None, limit=200)
        assert theirs["task"] in {row.id for row in tasks}
    async with tenant_uow(acme) as uow:
        listings, _ = await uow.listings.page(ListingFilter(), after=None, limit=200)
        assert not {row.id for row in listings} & {theirs["listing"]}
