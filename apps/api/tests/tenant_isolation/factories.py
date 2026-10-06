"""Row factories for the isolation suite (S9 T16-T18): one row per TenantScoped table per
tenant, built on the admin connection with the schema-test factories (every CHECK and composite
FK satisfied), plus the NULL-tenant rows of `rules` and `audit_log`.

`build_world(conn, n)` returns a `World`: for each table, the primary-key value of each
tenant's row (`None` for a table whose key is not a single `id`)."""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg
import tests.integration.factories as f
import tests.integration.ops_support as ops

Conn = psycopg.Connection[tuple[Any, ...]]
_counter = itertools.count(1)


def table_name(model: type[Any]) -> str:
    """A mapped class's table name (typed for mypy, which sees the mixin, not the mapping)."""
    return str(model.__table__.name)


@dataclass
class World:
    tenants: list[UUID]
    rows: dict[str, dict[UUID, Any]] = field(default_factory=dict)
    platform_rows: dict[str, Any] = field(default_factory=dict)

    def put(self, table: str, tenant_id: UUID, key: Any) -> None:
        self.rows.setdefault(table, {})[tenant_id] = key


def invitation(conn: Conn, tenant_id: UUID, invited_by: UUID) -> UUID:
    n = next(_counter)
    return f.insert(  # type: ignore[no-any-return]
        conn,
        "invitations",
        tenant_id=tenant_id,
        email=f"invite{n}@x.test",
        role="operator",
        token_hash=hashlib.sha256(f"invite-{n}-{tenant_id}".encode()).digest(),
        invited_by_user_id=invited_by,
        expires_at="2099-01-01T00:00:00Z",
    )


def _tenant_rows(conn: Conn, world: World) -> None:
    side = ops.seed_tenant(conn)
    t = side.tenant_id
    world.tenants.append(t)
    put: Callable[[str, Any], None] = lambda table, key: world.put(table, t, key)  # noqa: E731
    put("tenants", t)
    put("listings", side.listing_id)
    put("listing_versions", side.version_id)
    put("listing_items", side.item_id)
    put("marketplace_accounts", side.account_id)
    put("batches", side.batch_id)
    membership = conn.execute(
        "SELECT id FROM tenant_memberships WHERE tenant_id = %s AND user_id = %s",
        [t, side.user_id],
    ).fetchone()
    assert membership is not None
    put("tenant_memberships", membership[0])
    put("invitations", invitation(conn, t, side.user_id))
    put("business_parties", f.party(conn, t))
    f.brand_profile(conn, t)
    put("brand_profiles", None)
    style = f.product(conn, t, "style", size_run=["s"])
    put("products", side.product_id)
    colourway = f.colourway(conn, t, style)
    put("product_colourways", colourway)
    # 0004's matrix guard: a size_color cell names both axes, and its size is in the run
    put("variant_matrix_cells", f.cell(conn, t, style, colourway_id=colourway, size_key="s"))
    put("images", f.image(conn, t, side.product_id))
    put("brand_documents", f.brand_document(conn, t))
    put("brand_chunks", f.brand_chunk(conn, t))
    put("audit_log", f.audit(conn, t))
    put("rules", f.rule(conn, t))
    put(
        "review_decisions",
        f.review_decision(
            conn, t, side.listing_id, side.version_id, decided_by_user_id=side.user_id
        ),
    )
    put("validation_runs", f.validation_run(conn, t, side.listing_id))
    put("agent_runs", f.agent_run(conn, t))
    put("performance", f.performance(conn, t, side.listing_id))
    put("human_tasks", f.human_task(conn, t, product_id=side.product_id))
    put("listing_issues", f.listing_issue(conn, t, side.listing_id))
    put("job_runs", ops.job_run(conn, t))
    import_id = ops.import_run(conn, side)
    put("imports", import_id)
    put("import_rows", ops.import_row(conn, t, import_id))
    ops.idempotency_key(conn, side)
    put("idempotency_keys", None)
    put("refresh_tokens", ops.refresh_token(conn, side))
    put("oauth_states", ops.oauth_state(conn, side))
    submission_id = ops.submission(conn, side)
    put("listing_submissions", submission_id)
    put("publish_field_results", ops.field_result(conn, side, submission_id))


def build_world(conn: Conn, tenants: int = 2) -> World:
    """`tenants` tenants with one row in every tenant table each, and one NULL-tenant row in
    each of `rules` and `audit_log`. Runs on the caller's (admin) connection."""
    world = World(tenants=[])
    for _ in range(tenants):
        _tenant_rows(conn, world)
    world.platform_rows["rules"] = f.rule(conn, None)
    world.platform_rows["audit_log"] = f.audit(conn, None)
    return world
