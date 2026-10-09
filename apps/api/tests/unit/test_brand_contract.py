"""S16 T1 (AC0; PLAN D2, D3; CP3-CP5): the profile PATCH body, the document models and the
three new operations."""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.api.v1.schemas.brand import BrandDocumentOut, UploadChunkType
from app.api.v1.schemas.tenants import TenantPatch
from app.api.v1.surface import OPS
from pydantic import TypeAdapter, ValidationError
from tests.conftest import REPO_ROOT

OPENAPI = REPO_ROOT / "packages/schemas/jsonschema/openapi.json"


def _ok(body: dict[str, Any]) -> TenantPatch:
    return TenantPatch.model_validate(body)


def _bad(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TenantPatch.model_validate(body)


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "x" * 201},
        {"name": "   "},
        {"tone": "x" * 2001},
        {"tone": ""},
        {"guidelines": ["g"] * 101},
        {"guidelines": ["x" * 501]},
        {"guidelines": [" "]},
        {"glossary": {f"t{i}": "p" for i in range(501)}},
        {"glossary": {"x" * 101: "p"}},
        {"glossary": {"t": "x" * 101}},
        {"glossary": {" ": "p"}},
        {"glossary": {"t": " "}},
        {"name": "a\x00b"},
        {"tone": "a\x00b"},
        {"guidelines": ["a\x00b"]},
        {"glossary": {"a\x00b": "p"}},
        {"glossary": {"t": "a\x00b"}},
    ],
)
def test_tenant_patch_bounds(body: dict[str, Any]) -> None:
    _bad(body)


def test_tenant_patch_bounds_edges_ok() -> None:
    _ok({"name": "x"})
    _ok({"name": "x" * 200})
    _ok({"tone": "x" * 2000})
    _ok({"guidelines": ["x" * 500] * 100})
    _ok({"glossary": {f"t{i}": "p" * 100 for i in range(500)}})
    _ok({"guidelines": [], "glossary": {}})


def test_tenant_patch_empty_rejected() -> None:
    _bad({})


def test_tenant_patch_null_rules() -> None:
    patch = _ok({"tone": None})
    assert patch.tone is None and patch.model_fields_set == {"tone"}
    for key in ("name", "guidelines", "glossary"):
        _bad({key: None})


@pytest.mark.parametrize("key", ["tenant_id", "status", "trial_ends_at", "slug", "id"])
def test_tenant_patch_extra_tenant_id_rejected(key: str) -> None:
    _bad({"name": "Acme", key: "x"})


def test_brand_document_out_has_no_storage_key_or_hash() -> None:
    fields = set(BrandDocumentOut.model_fields)
    assert fields == {"id", "filename", "status", "chunk_count", "error", "created_at"}
    assert not fields & {"storage_key", "content_hash", "bytes", "job_id", "deleted_at"}


@pytest.mark.parametrize("value", ["example", "bogus", ""])
def test_upload_type_vocab(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(UploadChunkType).validate_python(value)


def test_upload_type_vocab_ok() -> None:
    for value in ("tone_rule", "glossary", "compliance"):
        assert TypeAdapter(UploadChunkType).validate_python(value) == value


def test_s16_operations() -> None:
    update = OPS["tenants_me_update"]
    assert (update.method, update.path, update.status) == ("PATCH", "/tenants/me", 200)
    assert update.if_match and update.etag and set(update.roles or ()) == {"owner", "admin"}
    listing = OPS["brand_documents_list"]
    assert set(listing.roles or ()) == {"owner", "admin", "operator", "reviewer"}
    delete = OPS["brand_documents_delete"]
    assert (delete.method, delete.status, delete.path_id) == ("DELETE", 204, "document_id")
    assert set(delete.roles or ()) == {"owner", "admin"}
    assert set(OPS["brand_documents_upload"].roles or ()) == {"owner", "admin"}


def test_tenants_me_declares_etag_header() -> None:
    spec = json.loads(OPENAPI.read_text(encoding="utf-8"))
    for method in ("get", "patch"):
        headers = spec["paths"]["/api/v1/tenants/me"][method]["responses"]["200"]["headers"]
        assert "ETag" in headers
    params = spec["paths"]["/api/v1/tenants/me"]["patch"]["parameters"]
    assert any(p["name"] == "If-Match" and p["required"] for p in params)


def test_upload_form_has_optional_type() -> None:
    spec = json.loads(OPENAPI.read_text(encoding="utf-8"))
    body = spec["paths"]["/api/v1/brand/documents"]["post"]["requestBody"]
    ref = body["content"]["multipart/form-data"]["schema"]["$ref"].rsplit("/", 1)[-1]
    form = spec["components"]["schemas"][ref]
    assert form["required"] == ["file"]
    assert form["properties"]["type"]["enum"] == ["tone_rule", "glossary", "compliance"]
