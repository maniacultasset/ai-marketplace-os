"""S26 T4 (AC 2, OWASP API3): broken object-property-level authorisation.

Mass assignment: every JSON body refuses a server-owned field (`tenant_id`, `status`, the
approver or creator, `id`) with 422 `VALIDATION_FAILED` and writes nothing, because every request
model forbids extra keys. Excessive exposure: no response schema and no read answer names a
secret-shaped key; the one exemption is the top-level `access_token` / `refresh_token` of
`TokenResponse` on login, refresh and switch-tenant.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from app.api.v1.surface import OPS, Operation
from app.core.redaction import DENY_KEYS, DENY_SUFFIXES

from .conftest import World2, call_for, ids_of, send, tenant_fingerprint, walk

MASS_FIELDS = ("tenant_id", "status", "approved_by_user_id", "created_by_user_id", "id")
JSON_OPS = [
    op
    for op in OPS.values()
    if op.has_body and op.op_id not in {"brand_documents_upload", "products_bulk", "images_upload"}
]
EXTRA_DENIED = frozenset(
    {"token_enc", "password_hash", "refresh_token_hash", "payload_storage_key"}
)
# Personal data the product shows its own users by design: a member list and "who am I".
# ADR-0019: the platform admin's sign-up queue shows the applicant's email and phone.
PERSONAL_KEYS = frozenset({"email", "phone"})
PERSONAL_OPS = frozenset(
    {
        "auth_me",
        "members_list",
        "members_patch",
        "admin_signup_requests_list",
        "admin_signup_requests_approve",
        "admin_signup_requests_reject",
    }
)
TOKEN_KEYS = frozenset({"access_token", "refresh_token"})
# The rules dry-run body carries the seam's CanonicalListing (CANON.md:122: every seam model is a
# WireModel, `extra="ignore"`, so a newer peer's keys are dropped, never stored). The dry-run
# writes nothing, and its own request model forbids extra keys at the top level.
SEAM_MODELS = frozenset({"CanonicalListing", "ImageRef", "ListingContent", "ListingKeywords"})
TOKEN_OPS = frozenset({"auth_login", "auth_refresh", "auth_switch_tenant"})


def denied(key: str) -> bool:
    name = key.lower()
    return name in DENY_KEYS or name.endswith(DENY_SUFFIXES) or name in EXTRA_DENIED


def allowed_in(op_id: str, key: str, *, top_level: bool) -> bool:
    if key in TOKEN_KEYS:
        return op_id in TOKEN_OPS and top_level
    return key in PERSONAL_KEYS and op_id in PERSONAL_OPS


@pytest.mark.parametrize("op", JSON_OPS, ids=ids_of(JSON_OPS))
async def test_api3_unknown_fields_422(
    api: httpx.AsyncClient, world2: World2, op: Operation
) -> None:
    from tests.support.rate_limits import reset_rate_limits

    conn = world2.base.conn
    acme = world2.side("acme").tenant_id
    headers = world2.caller_headers(op)
    # the body without the extra key passes validation, so the 422 below is the extra key's
    base = await send(api, call_for(op, world2, "acme"), headers)
    assert base.status_code != 422, (op.op_id, base.text[:300])
    before = tenant_fingerprint(conn, acme)
    for name in MASS_FIELDS:
        reset_rate_limits(conn)
        call = call_for(op, world2, "acme")
        call.json = {**(call.json or {}), name: str(world2.side("bharat").tenant_id)}
        response = await send(api, call, headers)
        assert response.status_code == 422, (op.op_id, name, response.text)
        assert response.json()["error"]["type"] == "VALIDATION_FAILED", (op.op_id, name)
    assert tenant_fingerprint(conn, acme) == before, op.op_id


_SPEC: dict[str, Any] = {}


def openapi_spec() -> dict[str, Any]:
    """`create_app().openapi()`, built once per worker (the env is the CI block every time)."""
    if not _SPEC:
        from app.main import create_app

        _SPEC.update(create_app().openapi())
    return _SPEC


def _schemas(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["components"]["schemas"]  # type: ignore[no-any-return]


def _refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref.rsplit("/", 1)[-1]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def _closure(spec: dict[str, Any], roots: Iterator[str]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(_refs(_schemas(spec)[name]))
    return seen


def _request_schemas(spec: dict[str, Any], content: str) -> set[str]:
    roots: list[str] = []
    for item in spec["paths"].values():
        for operation in item.values():
            body = (operation.get("requestBody") or {}).get("content", {}).get(content)
            if body:
                roots.extend(_refs(body["schema"]))
    return _closure(spec, iter(roots))


def test_api3_request_models_forbid_extra(auth_db: Any) -> None:
    """Every object schema a JSON body reaches (nested models included) is closed."""
    spec = openapi_spec()
    names = _request_schemas(spec, "application/json")
    assert names, "no JSON request bodies found"
    open_models = sorted(
        name
        for name in names
        if name not in SEAM_MODELS
        and _schemas(spec)[name].get("type") == "object"
        and _schemas(spec)[name].get("additionalProperties") is not False
        and "properties" in _schemas(spec)[name]
    )
    assert open_models == [], f"request models without extra='forbid': {open_models}"


def _response_keys(spec: dict[str, Any], op_id: str) -> Iterator[tuple[str, bool]]:
    """(property name, at the top level of the success body) for every schema `op_id`'s
    success response reaches."""
    for item in spec["paths"].values():
        for operation in item.values():
            if operation.get("operationId") != op_id:
                continue
            for status, response in operation["responses"].items():
                if not status.startswith("2"):
                    continue
                for media in (response.get("content") or {}).values():
                    top = list(_refs(media["schema"]))
                    for name in top:
                        for key in _schemas(spec)[name].get("properties", {}):
                            yield key, True
                    for name in _closure(spec, iter(top)) - set(top):
                        for key in _schemas(spec)[name].get("properties", {}):
                            yield key, False


@pytest.mark.parametrize("op", list(OPS.values()), ids=list(OPS))
def test_api3_no_response_schema_holds_denied_key(auth_db: Any, op: Operation) -> None:
    spec = openapi_spec()
    bad = sorted(
        key
        for key, top in _response_keys(spec, op.op_id)
        if denied(key) and not allowed_in(op.op_id, key, top_level=top)
    )
    assert bad == [], (op.op_id, bad)


READ_OPS = [op for op in OPS.values() if op.method == "GET" and not op.public]


@pytest.mark.parametrize("op", READ_OPS, ids=ids_of(READ_OPS))
async def test_api3_no_response_holds_denied_key(
    api: httpx.AsyncClient, world2: World2, op: Operation
) -> None:
    """The answers themselves, walked recursively (a free-form JSON column included)."""
    response = await send(api, call_for(op, world2, "acme"), world2.caller_headers(op))
    assert response.status_code < 500, (op.op_id, response.text)
    if not response.headers.get("content-type", "").startswith("application/json"):
        return
    for key, _ in walk(response.json()):
        assert not denied(key) or allowed_in(op.op_id, key, top_level=False), (op.op_id, key)


async def test_api3_token_keys_only_top_level_on_auth_login_refresh(
    api: httpx.AsyncClient, world2: World2
) -> None:
    from tests.integration.auth_support import PASSWORD

    login = await api.post(
        "/api/v1/auth/login", json={"email": "owner@acme.test", "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    refresh = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert refresh.status_code == 200, refresh.text
    for response in (login, refresh):
        body = response.json()
        assert set(body) >= TOKEN_KEYS
        nested = [key for value in body.values() for key, _ in walk(value)]
        assert not TOKEN_KEYS & set(nested), nested
    spec = openapi_spec()
    for op_id in OPS:
        keys = {key for key, _ in _response_keys(spec, op_id)}
        if op_id in TOKEN_OPS:
            assert keys >= TOKEN_KEYS, op_id
        else:
            assert not TOKEN_KEYS & keys, op_id
