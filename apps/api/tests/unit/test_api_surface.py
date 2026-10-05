"""S3 T10 (AC1-AC8): the whole frozen `/api/v1` surface, parametrized over the registry."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from app.api.errors import ErrorEnvelope
from app.api.pagination import Page
from app.api.v1.surface import OPS, Operation, case_status, cases
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mkos_schemas.codes import ErrorCode
from tests.api_requests import BEARER, PREFIX, invalid_request, valid_request

OP_LIST = list(OPS.values())
IDS = [o.op_id for o in OP_LIST]
NON_PUBLIC = [o for o in OP_LIST if not o.public]
WITH_INPUT = [o for o in OP_LIST if o.has_input]
LIST_OPS = [
    o for o in OP_LIST if o.response_model is not None and issubclass(o.response_model, Page)
]
UNVERSIONED_ALLOWED = frozenset(
    {"/healthz", "/readyz", "/openapi.json", "/docs", "/internal/admin"}
)
CANARY = "CANARYpw0123456789zz"


@pytest.fixture(scope="module")
def client(v1_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(v1_app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def spec(v1_app: FastAPI) -> dict[str, Any]:
    return v1_app.openapi()


def _operation(spec: dict[str, Any], op: Operation) -> dict[str, Any]:
    result: dict[str, Any] = spec["paths"][PREFIX + op.path][op.method.lower()]
    return result


def _resolve(spec: dict[str, Any], node: Any) -> Any:
    if isinstance(node, dict) and "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return spec["components"]["schemas"][name]
    return node


def _envelope(response: Any, status: int, code: ErrorCode | None = None) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    ErrorEnvelope.model_validate(body)
    error = body["error"]
    if code is not None:
        assert error["type"] == code.value
        assert error["error_report"]["error_code"] == code.value
    assert error["correlation_id"]
    for item in error["error_report"]["errors"]:
        assert item["source"] == "system"
        assert item["field"] == "request"
    return body


def test_openapi_paths_frozen(spec: dict[str, Any]) -> None:
    declared: set[tuple[str, str]] = set()
    unversioned: set[str] = set()
    for path, item in spec["paths"].items():
        if path.startswith(PREFIX + "/"):
            declared |= {(method.upper(), path) for method in item}
        else:
            unversioned.add(path)
    assert declared == {(op.method, PREFIX + op.path) for op in OP_LIST}
    assert unversioned <= UNVERSIONED_ALLOWED
    assert unversioned == {"/healthz", "/readyz"}


@pytest.mark.parametrize("op", OP_LIST, ids=IDS)
def test_stub_returns_501_envelope(client: TestClient, op: Operation) -> None:
    response = valid_request(op).send(client)
    body = _envelope(response, 501, ErrorCode.NOT_IMPLEMENTED)
    assert op.op_id in body["error"]["message"]
    assert response.headers["x-request-id"] == body["error"]["correlation_id"]


@pytest.mark.parametrize("op", WITH_INPUT, ids=[o.op_id for o in WITH_INPUT])
def test_error_envelope_validation(client: TestClient, op: Operation) -> None:
    response = invalid_request(op).send(client)
    body = _envelope(response, 422, ErrorCode.VALIDATION_FAILED)
    assert body["error"]["details"]
    for detail in body["error"]["details"]:
        assert set(detail) == {"loc", "msg"}


@pytest.mark.parametrize("op", NON_PUBLIC, ids=[o.op_id for o in NON_PUBLIC])
def test_missing_auth_401(client: TestClient, op: Operation) -> None:
    call = valid_request(op)
    no_auth = call.send(client, auth=False)
    _envelope(no_auth, 401, ErrorCode.UNAUTHENTICATED)
    assert no_auth.headers["www-authenticate"] == "Bearer"
    if call.json is not None:
        # Well-formed but schema-invalid JSON: auth still answers first, and no detail leaks.
        call.json = {"unexpected": CANARY}
        invalid = call.send(client, auth=False)
        body = _envelope(invalid, 401, ErrorCode.UNAUTHENTICATED)
        assert body["error"]["details"] is None
        assert CANARY not in invalid.text
    if op.path_id:
        call = invalid_request(op)
        body = _envelope(call.send(client, auth=False), 401, ErrorCode.UNAUTHENTICATED)
        assert body["error"]["details"] is None


def test_malformed_json_422_before_auth(client: TestClient) -> None:
    # FastAPI parses the body before it solves dependencies (fastapi/routing.py), so a malformed
    # JSON body is 422 even without a token. Nothing from it is echoed.
    response = client.post(
        f"{PREFIX}/products",
        content='{"sku": "' + CANARY,
        headers={"content-type": "application/json"},
    )
    _envelope(response, 422, ErrorCode.VALIDATION_FAILED)
    assert CANARY not in response.text


@pytest.mark.parametrize("op", OP_LIST, ids=IDS)
def test_declared_error_codes_exist(spec: dict[str, Any], op: Operation) -> None:
    responses = _operation(spec, op)["responses"]
    for status, response in responses.items():
        if status == "default" or int(status) < 400:
            continue
        assert int(status) in op.error_codes, (op.op_id, status)
        codes = op.error_codes[int(status)]
        assert all(isinstance(c, ErrorCode) for c in codes)
        example = response["content"]["application/json"]["example"]
        assert example["error"]["type"] in {c.value for c in codes}
        ErrorCode(example["error"]["type"])
    assert {int(s) for s in responses if s != "default" and int(s) >= 400} == set(op.error_codes)


def test_callback_declares_303(spec: dict[str, Any]) -> None:
    op = OPS["marketplaces_callback"]
    responses = _operation(spec, op)["responses"]
    success = {s for s in responses if s != "default" and int(s) < 400}
    assert success == {"303"}
    assert "Location" in responses["303"]["headers"]
    assert "content" not in responses["303"]
    assert "OAUTH_STATE_INVALID" in responses["409"]["description"]
    assert responses["409"]["content"]["application/json"]["example"]["error"]["type"] == (
        "OAUTH_STATE_INVALID"
    )


def test_error_envelope_404_405_500(v1_app: FastAPI, client: TestClient) -> None:
    _envelope(client.get(f"{PREFIX}/x", headers=BEARER), 404, ErrorCode.NOT_FOUND)
    not_allowed = client.delete(f"{PREFIX}/products", headers=BEARER)
    _envelope(not_allowed, 405, ErrorCode.METHOD_NOT_ALLOWED)
    assert set(not_allowed.headers["allow"].replace(" ", "").split(",")) == {"GET", "POST"}

    app = FastAPI()
    from app.api.errors import install_error_handlers

    install_error_handlers(app)

    @app.get("/api/v1/boom")
    async def boom() -> None:
        raise RuntimeError(f"internal {CANARY}")

    with TestClient(app, raise_server_exceptions=False) as raw:
        response = raw.get("/api/v1/boom")
    _envelope(response, 500, ErrorCode.INTERNAL_ERROR)
    assert "Traceback" not in response.text
    assert CANARY not in response.text


@pytest.mark.parametrize("op", OP_LIST, ids=IDS)
def test_openapi_declares_case_matrix(spec: dict[str, Any], op: Operation) -> None:
    operation = _operation(spec, op)
    responses = operation["responses"]
    assert "default" in responses
    for case in cases(op):
        status = case_status(op, case)
        assert str(status) in responses, (op.op_id, case)
        if status >= 400:
            schema = responses[str(status)]["content"]["application/json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/ErrorEnvelope"}, (op.op_id, case)
    assert responses["429"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorEnvelope"
    )
    if op.has_body:
        assert "413" in responses
    if op.public:
        assert "security" not in operation
    else:
        assert operation["security"] == [{"HTTPBearer": []}]
    params = {(p["in"], p["name"]): p for p in operation.get("parameters", [])}
    idem = params.get(("header", "Idempotency-Key"))
    assert (idem is not None) is op.idempotency, op.op_id
    if idem is not None:
        assert idem["required"] is True
    if_match = params.get(("header", "If-Match"))
    assert (if_match is not None) is op.if_match, op.op_id
    if if_match is not None:
        assert if_match["required"] is True
    if op.response_model is not None and op.status < 300:
        success = responses[str(op.status)]["content"]["application/json"]
        assert success["schema"] == {"$ref": f"#/components/schemas/{op.response_model.__name__}"}
        assert "example" in success


def test_validate_contract_200(spec: dict[str, Any]) -> None:
    op = OPS["listings_validate"]
    responses = _operation(spec, op)["responses"]
    ok = responses["200"]["content"]["application/json"]
    assert ok["schema"] == {"$ref": "#/components/schemas/ValidationSummary"}
    assert ok["example"]["passed"] is False
    assert responses["422"]["content"]["application/json"]["example"]["error"]["type"] == (
        "VALIDATION_FAILED"
    )
    assert "Request validation failed." in responses["422"]["description"]


@pytest.mark.parametrize("op", LIST_OPS, ids=[o.op_id for o in LIST_OPS])
def test_list_envelope_shape(spec: dict[str, Any], op: Operation) -> None:
    operation = _operation(spec, op)
    schema = _resolve(spec, operation["responses"]["200"]["content"]["application/json"]["schema"])
    assert set(schema["properties"]) == {"data", "page"}
    page = _resolve(spec, schema["properties"]["page"])
    assert set(page["properties"]) == {"next_cursor", "has_more", "limit"}
    params = {p["name"]: p for p in operation["parameters"] if p["in"] == "query"}
    assert params["limit"]["schema"]["maximum"] == 200
    assert params["limit"]["schema"]["minimum"] == 1
    assert params["limit"]["schema"]["default"] == 50
    assert "cursor" in params


def test_list_ops_are_the_page_models() -> None:
    assert {o.op_id for o in LIST_OPS} == {
        "products_list",
        "imports_rows",
        "listings_list",
        "marketplaces_accounts",
        "rules_list",
    }


def test_no_offset_params(spec: dict[str, Any]) -> None:
    banned = {"offset", "page", "page_size", "skip", "per_page", "page_number"}
    for item in spec["paths"].values():
        for operation in item.values():
            names = {p["name"].lower() for p in operation.get("parameters", [])}
            assert not names & banned


def _walk_props(spec: dict[str, Any], node: Any, seen: set[str]) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                return names
            seen.add(ref)
            return _walk_props(spec, _resolve(spec, node), seen)
        if isinstance(node.get("properties"), dict):
            names |= set(node["properties"])
        for value in node.values():
            names |= _walk_props(spec, value, seen)
    elif isinstance(node, list):
        for value in node:
            names |= _walk_props(spec, value, seen)
    return names


def test_no_tenant_id_in_requests(spec: dict[str, Any]) -> None:
    for item in spec["paths"].values():
        for operation in item.values():
            for param in operation.get("parameters", []):
                assert param["name"] != "tenant_id"
            body = operation.get("requestBody")
            if body:
                assert "tenant_id" not in _walk_props(spec, body, set())


def test_no_dead_letter_paths(client: TestClient, spec: dict[str, Any]) -> None:
    assert not [p for p in spec["paths"] if "dead-letter" in p]
    response = client.get(f"{PREFIX}/jobs/dead-letter", headers=BEARER)
    _envelope(response, 422, ErrorCode.VALIDATION_FAILED)
    requeue = client.post(f"{PREFIX}/jobs/dead-letter/{'0' * 8}/requeue", headers=BEARER)
    assert requeue.status_code in {404, 405}
    assert requeue.status_code != 501


def test_no_trailing_slash_redirect(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/products/", headers=BEARER, follow_redirects=False)
    _envelope(response, 404, ErrorCode.NOT_FOUND)


def test_validation_never_echoes_input(client: TestClient) -> None:
    bad = client.post(f"{PREFIX}/auth/login", json={"email": "nope", "password": CANARY})
    _envelope(bad, 422, ErrorCode.VALIDATION_FAILED)
    assert CANARY not in bad.text
    too_long = client.post(
        f"{PREFIX}/auth/login", json={"email": "a@b.co", "password": CANARY * 60}
    )
    _envelope(too_long, 422, ErrorCode.VALIDATION_FAILED)
    assert CANARY not in too_long.text
    ok = client.post(f"{PREFIX}/auth/login", json={"email": "a@b.co", "password": CANARY})
    _envelope(ok, 501, ErrorCode.NOT_IMPLEMENTED)
    assert CANARY not in ok.text


def test_callback_public_and_marketplace_enum(client: TestClient) -> None:
    params = {"code": "c", "state": "s"}
    ebay = client.get(f"{PREFIX}/marketplaces/ebay/callback", params=params)
    _envelope(ebay, 422, ErrorCode.VALIDATION_FAILED)
    amazon = client.get(f"{PREFIX}/marketplaces/amazon/callback", params=params)
    _envelope(amazon, 501, ErrorCode.NOT_IMPLEMENTED)
    oversized = client.get(
        f"{PREFIX}/marketplaces/amazon/callback", params={"code": "c" * 2049, "state": "s"}
    )
    _envelope(oversized, 422, ErrorCode.VALIDATION_FAILED)


def test_images_upload_needs_exactly_one_source(client: TestClient) -> None:
    op = OPS["images_upload"]
    both = valid_request(op)
    assert both.data is not None
    both.data = {**both.data, "storage_key": "t/x/img/1.jpg"}
    _envelope(both.send(client), 422, ErrorCode.VALIDATION_FAILED)
    neither = valid_request(op)
    neither.files = None
    _envelope(neither.send(client), 422, ErrorCode.VALIDATION_FAILED)
    key_only = valid_request(op)
    key_only.files = None
    assert key_only.data is not None
    key_only.data = {**key_only.data, "storage_key": "t/x/img/1.jpg"}
    _envelope(key_only.send(client), 501, ErrorCode.NOT_IMPLEMENTED)
    traversal = valid_request(op)
    traversal.files = None
    assert traversal.data is not None
    traversal.data = {**traversal.data, "storage_key": "t/../other/1.jpg"}
    _envelope(traversal.send(client), 422, ErrorCode.VALIDATION_FAILED)


def test_analytics_range_checked(client: TestClient) -> None:
    for path in ("/analytics", "/analytics/summary"):
        response = client.get(
            PREFIX + path, params={"from": "2026-10-02", "to": "2026-10-01"}, headers=BEARER
        )
        _envelope(response, 422, ErrorCode.VALIDATION_FAILED)
