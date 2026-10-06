"""`listings` and `listing_versions` (DATABASE.md §5.3; Site [06]/[85]/[91]; KI-A, KI-B): the 9
statuses, the status/reason/published/live CHECKs, maker-checker, the composite tenant FKs and the
DEFERRABLE version pointers."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest

from . import factories as f
from .integrity_support import disable_0004_guard

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

STATUSES = [
    "created",
    "generating",
    "validated",
    "needs_review",
    "approved",
    "publishing",
    "published",
    "blocked",
    "failed",
]
LIVE = {
    "live_confirmed_at": "2026-10-05",
    "last_synced_at": "2026-10-05",
}


def _published(db: Conn, tenant_id: UUID, **columns: Any) -> UUID:
    """A listing with a version, then moved to `published` with what ck_published needs."""
    listing_id = f.listing(db, tenant_id)
    version_id = f.version(db, tenant_id, listing_id)
    assignments = {
        "status": "published",
        "current_version_id": version_id,
        "published_version_id": version_id,
        "first_published_at": "2026-10-05",
        "last_published_at": "2026-10-05",
        "marketplace_state": "processing",
        **columns,
    }
    sets = ", ".join(f"{name} = %s" for name in assignments)
    # 0004's status guard allows `published` only through its transition table; this CHECK
    # fixture places the row directly (user triggers skipped for the one set-up statement).
    db.execute("SET LOCAL session_replication_role = replica")
    db.execute(
        f"UPDATE listings SET {sets} WHERE id = %s",  # noqa: S608 - fixed column names
        (*assignments.values(), listing_id),
    )
    db.execute("SET LOCAL session_replication_role = origin")
    return listing_id


# ---- listings ----


@pytest.mark.parametrize("status", STATUSES)
def test_vocab_checks_listings_status(db: Conn, status: str) -> None:
    tenant_id = f.tenant(db)
    if status == "published":
        _published(db, tenant_id)
        return
    reason = {"blocked": "rejected", "failed": "generation_failed"}.get(status)
    f.listing(db, tenant_id, status=status, status_reason=reason)


@pytest.mark.parametrize("status", ["Draft", "rejected", "CREATED", "live"])
def test_vocab_checks_listings_status_rejects(db: Conn, status: str) -> None:
    tenant_id = f.tenant(db)
    f.expect_violation(
        db, "23514", "ck_listings_status", lambda: f.listing(db, tenant_id, status=status)
    )


def test_listings_defaults(db: Conn) -> None:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    row = db.execute(
        "SELECT status, status_reason, last_event, marketplace_state, sync_poll_count, "
        "last_event_at = now(), status_changed_at = now() FROM listings WHERE id = %s",
        (listing_id,),
    ).fetchone()
    assert row == ("created", None, "create", "not_submitted", 0, True, True)


def test_listings_one_per_product_and_marketplace(db: Conn) -> None:
    tenant_id = f.tenant(db)
    product_id = f.product(db, tenant_id)
    f.listing(db, tenant_id, product_id)
    f.expect_violation(
        db,
        "23505",
        "uq_listings_product_id_marketplace",
        lambda: f.listing(db, tenant_id, product_id),
    )
    f.listing(db, tenant_id, product_id, marketplace="flipkart")


LISTING_REJECTS: dict[str, tuple[dict[str, Any], str]] = {
    "blocked without reason": ({"status": "blocked"}, "ck_status_reason"),
    "blocked with a failed reason": (
        {"status": "blocked", "status_reason": "publish_failed"},
        "ck_status_reason",
    ),
    "failed with a blocked reason": (
        {"status": "failed", "status_reason": "rejected"},
        "ck_status_reason",
    ),
    "reason on created": ({"status_reason": "rejected"}, "ck_status_reason"),
    "published without version": ({"status": "published"}, "ck_published"),
    "live without sync": ({"marketplace_state": "live"}, "ck_live_minimum"),
    "partially_live without confirmation": (
        {"marketplace_state": "partially_live", "last_synced_at": "2026-10-05"},
        "ck_live_minimum",
    ),
    "marketplace_state": ({"marketplace_state": "listed"}, "ck_listings_marketplace_state"),
    "marketplace": ({"marketplace": "myntra"}, "ck_listings_marketplace"),
    "last before first published": (
        {"first_published_at": "2026-10-05", "last_published_at": "2026-10-04"},
        "ck_listings_published_at",
    ),
    "last without first published": (
        {"last_published_at": "2026-10-04"},
        "ck_listings_published_at",
    ),
    "sync_poll_count 10": ({"sync_poll_count": 10}, "ck_listings_sync_poll_count"),
    "sync_poll_count -1": ({"sync_poll_count": -1}, "ck_listings_sync_poll_count"),
    "paused and archived": (
        {"paused_at": "2026-10-05", "archived_at": "2026-10-05"},
        "ck_listings_pause_archive",
    ),
    "paused while generating": (
        {"status": "generating", "paused_at": "2026-10-05"},
        "ck_listings_pause_archive",
    ),
    "archived while publishing": (
        {"status": "publishing", "archived_at": "2026-10-05"},
        "ck_listings_pause_archive",
    ),
}


@pytest.mark.parametrize("case", sorted(LISTING_REJECTS))
def test_checks_reject_listings(db: Conn, case: str) -> None:
    columns, constraint = LISTING_REJECTS[case]
    tenant_id = f.tenant(db)
    f.expect_violation(db, "23514", constraint, lambda: f.listing(db, tenant_id, **columns))


@pytest.mark.parametrize("column", ["paused", "archived"])
def test_checks_reject_listings_by_user_without_at(db: Conn, column: str) -> None:
    tenant_id = f.tenant(db)
    user_id = f.member(db, tenant_id)
    f.expect_violation(
        db,
        "23514",
        "ck_listings_pause_archive",
        lambda: f.listing(db, tenant_id, **{f"{column}_by_user_id": user_id}),
    )
    accepted: dict[str, Any] = {f"{column}_by_user_id": user_id, f"{column}_at": "2026-10-05"}
    f.listing(db, tenant_id, **accepted)


def test_checks_accept_listings(db: Conn) -> None:
    tenant_id = f.tenant(db)
    for reason in (
        "corrections_exhausted",
        "validation_errors",
        "rejected",
        "human_task_open",
        "marketplace_action_required",
        "publish_attempts_exhausted",
    ):
        f.listing(db, tenant_id, status="blocked", status_reason=reason)
    for reason in (
        "generation_failed",
        "publish_failed",
        "publish_partial",
        "marketplace_rejected",
    ):
        f.listing(db, tenant_id, status="failed", status_reason=reason)
    f.listing(db, tenant_id, paused_at="2026-10-05")  # system pause: no user
    listing_id = _published(db, tenant_id)
    version_id = db.execute(
        "SELECT current_version_id FROM listings WHERE id = %s", (listing_id,)
    ).fetchone()
    assert version_id is not None
    db.execute(
        "UPDATE listings SET marketplace_state = 'live', live_version_id = %s, "
        "live_confirmed_at = now(), last_synced_at = now() WHERE id = %s",
        (version_id[0], listing_id),
    )


LIVE_URL_CASES = {
    "javascript": ("javascript:alert(1)", False),
    "http": ("http://www.amazon.in/dp/B0TEST", False),
    "whitespace": ("https://www.amazon.in/dp/B0 TEST", False),
    "2049": ("https://" + "a" * 2041, False),
    "2048": ("https://" + "a" * 2040, True),
}


@pytest.mark.parametrize("case", sorted(LIVE_URL_CASES))
def test_checks_reject_bounds_listings(db: Conn, case: str) -> None:
    url, ok = LIVE_URL_CASES[case]
    tenant_id = f.tenant(db)
    if ok:
        assert len(url) == 2048
        f.listing(db, tenant_id, live_url=url)
    else:
        f.expect_violation(
            db, "23514", "ck_listings_live_url", lambda: f.listing(db, tenant_id, live_url=url)
        )


def test_listing_account_must_match_marketplace(db: Conn) -> None:
    tenant_id = f.tenant(db)
    flipkart = f.marketplace_account(db, tenant_id, marketplace="flipkart")
    f.expect_violation(
        db,
        "23503",
        "fk_listings_marketplace_account_id_marketplace",
        lambda: f.listing(db, tenant_id, marketplace_account_id=flipkart),
    )
    f.listing(db, tenant_id, marketplace="flipkart", marketplace_account_id=flipkart)


# ---- listing_versions ----


def test_listing_version_tenant_fk(db: Conn) -> None:
    """Done-when 2: a version whose tenant differs from its listing's fails the composite FK."""
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    listing_a = f.listing(db, tenant_a)
    f.expect_violation(
        db,
        "23503",
        "fk_listing_versions_listing_id",
        lambda: f.version(db, tenant_b, listing_a),
    )
    f.version(db, tenant_a, listing_a)


