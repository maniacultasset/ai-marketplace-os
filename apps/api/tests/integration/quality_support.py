"""SI14 T19-T20 helpers: a known quality scenario, written with the guards off for its set-up
(test arrangement only), plus one listing blocked through the real generation worker."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from . import factories as f
from .generation_support import GenWorld, make_generating

NOW = datetime.now(UTC)
RANGE = (NOW - timedelta(days=1), NOW + timedelta(days=1))


def _replica(conn: Any) -> None:
    conn.execute("SET session_replication_role = replica")


def _normal(conn: Any) -> None:
    conn.execute("SET session_replication_role = DEFAULT")


def scenario(world: GenWorld, slug: str) -> dict[str, uuid.UUID]:
    """In `slug`: L2 amazon first-pass approved (plus a user move to blocked, audited, and a
    USD 0.02 run), L3 flipkart approved after an edit of `title` and `bullets.0` (its generation
    took 2 attempts), L4 amazon generated and approved 40 days ago (out of range)."""
    conn = world.conn
    tenant = world.tenant(slug)
    old = NOW - timedelta(days=40)
    approver = f.member(conn, tenant, "reviewer")
    _replica(conn)
    try:
        l2 = f.listing(conn, tenant, marketplace="amazon")
        v2 = f.version(
            conn, tenant, l2, attempts_used=1, approved_at=NOW, approved_by_user_id=approver
        )
        l3 = f.listing(conn, tenant, marketplace="flipkart")
        v3a = f.version(conn, tenant, l3, attempts_used=2)
        f.version(
            conn,
            tenant,
            l3,
            origin="edit",
            parent_version_id=v3a,
            changed_paths=["title", "bullets.0"],
            approved_at=NOW,
            approved_by_user_id=approver,
        )
        l4 = f.listing(conn, tenant, marketplace="amazon")
        f.version(
            conn,
            tenant,
            l4,
            attempts_used=1,
            created_at=old,
            approved_at=old,
            approved_by_user_id=approver,
        )
        f.audit(
            conn,
            tenant,
            actor_type="user",
            actor_user_id=approver,
            action="listing.status_changed",
            entity_type="listing",
            entity_id=l2,
            after=Jsonb({"status": "blocked"}),
        )
        f.agent_run(conn, tenant, listing_id=l2, cost_usd=Decimal("0.02"), started_at=NOW)
    finally:
        _normal(conn)
    conn.commit()
    return {"l2": l2, "v2": v2, "l3": l3, "l4": l4}


async def blocked_by_worker(world: GenWorld) -> uuid.UUID:
    """KURTA-002 x amazon of acme driven to `blocked` by the generation worker (stub)."""
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    accepted = await make_generating(world, sku="KURTA-002", instructions="stub:blocked")
    register_bodies()
    await run_job(str(accepted.job_id))
    assert world.listing(accepted.listing_id)["status"] == "blocked"
    listing_id: uuid.UUID = accepted.listing_id
    return listing_id
