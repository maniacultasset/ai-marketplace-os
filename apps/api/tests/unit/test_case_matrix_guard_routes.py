"""S3 T15 (AC9), tester round 3: route shapes the R2-1 guard fix does catch. Each app answers an
/api/v1 URL from a route outside the registry, and the guard must flag it."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Host, Route
from tests.endpoint_cases import guard_failures
from tests.unit.test_case_matrix_guard import _app_with, scan_dir_empty


async def _leak() -> dict[str, str]:
    return {"rows": "every tenant's products"}


async def _raw_leak(request: Request) -> JSONResponse:
    return JSONResponse({"rows": "every tenant's products"})


def test_guard_flags_a_raw_starlette_route() -> None:
    app = _app_with(fn_is_stub=True)
    app.router.routes.append(Route("/api/v1/raw", _raw_leak))
    assert TestClient(app).get("/api/v1/raw").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET,HEAD /api/v1/raw: route is not in the registry"
    ]


def test_guard_flags_a_templated_raw_starlette_route() -> None:
    app = _app_with(fn_is_stub=True)
    app.router.routes.append(Route("/api/{v}/raw", _raw_leak))
    assert TestClient(app).get("/api/v1/raw").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET,HEAD /api/{v}/raw: route is not in the registry"
    ]


def test_guard_flags_a_route_behind_a_host() -> None:
    app = _app_with(fn_is_stub=True)
    sub = FastAPI()
    sub.add_api_route("/api/{v}/products/export", _leak, methods=["GET"])
    app.router.routes.append(Host("{sub}.example.com", sub))
    client = TestClient(app, base_url="http://a.example.com")
    assert client.get("/api/v1/products/export").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /api/{v}/products/export: route is not in the registry"
    ]


def test_guard_flags_a_route_in_a_root_mount() -> None:
    app = _app_with(fn_is_stub=True)
    sub = FastAPI()
    sub.add_api_route("/api/v1/zz", _leak, methods=["GET"])
    app.mount("", sub)
    assert TestClient(app).get("/api/v1/zz").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == ["GET /api/v1/zz: route is not in the registry"]


def test_guard_flags_a_templated_non_get_route() -> None:
    app = _app_with(fn_is_stub=True)
    app.add_api_route("/api/{x}/zz", _leak, methods=["DELETE"], include_in_schema=False)
    assert TestClient(app).delete("/api/v1/zz").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "DELETE /api/{x}/zz: route is not in the registry"
    ]


def test_guard_flags_an_int_converter_version() -> None:
    app = _app_with(fn_is_stub=True)
    app.add_api_route("/api/v{n:int}/zz", _leak, methods=["GET"], include_in_schema=False)
    assert TestClient(app).get("/api/v1/zz").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /api/v{n:int}/zz: route is not in the registry"
    ]


# R3-1: templated and nested mounts, and a parameter glued to a literal. The guard walks by real
# dispatch (route.matches and the mount's child scope), not by the declared mount prefix.


def _bare_sub(path: str) -> FastAPI:
    sub = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sub.add_api_route(path, _leak, methods=["GET"])
    return sub


def test_guard_flags_a_route_in_a_templated_mount() -> None:
    app = _app_with(fn_is_stub=True)
    app.mount("/api/{ver}", _bare_sub("/products/export"))
    assert TestClient(app).get("/api/v1/products/export").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /api/{ver}/products/export: route is not in the registry"
    ]


def test_guard_flags_a_route_in_a_nested_templated_mount() -> None:
    app = _app_with(fn_is_stub=True)
    mid = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    mid.mount("/{ver}", _bare_sub("/products/export"))
    app.mount("/api", mid)
    assert TestClient(app).get("/api/v1/products/export").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /api/{ver}/products/export: route is not in the registry"
    ]


def test_guard_flags_a_route_in_a_nested_mount_with_a_templated_middle() -> None:
    app = _app_with(fn_is_stub=True)
    mid = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    mid.mount("/{ver}/products", _bare_sub("/export"))
    app.mount("/api", mid)
    assert TestClient(app).get("/api/v1/products/export").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /api/{ver}/products/export: route is not in the registry"
    ]


def test_guard_flags_a_route_in_a_mount_with_a_leading_parameter() -> None:
    app = _app_with(fn_is_stub=True)
    app.mount("/{x}/v1", _bare_sub("/products/export"))
    assert TestClient(app).get("/api/v1/products/export").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /{x}/v1/products/export: route is not in the registry"
    ]


def test_guard_flags_a_route_in_a_path_converter_mount() -> None:
    app = _app_with(fn_is_stub=True)
    app.mount("/{p:path}", _bare_sub("/export"))
    assert TestClient(app).get("/api/v1/export").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /{p:path}/export: route is not in the registry"
    ]


def test_guard_flags_a_parameter_glued_to_a_literal() -> None:
    app = _app_with(fn_is_stub=True)
    app.add_api_route("/api/{x}1/zz-export", _leak, methods=["GET"], include_in_schema=False)
    assert TestClient(app).get("/api/v1/zz-export").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /api/{x}1/zz-export: route is not in the registry"
    ]


def test_guard_flags_a_mount_with_a_parameter_glued_to_a_literal() -> None:
    app = _app_with(fn_is_stub=True)
    app.mount("/a{x}", _bare_sub("/v1/zz"))
    assert TestClient(app).get("/api/v1/zz").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /a{x}/v1/zz: route is not in the registry"
    ]