def test_listing_versions_keys(db: Conn) -> None:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    f.version(db, tenant_id, listing_id, version=1)
    f.expect_violation(
        db,
        "23505",
        "listing_versions_listing_version_idx",
        lambda: f.version(db, tenant_id, listing_id, version=1),
    )
    row = db.execute(
        "SELECT locale, attempts_used, image_reports_json FROM listing_versions "
        "WHERE listing_id = %s",
        (listing_id,),
    ).fetchone()
    assert row == ("en-IN", 0, [])


def _versions(db: Conn) -> tuple[UUID, UUID, UUID, UUID]:
    tenant_id = f.tenant(db)
    listing_id = f.listing(db, tenant_id)
    maker, editor = f.member(db, tenant_id), f.member(db, tenant_id)
    return tenant_id, listing_id, maker, editor


VERSION_REJECTS: dict[str, tuple[Callable[[UUID, UUID], dict[str, Any]], str]] = {
    "approver = generator": (
        lambda maker, editor: {"approved_by_user_id": maker, "approved_at": "2026-10-05"},
        "ck_maker_checker",
    ),
    "approver = editor": (
        lambda maker, editor: {
            "edited_by_user_id": editor,
            "approved_by_user_id": editor,
            "approved_at": "2026-10-05",
        },
        "ck_maker_checker",
    ),
    "approved_by without approved_at": (
        lambda maker, editor: {"approved_by_user_id": editor},
        "ck_approval_ts",
    ),
    "approved_at without approved_by": (
        lambda maker, editor: {"approved_at": "2026-10-05"},
        "ck_approval_ts",
    ),
    "origin": (lambda maker, editor: {"origin": "manual"}, "ck_listing_versions_origin"),
    "edit without changed_paths": (
        lambda maker, editor: {"origin": "edit", "edited_by_user_id": editor},
        "ck_listing_versions_changed_paths",
    ),
    "edit with empty changed_paths": (
        lambda maker, editor: {"origin": "edit", "changed_paths": []},
        "ck_listing_versions_changed_paths",
    ),
    "changed_paths on a generation": (
        lambda maker, editor: {"changed_paths": ["content.title"]},
        "ck_listing_versions_changed_paths",
    ),
    "locale": (lambda maker, editor: {"locale": "en_IN"}, "ck_listing_versions_locale"),
    "attempts_used 4": (
        lambda maker, editor: {"attempts_used": 4},
        "ck_listing_versions_attempts_used",
    ),
    "version 0": (lambda maker, editor: {"version": 0}, "ck_listing_versions_version"),
    "content not an object": (
        lambda maker, editor: {"content_json": psycopg.types.json.Jsonb([])},
        "ck_listing_versions_content_json",
    ),
}


