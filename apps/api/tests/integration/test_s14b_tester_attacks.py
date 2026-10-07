"""S14b tester attacks (round 1): the rules list keyset on tied timestamps, dry-run body edges,
the admin's sort/search/page-size query surface and the staff gate on every method."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from .admin_support import ADMIN, AdminClient
from .auth_support import World
from .conftest import LOCAL_HOSTS
from .products_support import bearer_for
from .seam_support import CATEGORY, Conn, add_rule, banned, max_length

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
RULES = "/api/v1/rules"
DRY_RUN = "/api/v1/rules/dry-run"
TIED = datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC)


def _as(world: World, role: str = "operator", slug: str = "acme") -> dict[str, str]:
    return bearer_for(world, f"{role}@{slug}.test")


def _identity(table: str) -> str:
    from app.admin.views import ADMIN_VIEWS

    (view,) = [v for v in ADMIN_VIEWS if v.table == table]
    return str(view.identity)


# ---- GET /rules: keyset over rows that share one created_at ----


async def test_rules_list_keyset_tied_created_at_platform_and_own(
    api: httpx.AsyncClient, products_world: World, auth_db: Conn
) -> None:
    from . import factories as f

    acme, bharat = products_world.tenant("acme"), products_world.tenant("bharat")

    def tied(body: dict[str, Any], tenant: uuid.UUID | None, version: int) -> str:
        return str(
            f.rule(
                auth_db,
                tenant,
                rule_def=body,
                category=CATEGORY,
                rule_type=body["type"],
                pack=body.get("pack", "core"),
                version=version,
                created_at=TIED,
            )
        )

    mine = {tied(max_length(120 + n), None, n + 1) for n in range(4)}
    mine |= {tied(banned([f"term{n}"]), acme, n + 1) for n in range(3)}
    foreign = tied(banned(["other"]), bharat, 1)
    assert auth_db.execute("SELECT count(DISTINCT created_at) FROM rules").fetchone() == (1,)
    items: list[Any] = []
    query: dict[str, Any] = {"limit": 2}
    while True:
        response = await api.get(RULES, params=query, headers=_as(products_world))
        assert response.status_code == 200, response.text
        body = response.json()
        items += body["data"]
        if not body["page"]["has_more"]:
            break
        query = {"limit": 2, "cursor": body["page"]["next_cursor"]}
    ids = [r["id"] for r in items]
    assert len(ids) == len(set(ids)) == len(mine)
    assert set(ids) == mine and foreign not in ids
    assert ids == sorted(ids)  # the id tie-break holds the order


# ---- POST /rules/dry-run: body edges ----


@pytest.fixture
async def seeded_rules(products_world: World, auth_db: Conn) -> World:
    import os

    from app.cli import RULES_SEED_PATH
    from app.db.seed_ops import seed_platform_rows
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(os.environ["ALEMBIC_DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await seed_platform_rows(connection, RULES_SEED_PATH)
    finally:
        await engine.dispose()
    return products_world


def _listing(title: str = "Cotton kurta set") -> dict[str, Any]:
    return {
        "marketplace": "amazon",
        "category": CATEGORY,
        "product_id": str(uuid.uuid4()),
        "content": {"title": title, "bullets": ["Soft"], "description": "A kurta."},
        "attributes": {"color": "indigo", "fabric_type": "cotton"},
    }


@pytest.mark.parametrize(
    ("content", "data"),
    [
        ("text/plain", json.dumps({"listing": _listing()})),
        ("application/json", "{"),
        ("application/json", "[]"),
        ("application/json", "null"),
        ("application/json", json.dumps({"listing": None})),
        ("application/json", '{"listing": ' + "[" * 5000 + "]" * 5000 + "}"),
    ],
    ids=["text_plain", "truncated", "array", "null", "listing_null", "deep_nesting"],
)
async def test_attack_dry_run_malformed_bodies_4xx(
    api: httpx.AsyncClient, seeded_rules: World, auth_db: Conn, content: str, data: str
) -> None:
    before = auth_db.execute("SELECT count(*) FROM job_runs").fetchone()
    headers = {**_as(seeded_rules), "content-type": content}
    response = await api.post(DRY_RUN, content=data.encode(), headers=headers)
    assert 400 <= response.status_code < 500, (response.status_code, response.text[:200])
    assert response.json()["error"]["type"] in {"VALIDATION_FAILED", "PAYLOAD_TOO_LARGE"}
    assert auth_db.execute("SELECT count(*) FROM job_runs").fetchone() == before


async def test_attack_dry_run_padded_body_under_cap_200(
    api: httpx.AsyncClient, seeded_rules: World
) -> None:
    from app.api.v1.routers.rules import DRY_RUN_MAX_BODY_BYTES

    raw = json.dumps({"listing": _listing()}).encode()
    padded = raw[:-1] + b" " * (DRY_RUN_MAX_BODY_BYTES - len(raw)) + b"}"
    assert len(padded) == DRY_RUN_MAX_BODY_BYTES
    headers = {**_as(seeded_rules), "content-type": "application/json"}
    ok = await api.post(DRY_RUN, content=padded, headers=headers)
    assert ok.status_code == 200, ok.text
    over = await api.post(DRY_RUN, content=padded + b" ", headers=headers)
    assert over.status_code == 413
    assert over.json()["error"]["type"] == "PAYLOAD_TOO_LARGE"


async def test_attack_dry_run_cap_not_bypassed_by_trailing_slash(
    api: httpx.AsyncClient, seeded_rules: World
) -> None:
    from app.api.v1.routers.rules import DRY_RUN_MAX_BODY_BYTES

    big = b'{"listing": {"x": "' + b"a" * DRY_RUN_MAX_BODY_BYTES + b'"}}'
    headers = {**_as(seeded_rules), "content-type": "application/json"}
    response = await api.post(DRY_RUN + "/", content=big, headers=headers)
    assert response.status_code != 200
    assert response.status_code < 500


async def test_attack_dry_run_get_method_not_allowed(
    api: httpx.AsyncClient, seeded_rules: World
) -> None:
    response = await api.get(DRY_RUN, headers=_as(seeded_rules))
    assert response.status_code == 405


# ---- /internal/admin: the query surface ----


@pytest.mark.parametrize(
    "query",
    [
        "sortBy=hashed_password",
        "sortBy=hashed_password&sort=desc",
        "sortBy=email",
        "search=%25",
        "search=" + "a" * 5000,
        "pageSize=100000",
        "pageSize=-5",
        "page=1e9",
    ],
)
async def test_attack_admin_users_query_surface(
    admin_client: AdminClient, products_world: World, query: str
) -> None:
    async with admin_client() as client:
        response = await client.get(f"{ADMIN}/{_identity('users')}/list?{query}")
    assert response.status_code < 500, (query, response.status_code)
    if response.status_code == 200:
        assert "$argon2" not in response.text and "$2b$" not in response.text


async def test_attack_admin_page_size_clamped(
    admin_client: AdminClient, products_world: World, auth_db: Conn
) -> None:
    for n in range(105):
        add_rule(auth_db, max_length(100 + n), version=n + 1)
    async with admin_client() as client:
        response = await client.get(f"{ADMIN}/{_identity('rules')}/list?pageSize=100000")
    assert response.status_code == 200
    links = set(
        re.findall(rf"{ADMIN}/{_identity('rules')}/details/([0-9a-f-]{{36}})", response.text)
    )
    assert 0 < len(links) <= 100


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
async def test_attack_admin_staging_every_method_403(
    admin_client: AdminClient, products_world: World, method: str
) -> None:
    headers = bearer_for(products_world, "admin@acme.test")
    async with admin_client("staging") as client:
        for path in (f"{ADMIN}/", f"{ADMIN}/{_identity('users')}/list", f"{ADMIN}/ops/dlq"):
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 403, (method, path, response.status_code)
            assert "@" not in response.text


async def test_attack_admin_staging_cors_preflight_grants_nothing_readable(
    admin_client: AdminClient,
) -> None:
    """The API's CORS layer sits outside the admin mount and answers a preflight itself; the
    real request that follows still meets the staff gate (403), so no admin byte is readable."""
    pre = {"origin": "http://localhost:3000", "access-control-request-method": "GET"}
    async with admin_client("staging") as client:
        preflight = await client.options(f"{ADMIN}/", headers=pre)
        real = await client.get(f"{ADMIN}/", headers={"origin": "http://localhost:3000"})
    assert preflight.status_code in (200, 400)
    assert preflight.text in ("OK", "") or preflight.status_code == 400
    assert real.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/statics/../../../../etc/passwd",
        "/statics/%2e%2e/%2e%2e/app/core/config.py",
        "/statics/..%2f..%2f__init__.py",
        "/ajax/lookup?name=tenant&term=a",
        "/users/export/csv",
    ],
)
async def test_attack_admin_odd_paths_no_leak(
    admin_client: AdminClient, products_world: World, path: str
) -> None:
    async with admin_client() as client:
        response = await client.get(ADMIN + path)
    assert response.status_code < 500 or response.status_code == 404, response.status_code
    assert "root:" not in response.text
    assert "JWT_SECRET" not in response.text and "import" not in response.text[:200]


async def test_attack_admin_production_ops_and_statics_404(
    admin_client: AdminClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production never mounts the admin, so ops pages and statics are the API's 404 too
    (the boot guard refuses ADMIN_ENABLED=true there, so the flag is off)."""
    async with admin_client("production", enabled=False) as client:
        for path in (f"{ADMIN}/ops/queue", f"{ADMIN}/statics/css/main.css", f"{ADMIN}/"):
            response = await client.get(path)
            assert response.status_code == 404, path
            assert response.json()["error"]["type"] == "NOT_FOUND"
