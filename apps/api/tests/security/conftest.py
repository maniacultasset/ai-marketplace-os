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
    product_id = f.product(conn, t, sku=f"{slug.upper()}-SKU-1", title=f"{canary} kurta")
    account_id = f.marketplace_account(
        conn, t, connected_by_user_id=owner, seller_id=f"{slug.upper()}SELLER"
    )
    listing_id = f.listing(conn, t, product_id, marketplace_account_id=account_id)
    version_id = f.version(
        conn,
        t,
        listing_id,
        generated_by_user_id=operator,
        content_json={"title": f"{canary} listing title"},
    )
    conn.execute(
        "UPDATE listings SET current_version_id = %s WHERE id = %s", [version_id, listing_id]
    )
    seed = ops_support.TenantSeed(
        t, owner, product_id, account_id, listing_id, version_id, uuid.uuid4(), uuid.uuid4()
    )
    job_id = ops_support.job_run(conn, t, created_by=operator)
    import_id = ops_support.import_run(conn, seed, filename=f"{canary}.csv")
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
    from tests.integration.products_support import seed_dictionary
    from tests.support.rate_limits import reset_rate_limits

    seed_dictionary(conn)
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
