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
PRODUCTS_DELTA = 15
BUCKET_DELTA = {
    "total": 13,
    "live": 2,
    "partly_live": 0,
    "in_progress": 1,
    "problem": 3,
    "ready_to_approve": 4,
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
    own: uuid.UUID
    """Ready to approve, edited by `owner@<slug>.test`: maker-checker keeps that owner out."""


def _versioned(conn: Any, tenant: uuid.UUID, **columns: Any) -> tuple[uuid.UUID, uuid.UUID]:
    listing = f.listing(conn, tenant, **columns)
    version = f.version(conn, tenant, listing)
    conn.execute("UPDATE listings SET current_version_id = %s WHERE id = %s", [version, listing])
    return listing, version


def _live(conn: Any, tenant: uuid.UUID) -> uuid.UUID:
    """A published listing the marketplace confirmed live, with what `ck_published` and
    `ck_live_minimum` need."""
    listing, version = _versioned(conn, tenant)
    conn.execute(
        "UPDATE listings SET status = 'published', marketplace_state = 'live',"
        " published_version_id = %s, live_version_id = %s, first_published_at = now(),"
        " last_published_at = now(), live_confirmed_at = now(), last_synced_at = now()"
        " WHERE id = %s",
        [version, version, listing],
    )
    return listing


def connected(world: GenWorld, slug: str, status: str = "active") -> None:
    """An Amazon and a Flipkart account in `slug` with `status` (made once, then updated), so
    approved listings can be sent there."""
    conn = world.conn
    tenant = world.tenant(slug)
    for market in ("amazon", "flipkart"):
        found = world.scalar(
            "SELECT id FROM marketplace_accounts WHERE tenant_id = %s AND marketplace = %s"
            " AND status <> 'revoked'",
            [tenant, market],
        )
        since = datetime.now(UTC) if status == "needs_reconnect" else None
        if found is None:
            f.marketplace_account(
                conn, tenant, marketplace=market, status=status, needs_reconnect_since=since
            )
        else:
            conn.execute(
                "UPDATE marketplace_accounts SET status = %s, needs_reconnect_since = %s"
                " WHERE id = %s",
                [status, since, found],
            )
    conn.commit()


def scenario(world: GenWorld, slug: str) -> HomeScenario:
    """In `slug`, 14 listings (one archived) whose buckets are `BUCKET_DELTA`, one blocking and
    one non-blocking open question (15 new products in all), and one issue Kuinn fixed on its
    own. Two of the listings ready to approve test maker-checker: one whose edit (on top of
    another member's generation) is `owner@<slug>.test`'s, and one that owner generated before
    another member fully regenerated it (the chain stops at the regeneration)."""
    conn = world.conn
    tenant = world.tenant(slug)
    owner = world.user(f"owner@{slug}.test")
    conn.execute("SET session_replication_role = replica")
    try:
        _live(conn, tenant)
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
        q_listing, q_version = _versioned(
            conn, tenant, status="blocked", status_reason="human_task_open"
        )
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
        # maker-checker: the owner edited this one, so the owner cannot approve it
        own = f.listing(conn, tenant, status="validated")
        made = f.version(conn, tenant, own)
        edited = f.version(
            conn, tenant, own, origin="edit", parent_version_id=made,
            changed_paths=["content.title"], edited_by_user_id=owner,
        )  # fmt: skip
        conn.execute("UPDATE listings SET current_version_id = %s WHERE id = %s", [edited, own])
        # ... and generated this one, which another member then fully regenerated: approvable
        redone = f.listing(conn, tenant, status="validated")
        first = f.version(conn, tenant, redone, generated_by_user_id=owner)
        again = f.version(conn, tenant, redone, origin="regeneration", parent_version_id=first)
        conn.execute("UPDATE listings SET current_version_id = %s WHERE id = %s", [again, redone])
        # a question that holds nothing back
        f.human_task(conn, tenant)
        # a fix Kuinn made itself, the newest action
        fixed = _live(conn, tenant)
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
        tenant, blocked, blocked_product, issue, q_listing, question, failed, str(title), own
    )
