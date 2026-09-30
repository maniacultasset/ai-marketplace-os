"""AC7: every route sits under /api/v1 or in the CANON.md:34 allowlist of unversioned routes."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts

API_V1_PREFIX = "/api/v1"
UNVERSIONED = frozenset({"/healthz", "/readyz", "/openapi.json", "/docs", "/internal/admin"})


def route_paths(app: FastAPI) -> list[str]:
    """Every effective route path, included routers flattened (as OpenAPI generation sees them)."""
    return [str(context.path) for context in iter_route_contexts(app.routes)]


def offending_paths(app: FastAPI) -> list[str]:
    """The guard: route paths that are neither versioned nor allowlisted."""
    bad = []
    for path in route_paths(app):
        if path in UNVERSIONED or path == API_V1_PREFIX or path.startswith(API_V1_PREFIX + "/"):
            continue
        bad.append(path)
    return bad


async def _handler() -> dict[str, str]:
    return {}


def test_every_route_versioned_or_allowlisted(make_app: Callable[[], FastAPI]) -> None:
    app = make_app()
    assert offending_paths(app) == []
    assert {"/healthz", "/readyz", "/docs", "/openapi.json"} <= set(route_paths(app))


def test_guard_flags_unprefixed_route(make_app: Callable[[], FastAPI]) -> None:
    app = make_app()
    app.add_api_route("/foo", _handler)
    assert offending_paths(app) == ["/foo"]
    # A route reached through an included router is flattened, never hidden.
    nested = APIRouter()
    nested.add_api_route("/bar", _handler)
    app.include_router(nested)
    assert offending_paths(app) == ["/foo", "/bar"]


def test_guard_flags_lookalikes(make_app: Callable[[], FastAPI]) -> None:
    app = make_app()
    for path in ("/api/v1foo", "/api/v2/x", "/API/V1/x", "/healthz/extra", "/v1/x"):
        app.add_api_route(path, _handler)
    assert offending_paths(app) == [
        "/api/v1foo",
        "/api/v2/x",
        "/API/V1/x",
        "/healthz/extra",
        "/v1/x",
    ]


def test_guard_accepts_nested_v1(make_app: Callable[[], FastAPI]) -> None:
    app = make_app()
    app.add_api_route("/api/v1/products/{product_id}/images", _handler)
    app.add_api_route("/api/v1/marketplaces/{marketplace}/callback", _handler)
    assert offending_paths(app) == []


def test_allowlist_equals_canon(repo_root: Path) -> None:
    canon = (repo_root / "docs/canon/CANON.md").read_text(encoding="utf-8")
    row = next(line for line in canon.splitlines() if line.startswith("| Unversioned endpoints"))
    cell = re.search(r"\| `([^`]+)` \|", row)
    assert cell is not None
    assert frozenset(p.strip() for p in cell.group(1).split(",")) == UNVERSIONED


def test_no_oauth2_redirect_route(make_app: Callable[[], FastAPI]) -> None:
    paths = set(route_paths(make_app()))
    assert "/docs/oauth2-redirect" not in paths
    assert "/redoc" not in paths