@pytest.mark.parametrize("case", sorted(VERSION_REJECTS))
def test_checks_reject_versions(db: Conn, case: str) -> None:
    build, constraint = VERSION_REJECTS[case]
    tenant_id, listing_id, maker, editor = _versions(db)
    disable_0004_guard(db, "listing_versions", "lv_guard")  # it refuses any approval on INSERT
    columns = {"generated_by_user_id": maker, **build(maker, editor)}
    f.expect_violation(
        db, "23514", constraint, lambda: f.version(db, tenant_id, listing_id, **columns)
    )


def test_checks_accept_versions(db: Conn) -> None:
    """A distinct approver with no editor, a distinct approver beside an editor, an edit with
    its paths, a full regeneration (NULL paths)."""
    tenant_id, listing_id, maker, editor = _versions(db)
    disable_0004_guard(db, "listing_versions", "lv_guard")  # it refuses any approval on INSERT
    checker = f.member(db, tenant_id, role="reviewer")
    approved = {"approved_by_user_id": checker, "approved_at": "2026-10-05"}
    f.version(db, tenant_id, listing_id, generated_by_user_id=maker, **approved)
    f.version(
        db, tenant_id, listing_id, generated_by_user_id=maker, edited_by_user_id=editor, **approved
    )
    f.version(
        db,
        tenant_id,
        listing_id,
        generated_by_user_id=maker,
        origin="edit",
        edited_by_user_id=editor,
        changed_paths=["content.title"],
    )
    f.version(db, tenant_id, listing_id, generated_by_user_id=maker, origin="regeneration")


