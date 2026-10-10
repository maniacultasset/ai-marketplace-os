"""The S26 security suites (OWASP API Top 10 as tests, secrets, prompt injection).

Every test here is marked `security` and runs in `make api-tests` against mos_test, through the
integration suite's fixtures (`auth_db`, `api`, `superuser_conn`), re-exported below.

`world2` is the two-tenant world: acme and bharat, one user per role each (S10 `build_world`),
and in each tenant one product, import, listing (with a version), image, job, marketplace
account, brand document and issue, built on the admin connection. Every text field carries the
tenant's canary (`ACMEzq9-<hex>`, `BHARATzq9-<hex>`), so a cross-tenant leak is a substring check.

Helpers:
- `ops(*, public=None, path_id=None)`: the registry ops, filtered;
- `call_for(op, world, slug)`: a valid request whose path and body ids name `slug`'s rows;
- `tenant_counts(conn, tenant_id)`: `count(*)` per tenant table, to prove a call changed nothing.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from tests.integration.auth_support import ROLES, World, bearer, build_world, tokens_for

# The integration fixtures, re-exported for this folder (a conftest's fixtures reach only the
# tests below it).
from tests.integration.conftest import (  # noqa: F401
    LOCAL_HOSTS,
    api,
    auth_db,
    clean_tenant_db,
    db_settings,
    superuser_conn,
)

if TYPE_CHECKING:
    from app.api.v1.surface import Operation
    from tests.api_requests import Call

HERE = Path(__file__).resolve().parent
SLUGS = ("acme", "bharat")
# A category with a seeded template (tests/integration/imports_support.py), so every op that
# reads the template (validate, generate, preview, dry-run) runs its real path.
CATEGORY = "ethnic_wear_kurta_sets"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if HERE in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.security)
            item.add_marker(pytest.mark.allow_hosts(LOCAL_HOSTS))


def canary_for(slug: str) -> str:
    return f"{slug.upper()}zq9-{secrets.token_hex(8)}"


@dataclass
class Side:
    slug: str
    tenant_id: uuid.UUID
    canary: str
    ids: dict[str, Any] = field(default_factory=dict)


@dataclass
class World2:
    base: World
    sides: dict[str, Side]

    def side(self, slug: str) -> Side:
        return self.sides[slug]

    def token(self, email: str, slug: str | None = None, role: str | None = None) -> str:
        return tokens_for(self.base, email, slug or email.split("@", 1)[1].split(".")[0], role)

    def headers(self, email: str, slug: str | None = None) -> dict[str, str]:
        return bearer(self.token(email, slug))

    def foreign_values(self, slug: str) -> list[str]:
        """Every id and canary of `slug`'s side, as text (what must never cross over)."""
        side = self.sides[slug]
        values = [side.canary, str(side.tenant_id)]
        values += [str(v) for k, v in side.ids.items() if k != "storage_prefix"]
        values += [str(self.base.user(f"{r}@{slug}.test")) for r in ROLES]
        return values


