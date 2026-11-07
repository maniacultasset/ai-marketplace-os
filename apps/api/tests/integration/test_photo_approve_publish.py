"""SI3 T12 AC9 over HTTP: a listing generated with photo facts is approved by another member and
published to the fake marketplace; a seller edit after the approval voids it at publish, and a
confirm after the approval does not."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .images_support import FakeStorage
from .photo_support import (
    PHOTO_SLOTS,
    SKU,
    PhotoWorld,
    generate,
    load_platform_templates,
    seller_patch,
    vision_jpeg,
)
from .public_images_support import public_hosting
from .publish_support import PubWorld, pub, publish_one, run_job
from .review_support import REVIEWER, Made, only_item, post_approve

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub", "public_hosting"]


@pytest.fixture
async def photo_pub(
    pub: PubWorld, public_hosting: str
) -> AsyncIterator[tuple[PubWorld, PhotoWorld]]:
    """The photo world on the compose MinIO (publish reads the originals and copies the clean
    derivatives to the public bucket): every object `add_image` made, plus a clean copy."""
    from app.adapters.storage import get_storage, images_bucket
    from app.services.image_keys import derived_key

    await load_platform_templates()
    pw = PhotoWorld(pub.world, FakeStorage(), pub.world.product("acme", SKU))
    for slot in (*PHOTO_SLOTS, "packaging_label"):
        pw.images[slot] = pw.add_image(slot)
    real = get_storage()
    for key, (data, mime, _at) in pw.store.objects.items():
        await real.put(images_bucket(), key, data, mime)
    for image_id in pw.images.values():
        clean = vision_jpeg() + image_id.bytes
        key = derived_key(pw.tenant, pw.product_id, image_id, "clean.jpg")
        await real.put(images_bucket(), key, clean, "image/jpeg")
        pw.conn.execute(
            "UPDATE images SET clean_content_hash = %s WHERE id = %s",
            [hashlib.sha256(clean).hexdigest(), image_id],
        )
    yield pub, pw


async def _approved(api: httpx.AsyncClient, pw: PhotoWorld) -> Made:
    accepted = await generate(pw, "photo_ok")
    assert any(v.get("source") == "ai" for v in pw.attributes().values())
    row = pw.world.listing(accepted.listing_id)
    made = Made(accepted.listing_id, row["current_version_id"], accepted.job_id)
    item = only_item(
        await post_approve(
            api,
            pw.world,
            REVIEWER,
            [
                {
                    "listing_id": str(made.listing_id),
                    "listing_version_id": str(made.version_id),
                    "decision": "approve",
                }
            ],
        )
    )
    assert item["http_status"] == 200, item
    return made


async def _publish(api: httpx.AsyncClient, pub: PubWorld, made: Made) -> dict[str, Any]:
    item = await publish_one(api, pub, made)
    assert item["http_status"] == 202, item
    await run_job(uuid.UUID(item["job_id"]))
    return pub.job(item["job_id"])


async def test_photo_listing_approves_and_publishes(
    api: httpx.AsyncClient, photo_pub: tuple[PubWorld, PhotoWorld]
) -> None:
    pub, pw = photo_pub
    made = await _approved(api, pw)
    job = await _publish(api, pub, made)
    assert job["status"] == "succeeded", (job["status"], job["error_report_json"])
    assert job["error_code"] not in ("STALE_VERSION", "FACTS_CHANGED_SINCE_APPROVAL")


async def test_a_seller_edit_after_approval_voids_it(
    api: httpx.AsyncClient, photo_pub: tuple[PubWorld, PhotoWorld]
) -> None:
    pub, pw = photo_pub
    made = await _approved(api, pw)
    await seller_patch(pw, {"attributes": {"neckline": "V-Neck"}})
    job = await _publish(api, pub, made)
    assert (job["status"], job["error_code"]) == ("failed", "FACTS_CHANGED_SINCE_APPROVAL")


async def test_a_confirm_after_approval_keeps_it(
    api: httpx.AsyncClient, photo_pub: tuple[PubWorld, PhotoWorld]
) -> None:
    pub, pw = photo_pub
    made = await _approved(api, pw)
    await seller_patch(pw, {"confirm_attributes": ("neckline", "fit")})
    assert pw.attributes()["neckline"]["confirmed_at"] is not None
    job = await _publish(api, pub, made)
    assert job["status"] == "succeeded", (job["status"], job["error_code"])
