"""Submission snapshots and raw marketplace bodies (S21 T8; PLAN D5; CP12; DATABASE.md:856, :866).

- `snapshot_bytes(adapter_listing, *, operation, field_paths, image_tags)`: the canonical JSON of
  what one submission sends (sorted keys, no whitespace), whose sha256 is the row's
  `payload_sha256` and feeds the marketplace idempotency key. Images appear only as
  `image:<id>@<content_hash>`, never a URL; the snapshot holds no token, no account secret and no
  idempotency key.
- `write_snapshot` / `write_raw`: one object each under the tenant prefix of the submissions
  bucket, `<tenant_id>/submissions/<submission_id>/{payload.json,raw.jsonl}`. The raw file is one
  JSON line per marketplace answer `{seller_sku, operation, body}` in call order; its sha256 is
  `raw_response_sha256`.
- `BufferedSink`: the adapter's `RawResponseSink`, buffering in memory until the call returns.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.adapters.base import AdapterListing
from app.adapters.storage import ObjectStorage, submissions_bucket

SNAPSHOT_TYPE: Final = "application/json"
RAW_TYPE: Final = "application/x-ndjson"
RAW_BODY_MAX: Final = 256 * 1024


def snapshot_key(tenant_id: uuid.UUID, submission_id: uuid.UUID) -> str:
    return f"{tenant_id}/submissions/{submission_id}/payload.json"


def raw_key(tenant_id: uuid.UUID, submission_id: uuid.UUID) -> str:
    return f"{tenant_id}/submissions/{submission_id}/raw.jsonl"


def snapshot_bytes(
    item: AdapterListing,
    *,
    operation: str,
    field_paths: Mapping[str, Sequence[str]] | None,
    image_tags: Mapping[uuid.UUID, str],
) -> bytes:
    listing = item.listing.model_dump(mode="json")
    body: dict[str, Any] = {
        "listing_id": str(item.listing_id),
        "seller_sku": item.seller_sku,
        "operation": operation,
        "field_paths": (
            None
            if field_paths is None
            else {sku: sorted(paths) for sku, paths in sorted(field_paths.items())}
        ),
        "items": [
            {
                "product_id": str(u.product_id),
                "seller_sku": u.seller_sku,
                "item_role": u.item_role,
                "variant_attributes": dict(u.variant_attributes),
            }
            for u in item.units()
        ],
        "variation_theme": item.variation_theme,
        "product_type": None if item.template is None else item.template.product_type,
        "listing": listing,
        "images": [image_tags[i] for i in sorted(image_tags, key=str)],
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


async def write_snapshot(
    storage: ObjectStorage, tenant_id: uuid.UUID, submission_id: uuid.UUID, data: bytes
) -> tuple[str, bytes]:
    key = snapshot_key(tenant_id, submission_id)
    await storage.put(submissions_bucket(), key, data, SNAPSHOT_TYPE)
    return key, hashlib.sha256(data).digest()


@dataclass
class BufferedSink:
    """The adapter's raw-body sink: kept in memory until the job writes them once."""

    entries: list[tuple[str, str, bytes]]

    def __init__(self) -> None:
        self.entries = []

    def put(self, seller_sku: str, operation: str, body: bytes) -> None:
        self.entries.append((seller_sku, operation, body[:RAW_BODY_MAX]))

    def __repr__(self) -> str:
        return f"BufferedSink({len(self.entries)} bodies)"


def raw_bytes(sink: BufferedSink) -> bytes:
    lines = [
        json.dumps(
            {"seller_sku": sku, "operation": op, "body": body.decode("utf-8", "replace")},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for sku, op, body in sink.entries
    ]
    return ("\n".join(lines) + "\n").encode() if lines else b""


async def write_raw(
    storage: ObjectStorage, tenant_id: uuid.UUID, submission_id: uuid.UUID, sink: BufferedSink
) -> tuple[str, bytes] | None:
    """The raw bodies as one object; None when the call produced no marketplace body."""
    data = raw_bytes(sink)
    if not data:
        return None
    key = raw_key(tenant_id, submission_id)
    await storage.put(submissions_bucket(), key, data, RAW_TYPE)
    return key, hashlib.sha256(data).digest()