def test_parent_version_same_listing(db: Conn) -> None:
    tenant_id = f.tenant(db)
    listing_a, listing_b = f.listing(db, tenant_id), f.listing(db, tenant_id)
    parent_b = f.version(db, tenant_id, listing_b)
    f.expect_violation(
        db,
        "23503",
        "fk_listing_versions_parent_version_id",
        lambda: f.version(db, tenant_id, listing_a, parent_version_id=parent_b),
    )
    parent_a = f.version(db, tenant_id, listing_a)
    f.version(db, tenant_id, listing_a, origin="regeneration", parent_version_id=parent_a)


# ---- version pointers (→ lv, DEFERRABLE INITIALLY IMMEDIATE) ----

POINTERS = ["current_version_id", "approved_version_id", "published_version_id", "live_version_id"]


@pytest.mark.parametrize("pointer", POINTERS)
def test_pointer_to_other_listings_version_rejected(db: Conn, pointer: str) -> None:
    tenant_id = f.tenant(db)
    listing_a, listing_b = f.listing(db, tenant_id), f.listing(db, tenant_id)
    version_b = f.version(db, tenant_id, listing_b)
    f.expect_violation(
        db,
        "23503",
        f"fk_listings_{pointer}",
        lambda: db.execute(
            f"UPDATE listings SET {pointer} = %s WHERE id = %s",  # noqa: S608 - fixed names
            (version_b, listing_a),
        ),
    )


def test_pointer_deferred_in_one_tx(db: Conn) -> None:
    """The listing points at a version id that does not exist yet; the version arrives later in
    the same transaction, and the deferred check passes at SET CONSTRAINTS ALL IMMEDIATE."""
    tenant_id = f.tenant(db)
    version_id = uuid.uuid4()
    with db.transaction():
        db.execute("SET CONSTRAINTS ALL DEFERRED")
        listing_id = f.listing(db, tenant_id, current_version_id=version_id)
        f.version(db, tenant_id, listing_id, id=version_id)
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    row = db.execute("SELECT current_version_id FROM listings WHERE id = %s", (listing_id,))
    assert row.fetchone() == (version_id,)


def test_pointer_deferred_dangling_rejected(db: Conn) -> None:
    tenant_id = f.tenant(db)

    def dangling() -> None:
        db.execute("SET CONSTRAINTS ALL DEFERRED")
        f.listing(db, tenant_id, current_version_id=uuid.uuid4())
        db.execute("SET CONSTRAINTS ALL IMMEDIATE")

    f.expect_violation(db, "23503", "fk_listings_current_version_id", dangling)


