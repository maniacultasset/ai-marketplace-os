"""FB6 helpers: a known Home scenario in one tenant, written with the guards off for its set-up
(test arrangement only, as `quality_support`), and the expected change of every count."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import factories as f
from .generation_support import GenWorld

SOON = datetime.now(UTC) + timedelta(minutes=5)
LATER = SOON + timedelta(minutes=1)
PLAIN = "The title is longer than Amazon allows."

# What `scenario` adds to each listing bucket (the archived listing adds nothing).
PRODUCTS_DELTA = 13
BUCKET_DELTA = {
    "total": 11,
    "live": 2,
    "partly_live": 0,
    "in_progress": 1,
    "problem": 3,
    "ready_to_approve": 2,
    "ready_to_publish": 2,
    "draft": 1,
    "inactive": 0,
}


@dataclass(frozen=True, slots=True)
class HomeScenario:
    tenant: uuid.UUID
    blocked: uuid.UUID
    blocked_product: uuid.UUID
    blocked_issue: uuid.UUID
    question_listing: uuid.UUID
    question: uuid.UUID
    failed: uuid.UUID
    fixed_title: str


def _versioned(conn: Any, tenant: uuid.UUID, **columns: Any) -> tuple[uuid.UUID, uuid.UUID]:
    listing = f.listing(conn, tenant, **columns)
    version = f.version(conn, tenant, listing)
    conn.execute("UPDATE listings SET current_version_id = %s WHERE id = %s", [version, listing])
    return listing, version


def scenario(world: GenWorld, slug: str) -> HomeScenario:
    """In `slug`, 12 listings (one archived) whose buckets are `BUCKET_DELTA`, one blocking and
    one non-blocking open question (13 new products in all), and one issue Kuinn fixed on its
    own."""
    conn = world.conn
    tenant = world.tenant(slug)
    conn.execute("SET session_replication_role = replica")
    try:
        f.listing(conn, tenant, marketplace_state="live", status="published")
        f.listing(conn, tenant, status="generating")
        f.listing(conn, tenant, status="created")
        f.listing(conn, tenant, status="approved")  # amazon: paused when publishing is off
        f.listing(conn, tenant, status="approved", marketplace="flipkart")
        _versioned(conn, tenant, status="validated")
        # an open error on an older version is history: still ready to approve
        old, _ = _versioned(conn, tenant, status="needs_review")
        older = f.version(conn, tenant, old)
        f.listing_issue(conn, tenant, old, listing_version_id=older)
        # blocked on a fix: an open error on the current version, linked to no question
        blocked, version = _versioned(conn, tenant, status="validated", status_changed_at=LATER)
        issue = f.listing_issue(
            conn, tenant, blocked, listing_version_id=version, plain_message=PLAIN
        )
        # blocked on an answer only: its open error waits on an open question
        q_listing, q_version = _versioned(conn, tenant, status="blocked")
        product = world.scalar("SELECT product_id FROM listings WHERE id = %s", [q_listing])
        question = f.human_task(conn, tenant, product_id=product)
        f.listing_issue(
            conn, tenant, q_listing, listing_version_id=q_version, human_task_id=question
        )
        failed = f.listing(
            conn, tenant, status="failed", status_reason="generation_failed", status_changed_at=SOON
        )
        # archived: in no bucket
        f.listing(conn, tenant, status="validated", archived_at=datetime.now(UTC))
        # a question that holds nothing back
        f.human_task(conn, tenant)
        # a fix Kuinn made itself, the newest action
        fixed, _ = _versioned(conn, tenant, marketplace_state="live", status="published")
        f.listing_issue(
            conn, tenant, fixed, status="auto_fixed", resolution="deterministic_fix",
            resolved_at=LATER, action_taken="Shortened the title.",
        )  # fmt: skip
    finally:
        conn.execute("SET session_replication_role = DEFAULT")
    conn.commit()
    title = world.scalar(
        "SELECT p.title FROM listings l JOIN products p ON p.id = l.product_id WHERE l.id = %s",
        [fixed],
    )
    blocked_product = world.scalar("SELECT product_id FROM listings WHERE id = %s", [blocked])
    return HomeScenario(
        tenant, blocked, blocked_product, issue, q_listing, question, failed, str(title)
    )
