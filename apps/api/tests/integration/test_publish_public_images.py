"""SI1 T19 (AC12-AC14; ADR-0026; CP1, CP7, CP16): publish hands the adapter stable public URLs
of EXIF-free copies in a public bucket, on the compose MinIO. Run in the Docker batch
(`make api-tests-integration`)."""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .public_images_support import PUBLIC_BUCKET, public_hosting
from .publish_support import PubWorld, pub, publish_and_run, publish_one, run_job
from .test_publish_attacks import _approved_with_image, _run_quiet

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub", "public_hosting"]
JPEG = b"\xff\xd8\xff" + b"si1-public" * 40
PUBLIC_ID = re.compile(r"^[a-z2-7]{26}$")


def _spy(pub: PubWorld) -> list[Any]:
    seen: list[Any] = []
    original = pub.fake.create_listing

    async def spy(item: Any) -> Any:
        seen.append(item)
        return await original(item)

    pub.fake.create_listing = spy
    return seen


def _row(pub: PubWorld, image: uuid.UUID) -> tuple[Any, Any]:
    found = pub.conn.execute(
        "SELECT public_id, public_written_at FROM images WHERE id = %s", [image]
    ).fetchone()
    assert found is not None
    return found[0], found[1]


async def test_public_url_served_anonymously(
    api: httpx.AsyncClient, pub: PubWorld, public_hosting: str
) -> None:
    made, image, key = await _approved_with_image(pub, JPEG)
    seen = _spy(pub)
    await publish_and_run(api, pub, made)
    assert pub.listing(made.listing_id)["status"] == "published"
    url = seen[0].image_refs[image]
    public_id, written = _row(pub, image)
    assert PUBLIC_ID.fullmatch(public_id) and written is not None
    assert url == f"{public_hosting}/{public_id}.jpg"
    async with httpx.AsyncClient() as anonymous:
        got = await anonymous.get(url)
        assert got.status_code == 200 and got.content == JPEG
        assert got.headers.get("cache-control") == "public, max-age=86400"  # V1 hardening #17
        private = await anonymous.get(
            public_hosting.replace(PUBLIC_BUCKET, "marketplace-images") + "/" + key
        )
        assert private.status_code == 403
        listing = await anonymous.get(public_hosting + "/")
        assert listing.status_code == 403
    for value in (str(image), str(made.listing_id), str(pub.world.tenant("acme"))):
        assert value.replace("-", "") not in public_id


