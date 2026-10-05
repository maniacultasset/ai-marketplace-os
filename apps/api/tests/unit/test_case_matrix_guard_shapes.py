"""S3 T15 (AC9), tester round 4: more route shapes that answer an /api/v1 URL from outside the
registry. The R3-1 guard (dispatch-based mount walk) must flag each one."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Router, WebSocketRoute
from starlette.websockets import WebSocket
from tests.endpoint_cases import guard_failures
from tests.unit.test_case_matrix_guard import _app_with, scan_dir_empty


async def _leak() -> dict[str, str]:
    return {"rows": "every tenant's products"}


def _bare_sub(path: str) -> FastAPI:
    sub = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sub.add_api_route(path, _leak, methods=["GET"])
    return sub


def test_guard_flags_two_parameters_glued_together() -> None:
    app = _app_with(fn_is_stub=True)
    app.add_api_route("/api/{a}{b}/zz", _leak, methods=["GET"], include_in_schema=False)
    assert TestClient(app).get("/api/v1/zz").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /api/{a}{b}/zz: route is not in the registry"
    ]


def test_guard_flags_a_float_converter_version() -> None:
    app = _app_with(fn_is_stub=True)
    app.add_api_route("/api/v{n:float}/zz", _leak, methods=["GET"], include_in_schema=False)
    assert TestClient(app).get("/api/v1/zz").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /api/v{n:float}/zz: route is not in the registry"
    ]


def test_guard_flags_a_templated_mount_the_guard_cannot_look_into() -> None:
    # Middleware hides the sub-app's routes; the mount itself must be flagged.
    app = _app_with(fn_is_stub=True)
    app.mount("/api/{ver}", CORSMiddleware(_bare_sub("/zz"), allow_origins=["*"]))
    assert TestClient(app).get("/api/v1/zz").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "MOUNT /api/{ver}: route is not in the registry"
    ]


def test_guard_flags_nested_templated_router_prefixes() -> None:
    app = _app_with(fn_is_stub=True)
    outer = APIRouter(prefix="/api/{ver}")
    inner = APIRouter(prefix="/{x}")
    inner.add_api_route("/zz", _leak, methods=["GET"])
    outer.include_router(inner)
    app.include_router(outer)
    assert TestClient(app).get("/api/v1/a/zz").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /api/{ver}/{x}/zz: route is not in the registry"
    ]


def test_guard_flags_a_fully_templated_route_two_mounts_deep() -> None:
    app = _app_with(fn_is_stub=True)
    outer, mid = FastAPI(), FastAPI()
    mid.mount("/{v}", _bare_sub("/{id}/{t}/{u}/{w}"))
    outer.mount("/{p}", mid)
    app.mount("", outer)
    assert TestClient(app).get("/api/v1/a/b/c/d").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "GET /{p}/{v}/{id}/{t}/{u}/{w}: route is not in the registry"
    ]


def test_guard_flags_a_websocket_in_a_templated_mount() -> None:
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close()

    app = _app_with(fn_is_stub=True)
    app.mount("/api/{ver}", Router(routes=[WebSocketRoute("/zz", ws)]))
    with TestClient(app).websocket_connect("/api/v1/zz"):
        pass
    assert guard_failures(app, scan_dir_empty()) == [
        "WEBSOCKET /api/{ver}/zz: route is not in the registry"
    ]


def test_guard_flags_a_post_only_raw_route_in_a_templated_mount() -> None:
    async def h(request: Request) -> JSONResponse:
        return JSONResponse({})

    app = _app_with(fn_is_stub=True)
    app.mount("/api/{ver}", Router(routes=[Route("/zz/{n:int}", h, methods=["POST"])]))
    assert TestClient(app).post("/api/v1/zz/5").status_code == 200
    assert guard_failures(app, scan_dir_empty()) == [
        "POST /api/{ver}/zz/{n:int}: route is not in the registry"
    ]
