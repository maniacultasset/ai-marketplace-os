"""SI8 T12 (AC14): the demo kurta family (two colours x two sizes) through the API to the fake
SP-API: the owner sets the audience (PATCH), the owner generates (stub pipeline), a second member
approves, the owner publishes. The stored fake payloads carry the brand (the tenant name, no
override), the women audience, the style number as model and part number, each colour child only
its own colour's photos plus the size chart, and child titles that name the colour and size.

The family and its photos are seeded rows (no API creates a colour family or registers a photo
without the image worker; DEVIATIONS T12); every step after that is HTTP. Run in the Docker batch.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .failure_support import AmzWorld, amz
from .public_images_support import public_hosting, put_clean
from .publish_support import run_job
from .review_support import OWNER, REVIEWER, post_approve
from .test_approve_image_gate import _report, _store

pytestmark = [pytest.mark.allow_hosts(LOCAL_HOSTS), pytest.mark.usefixtures("public_hosting")]
__all__ = ["amz", "public_hosting"]
FAMILY = "KURTA-FAM-01"  # the demo seed's size_color style: navy blue, maroon x the seed sizes
STYLE_NUMBER = "KS-24"
AUDIENCE = {"target_gender": "female", "department": "Women", "age_range_description": "Adult"}


async def _run(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # RetryLater between attempts is the wrapper's signal
        assert type(exc).__name__ == "RetryLater", repr(exc)


async def _photo(
    amz: AmzWorld, style: uuid.UUID, name: str, slot: str, colourway: uuid.UUID | None
) -> uuid.UUID:
    from app.adapters.storage import get_storage, images_bucket

    acme = amz.world.tenant("acme")
    image_id = uuid.uuid4()
    data = b"\xff\xd8\xff" + f"si8-{name}".encode() * 40
    key = f"{acme}/{style}/images/{image_id}"
    f.image(
        amz.world.conn,
        acme,
        style,
        id=image_id,
        storage_key=key,
        slot=slot,
        colourway_id=colourway,
        content_hash=hashlib.sha256(data).hexdigest(),
    )
    await get_storage().put(images_bucket(), key, data, "image/jpeg")
    await _store(amz.world, "acme", image_id, _report(image_id))
    amz.world.conn.execute(  # `_store` writes a placeholder hash; publish checks the real one
        "UPDATE images SET content_hash = %s WHERE id = %s",
        [hashlib.sha256(data).hexdigest(), image_id],
    )
    await put_clean(acme, style, image_id, data, amz.world.conn)
    return image_id


async def _family(amz: AmzWorld) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    conn = amz.world.conn
    acme = amz.world.tenant("acme")
    conn.execute("UPDATE tenants SET name = 'Kuinn' WHERE id = %s", [acme])
    style = amz.world.product("acme", FAMILY)
    conn.execute(
        "UPDATE products SET gender = NULL, style_number = %s, "
        "provenance = (provenance - 'gender') || %s::jsonb WHERE id = %s",
        [STYLE_NUMBER, f'{{"style_number": {_fact()}}}', style],
    )
    colourways = dict(
        conn.execute(
            "SELECT colour_key, id FROM product_colourways WHERE style_product_id = %s",
            [style],
        ).fetchall()
    )
    photos = {
        "S": await _photo(amz, style, "S", "main", None),
        "Z": await _photo(amz, style, "Z", "size_chart", None),
        "N1": await _photo(amz, style, "N1", "main", colourways["navy blue"]),
        "N2": await _photo(amz, style, "N2", "secondary", colourways["navy blue"]),
        "M1": await _photo(amz, style, "M1", "main", colourways["maroon"]),
    }
    return style, photos


def _fact() -> str:
    import json

    return json.dumps(f.fact(value=STYLE_NUMBER))


async def test_si8_family_publishes_brand_audience_photos_and_titles(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    style, photos = await _family(amz)
    world = amz.world
    url = f"/api/v1/products/{style}"

    got = await api.get(url, headers=world.headers(OWNER))
    assert got.status_code == 200, got.text
    assert (got.json()["brand_name"], got.json()["brand_name_inherited"]) == ("Kuinn", True)
    patched = await api.patch(
        url,
        json={"gender": "women"},
        headers={**world.headers(OWNER), "If-Match": got.headers["ETag"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["gender"] == "women"

    generated = await api.post(
        "/api/v1/listings/generate",
        headers=world.headers(OWNER),
        json={"product_id": str(style), "marketplace": "amazon"},
    )
    assert generated.status_code == 202, generated.text
    await _run(generated.json()["job_id"])
    listing_id = world.listing_of(style)
    assert listing_id is not None
    version_id = world.listing(listing_id)["current_version_id"]
    approve = await post_approve(
        api,
        world,
        REVIEWER,
        [
            {
                "listing_id": str(listing_id),
                "listing_version_id": str(version_id),
                "decision": "approve",
            }
        ],
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["items"][0]["http_status"] == 200, approve.json()

    published = await api.post(
        "/api/v1/listings/publish",
        headers=world.headers(OWNER),
        json={"items": [{"listing_id": str(listing_id)}]},
    )
    assert published.status_code == 202, published.text
    (item,) = published.json()["items"]
    await _run(item["job_id"])
    job = amz.pub.job(item["job_id"])
    assert job["status"] == "succeeded", job.get("error_report_json")

    sent = {sku: v["attributes"] for (_, sku), v in amz.state.spapi_items.items()}
    assert len(sent) == 5  # the parent and four children
    for attrs in sent.values():
        assert attrs["brand"][0]["value"] == "Kuinn"
        assert {n: attrs[n][0]["value"] for n in AUDIENCE} == AUDIENCE
        assert attrs["model_number"][0]["value"] == STYLE_NUMBER
        assert attrs["part_number"][0]["value"] == STYLE_NUMBER
    parent = sent[FAMILY]
    family_title = parent["item_name"][0]["value"]  # the parent keeps the family title
    assert not any("_image_locator" in n for n in parent)
    public = {
        image: world.scalar("SELECT public_id FROM images WHERE id = %s", [image_id])
        for image, image_id in photos.items()
    }

    def shown(attrs: dict[str, Any]) -> list[str]:
        names = ["main_product_image_locator"] + [
            f"other_product_image_locator_{i}" for i in range(1, 9)
        ]
        by_public = {v: k for k, v in public.items() if v}
        out = []
        for name in names:
            if name in attrs:
                location = attrs[name][0]["media_location"]
                out.append(by_public[location.rsplit("/", 1)[-1].removesuffix(".jpg")])
        return out

    rows = world.conn.execute(
        "SELECT sku, variant_attributes FROM products WHERE parent_product_id = %s", [style]
    ).fetchall()
    for sku, axes in rows:
        attrs = sent[sku]
        colour = axes["color"]
        expected = ["N1", "N2", "Z"] if colour == "navy blue" else ["M1", "Z"]
        assert shown(attrs) == expected, (sku, shown(attrs))
        mapped = "Navy" if colour == "navy blue" else "Maroon"
        title = attrs["item_name"][0]["value"]
        assert title == f"{family_title} - {mapped}, {axes['size'].upper()}", title