async def test_republish_reuses_the_url_without_a_put(
    api: httpx.AsyncClient, pub: PubWorld, public_hosting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import public_images

    made, image, _key = await _approved_with_image(pub, JPEG)
    await publish_and_run(api, pub, made)
    first, _ = _row(pub, image)
    puts: list[str] = []
    from app.adapters.storage import get_storage as real

    class Counting:
        def __init__(self) -> None:
            self.inner = real()

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        async def put(self, *args: Any, **kwargs: Any) -> None:
            puts.append(args[1])
            await self.inner.put(*args, **kwargs)

    monkeypatch.setattr(public_images, "get_storage", Counting)
    from app.services.public_images import PublicImage, ensure_public

    url = await ensure_public(
        pub.world.tenant("acme"),
        PublicImage(image, 0, _key, *_clean(pub, image), first, _row(pub, image)[1]),
    )
    assert url.endswith(f"/{first}.jpg") and puts == []


def _clean(pub: PubWorld, image: uuid.UUID) -> tuple[str, str]:
    found = pub.conn.execute(
        "SELECT clean_content_hash, clean_mime_type FROM images WHERE id = %s", [image]
    ).fetchone()
    assert found is not None
    return found[0], found[1]


async def test_put_failure_retries_and_sends_nothing(
    api: httpx.AsyncClient, pub: PubWorld, public_hosting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters.storage import ObjectStorage, StorageUnavailable

    made, image, _key = await _approved_with_image(pub, JPEG)
    item = await publish_one(api, pub, made)
    real_put = ObjectStorage.put

    async def flaky(self: Any, bucket: str, key: str, *args: Any, **kwargs: Any) -> None:
        if bucket == PUBLIC_BUCKET:
            raise StorageUnavailable("down")
        await real_put(self, bucket, key, *args, **kwargs)

    monkeypatch.setattr(ObjectStorage, "put", flaky)
    await _run_quiet(item["job_id"])
    assert pub.fake.calls == [] and pub.submissions(made.listing_id) == []
    public_id, written = _row(pub, image)
    assert PUBLIC_ID.fullmatch(public_id) and written is None  # named, not written yet
    monkeypatch.setattr(ObjectStorage, "put", real_put)
    await _run_quiet(item["job_id"])
    assert _row(pub, image)[0] == public_id  # the retry re-PUTs the same name


async def test_unset_hosting_with_images_fails_closed(
    api: httpx.AsyncClient, pub: PubWorld, public_hosting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    made, image, _key = await _approved_with_image(pub, JPEG)
    monkeypatch.delenv("IMAGE_PUBLIC_BASE_URL")
    monkeypatch.delenv("GCS_BUCKET_IMAGES_PUBLIC")
    get_settings.cache_clear()
    item = await publish_one(api, pub, made)
    await _run_quiet(item["job_id"])
    job = pub.job(item["job_id"])
    assert (job["status"], job["error_code"]) == ("failed", "INTERNAL_ERROR")
    assert pub.fake.calls == [] and _row(pub, image) == (None, None)


async def test_pin_failure_leaves_no_public_id(
    api: httpx.AsyncClient, pub: PubWorld, public_hosting: str
) -> None:
    """Nothing is made public before the pins pass."""
    made, image, _key = await _approved_with_image(pub, JPEG)
    item = await publish_one(api, pub, made)
    pub.conn.execute("UPDATE images SET content_hash = NULL WHERE id = %s", [image])
    await _run_quiet(item["job_id"])
    assert _row(pub, image) == (None, None)


async def test_clean_hash_mismatch_refused(
    api: httpx.AsyncClient, pub: PubWorld, public_hosting: str
) -> None:
    made, image, _key = await _approved_with_image(pub, JPEG)
    item = await publish_one(api, pub, made)
    pub.conn.execute("UPDATE images SET clean_content_hash = %s WHERE id = %s", ["b" * 64, image])
    await _run_quiet(item["job_id"])
    job = pub.job(item["job_id"])
    assert (job["status"], job["error_code"]) == ("failed", "IMAGE_NON_COMPLIANT")
    assert pub.fake.calls == []


async def test_delete_between_set_public_id_and_put(
    api: httpx.AsyncClient, pub: PubWorld, public_hosting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CP16: the row is deleted after it was named and before the PUT lands."""
    from app.adapters.storage import ObjectStorage

    made, image, _key = await _approved_with_image(pub, JPEG)
    item = await publish_one(api, pub, made)
    real_head = ObjectStorage.head

    async def head_then_delete(self: Any, bucket: str, key: str) -> Any:
        if bucket == PUBLIC_BUCKET:
            pub.conn.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [image])
        return await real_head(self, bucket, key)

    monkeypatch.setattr(ObjectStorage, "head", head_then_delete)
    await _run_quiet(item["job_id"])
    public_id, written = _row(pub, image)
    assert PUBLIC_ID.fullmatch(public_id) and written is None  # the tombstone, no copy
    monkeypatch.setattr(ObjectStorage, "head", real_head)
    async with httpx.AsyncClient() as anonymous:
        assert (await anonymous.get(f"{public_hosting}/{public_id}.jpg")).status_code in (403, 404)
    assert pub.fake.calls == []


async def test_image_outside_the_version_gets_no_public_id(
    api: httpx.AsyncClient, pub: PubWorld, public_hosting: str
) -> None:
    """AC14: an image uploaded after the approval is not in the version and stays private."""
    from . import factories as f

    made, image, _key = await _approved_with_image(pub, JPEG)
    product_id = pub.conn.execute("SELECT product_id FROM images WHERE id = %s", [image]).fetchone()
    assert product_id is not None
    late = f.image(pub.conn, pub.world.tenant("acme"), product_id[0], slot="secondary")
    label = f.image(pub.conn, pub.world.tenant("acme"), product_id[0], slot="packaging_label")
    await publish_and_run(api, pub, made)
    assert _row(pub, late) == (None, None) and _row(pub, label) == (None, None)
    assert run_job is not None