def _side(conn: Any, world: World, slug: str) -> Side:
    import tests.integration.factories as f
    import tests.integration.ops_support as ops_support

    t = world.tenant(slug)
    owner = world.user(f"owner@{slug}.test")
    operator = world.user(f"operator@{slug}.test")
    canary = canary_for(slug)
    product_id = f.product(
        conn, t, sku=f"{slug.upper()}-SKU-1", title=f"{canary} kurta", category=CATEGORY
    )
    account_id = f.marketplace_account(
        conn, t, connected_by_user_id=owner, seller_id=f"{slug.upper()}SELLER"
    )
    listing_id = f.listing(
        conn, t, product_id, marketplace_account_id=account_id, category=CATEGORY
    )
    version_id = f.version(
        conn,
        t,
        listing_id,
        generated_by_user_id=operator,
        content_json={
            "title": f"{canary} listing title",
            "bullets": ["Soft cotton fabric"],
            "description": f"{canary} listing description",
        },
    )
    conn.execute(
        "UPDATE listings SET current_version_id = %s WHERE id = %s", [version_id, listing_id]
    )
    seed = ops_support.TenantSeed(
        t, owner, product_id, account_id, listing_id, version_id, uuid.uuid4(), uuid.uuid4()
    )
    job_id = ops_support.job_run(conn, t, created_by=operator)
    # the import's own job has finished: the tenant holds one live import job (the cap is 2)
    import_job = ops_support.job_run(
        conn, t, status="succeeded", started_at="2026-10-06T00:00:00Z",
        finished_at="2026-10-06T00:00:01Z", attempt=1,
    )  # fmt: skip
    import_id = ops_support.import_run(conn, seed, filename=f"{canary}.csv", job_id=import_job)
    ops_support.import_row(conn, t, import_id)
    image_id = f.image(conn, t, product_id)
    document_id = f.brand_document(conn, t, created_by=owner, filename=f"{canary}.pdf")
    issue_id = f.listing_issue(conn, t, listing_id, message=f"{canary} issue")
    ids: dict[str, Any] = {
        "product_id": product_id,
        "account_id": account_id,
        "listing_id": listing_id,
        "listing_version_id": version_id,
        "job_id": job_id,
        "import_id": import_id,
        "image_id": image_id,
        "document_id": document_id,
        "issue_id": issue_id,
        "user_id": operator,
        "target_tenant_id": t,
        "storage_prefix": f"{t}/images/",
    }
    return Side(slug, t, canary, ids)


def build_world2(conn: Any) -> World2:
    from tests.integration.imports_support import seed_templates
    from tests.integration.products_support import seed_dictionary
    from tests.support.rate_limits import reset_rate_limits

    seed_dictionary(conn)
    seed_templates(conn)
    base = build_world(conn)
    sides = {slug: _side(conn, base, slug) for slug in SLUGS}
    reset_rate_limits(conn)
    return World2(base, sides)


@pytest.fixture
def world2(auth_db: Any) -> World2:  # noqa: F811 - the re-exported fixture
    return build_world2(auth_db)


def ops(
    *, public: bool | None = None, path_id: bool | None = None, body: bool | None = None
) -> list[Operation]:
    from app.api.v1.surface import OPS

    out = list(OPS.values())
    if public is not None:
        out = [op for op in out if op.public is public]
    if path_id is not None:
        out = [op for op in out if (op.path_id is not None) is path_id]
    if body is not None:
        out = [op for op in out if op.has_body is body]
    return out


def ids_of(items: Iterable[Operation]) -> list[str]:
    return [op.op_id for op in items]


def resolve_id(world: World2, slug: str, name: str) -> Any:
    """The id `name` (a path id or body id, `items[].x` included) names in `slug`'s side."""
    key = name.removeprefix("items[].")
    if key in {"images", "storage_key"}:
        return world.side(slug).ids["image_id"]
    return world.side(slug).ids[key]


def call_for(op: Operation, world: World2, slug: str) -> Call:
    """`valid_request(op)` with every path and body id naming `slug`'s rows."""
    from tests.api_requests import valid_request

    call = valid_request(op)
    side = world.side(slug)
    if op.path_id:
        value = str(side.ids[op.path_id])
        placeholder = "0b1c2d3e-0000-4000-8000-00000000abcd"
        call.url = call.url.replace(placeholder, value)
    if call.json is not None:
        body = dict(call.json)
        if op.op_id == "products_create":
            body["category"] = CATEGORY
        if op.op_id == "rules_dry_run":
            body["listing"] = {**body["listing"], "category": CATEGORY}
        for name in ("product_id", "listing_id", "target_tenant_id"):
            if name in body:
                body[name] = str(side.ids[name])
        if "items" in body:
            body["items"] = [
                {
                    **item,
                    "listing_id": str(side.ids["listing_id"]),
                    **(
                        {"listing_version_id": str(side.ids["listing_version_id"])}
                        if "listing_version_id" in item
                        else {}
                    ),
                }
                for item in body["items"]
            ]
        call.json = body
    if call.data is not None and "product_id" in call.data:
        call.data = {**call.data, "product_id": str(side.ids["product_id"])}
    if op.idempotency:
        call.headers["Idempotency-Key"] = f"s26-{uuid.uuid4().hex}"
    return call


