"""Shared helpers for the S17b image suites: the job world (`acme`, `bharat`, one member per
role, an empty queue) plus products and image rows built by the admin login, a fake object
store, the image rules seed and generated image bytes. No committed binary."""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from PIL import Image as PILImage

from . import factories as f
from .jobs_support import Conn, JobWorld, as_member, reset_queue

PREFIX = "/api/v1"
UPLOAD_URL = f"{PREFIX}/images/upload-url"
UPLOAD = f"{PREFIX}/images/upload"


def report_url(image_id: Any) -> str:
    return f"{PREFIX}/images/{image_id}/report"


@pytest.fixture
async def images_db(auth_db: Conn) -> AsyncIterator[JobWorld]:
    """The job world with the platform image rules seeded and the storage/queue caches cold."""
    from app.adapters.storage import get_storage
    from app.db.queue import get_queue_app
    from tests.support.rate_limits import reset_rate_limits

    from .auth_support import build_world

    reset_queue(auth_db)
    seed_image_rules(auth_db)
    get_queue_app.cache_clear()
    get_storage.cache_clear()
    world = JobWorld(auth_db, build_world(auth_db))
    reset_rate_limits(auth_db)
    yield world
    get_queue_app.cache_clear()
    get_storage.cache_clear()


def seed_image_rules(conn: Conn) -> None:
    """The platform `images` rows of `rules.seed.json` (the S17b T5 rows), as the seed writes
    them: other rules are not needed by the image job."""
    from app.cli import RULES_SEED_PATH
    from app.db.seed_ops import _platform_rule_rows, load_rule_sets

    rows = [
        r for r in _platform_rule_rows(load_rule_sets(RULES_SEED_PATH)) if r["field"] == "images"
    ]
    for row in rows:
        f.insert(conn, "rules", **row)


def product(conn: Conn, tenant_id: uuid.UUID, kind: str = "standalone", **cols: Any) -> uuid.UUID:
    return f.product(conn, tenant_id, kind, category=cols.pop("category", "kurtas"), **cols)


def image_row(
    conn: Conn,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    slot: str = "secondary",
    image_id: uuid.UUID | None = None,
    key: str | None = None,
    **cols: Any,
) -> uuid.UUID:
    """An image row on a D1 key, written by the admin login."""
    from app.services.image_keys import image_object_key

    image_id = image_id or uuid.uuid4()
    return f.image(
        conn,
        tenant_id,
        product_id,
        id=image_id,
        slot=slot,
        storage_key=key or image_object_key(tenant_id, product_id, image_id),
        **cols,
    )


def picture(
    w: int, h: int, rgb: tuple[int, int, int] = (255, 255, 255), fmt: str = "JPEG"
) -> bytes:
    """A flat image with a centred darker square (a 'product'), encoded as `fmt`."""
    img = PILImage.new("RGB", (w, h), rgb)
    side = max(1, min(w, h) // 2)
    box = ((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2)
    img.paste((40, 60, 90), box)
    out = io.BytesIO()
    img.save(out, format=fmt, **({"quality": 90} if fmt == "JPEG" else {}))
    return out.getvalue()


@dataclass
class FakeStorage:
    """An in-memory `ObjectStorage` stand-in (the methods the image pipeline calls)."""

    objects: dict[str, tuple[bytes, str, datetime]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail: Mapping[str, Exception] = field(default_factory=dict)

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self._call("put")
        self.objects[key] = (data, content_type, datetime.now(UTC))

    async def get(self, bucket: str, key: str, *, max_bytes: int | None = None) -> bytes:
        from app.adapters.storage import StorageNotFound, StorageObjectTooLarge

        self._call("get")
        if key not in self.objects:
            raise StorageNotFound("the object does not exist")
        data = self.objects[key][0]
        if max_bytes is not None and len(data) > max_bytes:
            raise StorageObjectTooLarge("over")
        return data

    async def head(self, bucket: str, key: str) -> Any:
        from app.adapters.storage import ObjectHead

        self._call("head")
        if key not in self.objects:
            return None
        data, ctype, when = self.objects[key]
        return ObjectHead(size=len(data), content_type=ctype, last_modified=when)

    async def presign_put(self, bucket: str, key: str, **kw: Any) -> str:
        self._call("presign_put")
        return f"http://127.0.0.1:1/{bucket}/{key}?X-Amz-Signature=fake"


def headers(
    jw: JobWorld, email: str = "operator@acme.test", key: str | None = None
) -> dict[str, str]:
    return {**as_member(jw, email), "Idempotency-Key": key or str(uuid.uuid4())}


async def upload_file(
    api: httpx.AsyncClient,
    hdrs: Mapping[str, str],
    product_id: Any,
    data: bytes,
    slot: str = "main",
    **form: Any,
) -> httpx.Response:
    fields = {"product_id": str(product_id), "slot": slot, **{k: str(v) for k, v in form.items()}}
    return await api.post(
        UPLOAD, headers=dict(hdrs), data=fields, files={"file": ("x.jpg", data, "image/jpeg")}
    )


async def register_key(
    api: httpx.AsyncClient,
    hdrs: Mapping[str, str],
    product_id: Any,
    key: str,
    slot: str = "main",
    **form: Any,
) -> httpx.Response:
    fields = {
        "product_id": str(product_id),
        "slot": slot,
        "storage_key": key,
        **{k: str(v) for k, v in form.items()},
    }
    return await api.post(UPLOAD, headers=dict(hdrs), data=fields)


def image_db_row(conn: Conn, image_id: Any) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM images WHERE id = %s", [image_id])
    names = [d.name for d in cur.description or ()]
    found = cur.fetchone()
    assert found is not None, f"no images row {image_id}"
    return dict(zip(names, found, strict=True))