def test_pointers_are_deferrable_initially_immediate(db: Conn) -> None:
    rows = db.execute(
        "SELECT conname, condeferrable, condeferred FROM pg_constraint "
        "WHERE contype = 'f' AND confrelid = 'listing_versions'::regclass "
        "AND conrelid IN ('listings'::regclass, to_regclass('listing_items')) ORDER BY 1"
    ).fetchall()
    names = {f"fk_listings_{p}" for p in POINTERS}
    assert {r[0] for r in rows} >= names
    assert all(r[1:] == (True, False) for r in rows)
    other = db.execute(
        "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND condeferrable "
        "AND connamespace = 'public'::regnamespace AND NOT (confrelid = "
        "'listing_versions'::regclass AND conrelid IN ('listings'::regclass, "
        "to_regclass('listing_items')))"
    ).fetchone()
    assert other == (0,)  # only the version pointers defer (DATABASE.md §1)


# ---- cross-tenant FKs ----

Build = Callable[[Conn, UUID, UUID], None]


def _listing_product(db: Conn, a: UUID, b: UUID) -> None:
    f.listing(db, a, f.product(db, b))


def _listing_account(db: Conn, a: UUID, b: UUID) -> None:
    f.listing(db, a, marketplace_account_id=f.marketplace_account(db, b))


def _listing_paused_by(db: Conn, a: UUID, b: UUID) -> None:
    f.listing(db, a, paused_at="2026-10-05", paused_by_user_id=f.member(db, b))


def _listing_archived_by(db: Conn, a: UUID, b: UUID) -> None:
    f.listing(db, a, archived_at="2026-10-05", archived_by_user_id=f.member(db, b))


def _version_listing(db: Conn, a: UUID, b: UUID) -> None:
    f.version(db, a, f.listing(db, b))


def _version_member(column: str) -> Build:
    def build(db: Conn, a: UUID, b: UUID) -> None:
        columns: dict[str, Any] = {column: f.member(db, b)}
        if column == "approved_by_user_id":
            columns["approved_at"] = "2026-10-05"
            disable_0004_guard(db, "listing_versions", "lv_guard")  # refuses approval on INSERT
        f.version(db, a, f.listing(db, a), **columns)

    return build


CROSS_TENANT: dict[str, tuple[Build, str]] = {
    "listings.product_id": (_listing_product, "fk_listings_product_id"),
    "listings.marketplace_account_id": (_listing_account, "fk_listings_marketplace_account_id"),
    "listings.paused_by_user_id": (_listing_paused_by, "fk_listings_paused_by_user_id"),
    "listings.archived_by_user_id": (_listing_archived_by, "fk_listings_archived_by_user_id"),
    "listing_versions.listing_id": (_version_listing, "fk_listing_versions_listing_id"),
    "listing_versions.generated_by_user_id": (
        _version_member("generated_by_user_id"),
        "fk_listing_versions_generated_by_user_id",
    ),
    "listing_versions.edited_by_user_id": (
        _version_member("edited_by_user_id"),
        "fk_listing_versions_edited_by_user_id",
    ),
    "listing_versions.approved_by_user_id": (
        _version_member("approved_by_user_id"),
        "fk_listing_versions_approved_by_user_id",
    ),
}
CROSS_TENANT_FK_COLUMNS = frozenset(CROSS_TENANT)


@pytest.mark.parametrize("column", sorted(CROSS_TENANT))
def test_0002_cross_tenant_fk_listings(db: Conn, column: str) -> None:
    build, constraint = CROSS_TENANT[column]
    tenant_a, tenant_b = f.tenant(db), f.tenant(db)
    f.expect_violation(db, "23503", constraint, lambda: build(db, tenant_a, tenant_b))