async def send(client: Any, call: Call, headers: dict[str, str]) -> Any:
    kwargs: dict[str, Any] = {"headers": {**call.headers, **headers}, "params": call.params}
    if call.json is not None:
        kwargs["json"] = call.json
    if call.files is not None:
        kwargs["files"] = call.files
    if call.data is not None:
        kwargs["data"] = call.data
    return await client.request(call.method, call.url, **kwargs)


def tenant_tables(conn: Any) -> list[str]:
    rows = conn.execute(
        "SELECT c.table_name FROM information_schema.columns c "
        "JOIN information_schema.tables t USING (table_schema, table_name) "
        "WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id' "
        "AND t.table_type = 'BASE TABLE' ORDER BY 1"
    ).fetchall()
    return [r[0] for r in rows]


def tenant_counts(conn: Any, tenant_id: uuid.UUID) -> dict[str, int]:
    """`count(*)` of `tenant_id`'s rows per tenant table, on the admin connection."""
    from psycopg import sql

    out: dict[str, int] = {}
    for table in tenant_tables(conn):
        query = sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(
            sql.Identifier(table)
        )
        row = conn.execute(query, [tenant_id]).fetchone()
        out[table] = int(row[0]) if row else 0
    return out


def tenant_fingerprint(conn: Any, tenant_id: uuid.UUID) -> dict[str, str]:
    """An md5 of every tenant table's rows for `tenant_id` (catches updates, not only inserts)."""
    from psycopg import sql

    out: dict[str, str] = {}
    for table in tenant_tables(conn):
        query = sql.SQL(
            "SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
            "FROM {} t WHERE tenant_id = %s"
        ).format(sql.Identifier(table))
        row = conn.execute(query, [tenant_id]).fetchone()
        out[table] = str(row[0]) if row else ""
    return out


def walk(value: Any) -> Iterable[tuple[str, Any]]:
    """Every (key, value) pair in a JSON body, at any depth."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key, inner
            yield from walk(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from walk(inner)


# ---- a production app (API5, API8): the CI block plus what a deployed boot needs ----


def prod_env() -> dict[str, str]:
    from tests.support.deployed_env import DEPLOYED_S15_ENV

    return {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:5432/x",
        "JWT_SECRET": "x" * 32,
        "SENTRY_DSN": "https://public@o0.ingest.example.invalid/0",
        "ADMIN_ENABLED": "false",
        **DEPLOYED_S15_ENV,
    }


@pytest.fixture
async def prod_client(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """An httpx client on `create_app()` under APP_ENV=production (no database is touched)."""
    import app.main
    import httpx
    from app.core.config import get_settings

    for key, value in prod_env().items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    monkeypatch.setattr(app.main, "init_sentry", lambda _: False)
    transport = httpx.ASGITransport(app=app.main.create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    get_settings.cache_clear()


# ---- API7/API10: every outbound connection the code under test opens ----


@pytest.fixture
def socket_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Records the (host, port) of every Python-level socket connect and every httpx request
    while the test runs (libpq, the database driver, connects in C and is not seen). The real
    call still happens, under pytest-socket's host allow-list."""
    import socket

    import httpx

    seen: list[tuple[str, int]] = []
    sock_cls = socket.socket
    connect, connect_ex = sock_cls.connect, sock_cls.connect_ex

    def _record(address: Any) -> None:
        if isinstance(address, tuple) and len(address) >= 2:
            seen.append((str(address[0]), int(address[1])))
        else:
            seen.append((str(address), 0))

    def spy_connect(self: Any, address: Any) -> Any:
        _record(address)
        return connect(self, address)

    def spy_connect_ex(self: Any, address: Any) -> Any:
        _record(address)
        return connect_ex(self, address)

    monkeypatch.setattr(sock_cls, "connect", spy_connect)
    monkeypatch.setattr(sock_cls, "connect_ex", spy_connect_ex)
    for transport in (httpx.AsyncHTTPTransport, httpx.ASGITransport):
        original = transport.handle_async_request

        def make(orig: Any) -> Any:
            async def spy(self: Any, request: Any) -> Any:
                if request.url.host != "testserver":
                    seen.append((request.url.host, request.url.port or 0))
                return await orig(self, request)

            return spy

        monkeypatch.setattr(transport, "handle_async_request", make(original))
    return seen
