"""One valid and one invalid request per frozen `/api/v1` operation (S3 T10 surface tests).

`valid_request(op)` passes every request-level check, so a stub answers 501. `invalid_request(op)`
breaks exactly one input, so FastAPI or a dependency answers 422.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.api.v1.examples import SUCCESS_EXAMPLES
from app.api.v1.surface import Operation

PREFIX = "/api/v1"
BEARER = {"Authorization": "Bearer test"}
ID = str(uuid.UUID("0b1c2d3e-0000-4000-8000-00000000abcd"))
VERSION_ID = str(uuid.UUID("0b1c2d3e-0000-4000-8000-00000000abce"))
ETAG = f'"{VERSION_ID}"'
IDEMPOTENCY = {"Idempotency-Key": "test-key-0001"}

_LISTING_JSON = {
    k: v for k, v in SUCCESS_EXAMPLES["listings_get"]["content"].items() if k != "listing_id"
}

_JSON_BODIES: dict[str, dict[str, Any]] = {
    "auth_login": {"email": "owner@fabrica.example", "password": "pw"},
    "auth_refresh": {"refresh_token": "rt"},
    "auth_logout": {"refresh_token": "rt"},
    "products_create": {"sku": "FAB-1", "title": "Kurta"},
    "listings_generate": {"product_id": ID, "marketplace": "amazon"},
    "listings_validate": {"listing_id": ID},
    "listings_approve": {
        "items": [{"listing_id": ID, "listing_version_id": VERSION_ID, "decision": "approve"}]
    },
    "listings_publish": {"items": [{"listing_id": ID}]},
    "listings_content_patch": {"content": {"title": "T", "bullets": ["b"], "description": "d"}},
    "listings_regenerate": {"instructions": "Shorter title."},
    "marketplaces_connect": {"marketplace": "amazon"},
    "images_upload_url": {
        "product_id": ID,
        "slot": "main",
        "content_type": "image/jpeg",
        "bytes": 1024,
    },
    "rules_dry_run": {"listing": _LISTING_JSON},
}
_FORM_OPS = {"brand_documents_upload", "products_bulk", "images_upload"}
_QUERY: dict[str, dict[str, str]] = {
    "marketplaces_callback": {"code": "auth-code", "state": "st_0123456789"},
    "analytics_get": {"from": "2026-09-01", "to": "2026-09-30"},
    "analytics_summary": {"from": "2026-09-01", "to": "2026-09-30"},
}


@dataclass
class Call:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    json: Any = None
    files: dict[str, Any] | None = None
    data: dict[str, str] | None = None

    def send(self, client: Any, *, auth: bool = True) -> Any:
        headers = {**(BEARER if auth else {}), **self.headers}
        kwargs: dict[str, Any] = {"headers": headers, "params": self.params}
        if self.json is not None:
            kwargs["json"] = self.json
        if self.files is not None:
            kwargs["files"] = self.files
        if self.data is not None:
            kwargs["data"] = self.data
        return client.request(self.method, self.url, **kwargs)


def _path(op: Operation, value: str = ID) -> str:
    path = op.path
    if op.path_id:
        path = path.replace("{" + op.path_id + "}", value)
    return PREFIX + path.replace("{marketplace}", "amazon")


def valid_request(op: Operation) -> Call:
    call = Call(op.method, _path(op), params=dict(_QUERY.get(op.op_id, {})))
    if op.idempotency:
        call.headers.update(IDEMPOTENCY)
    if op.if_match:
        call.headers["If-Match"] = ETAG
    if op.op_id in _JSON_BODIES:
        call.json = _JSON_BODIES[op.op_id]
    elif op.op_id in _FORM_OPS:
        call.files = {"file": ("upload.bin", b"col1,col2\n1,2\n", "application/octet-stream")}
        if op.op_id == "images_upload":
            call.files = {"file": ("main.jpg", b"\xff\xd8\xff\xe0", "image/jpeg")}
            call.data = {"product_id": ID, "slot": "main"}
    return call


def invalid_request(op: Operation) -> Call:
    """Break one input. Raises when an op has no input (it then declares no 422)."""
    call = valid_request(op)
    if op.path_id:
        call.url = _path(op, "not-a-uuid")
    elif op.op_id == "marketplaces_callback":
        call.params = {"code": "auth-code"}
    elif op.op_id in _QUERY:
        call.params = {**call.params, "from": "not-a-date"}
    elif op.op_id in _JSON_BODIES:
        call.json = {}
    elif op.op_id in _FORM_OPS:
        call.files = None
        call.data = {"unexpected": "x"} if op.op_id != "images_upload" else {"slot": "main"}
    elif "limit" in op.query:
        call.params = {"limit": "0"}
    else:
        raise AssertionError(f"{op.op_id}: no input to break")
    return call
