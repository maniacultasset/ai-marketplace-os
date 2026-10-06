"""0004/17_display (DATABASE.md §6.3 :1491-1502): the BRD display projection. The golden table runs
every (status, reason, marketplace_state, paused, archived) the listings CHECKs allow through
`listing_display_status`; the expected names come from the literal tables below."""

from __future__ import annotations

import itertools
from typing import Any

import psycopg
import pytest

from . import factories as f
from .integrity_support import as_app_rw, flow, force_state

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

DISPLAY = frozenset(
    {
        "draft",
        "understanding",
        "classifying",
        "ready_for_review",
        "ready_to_list",
        "submitting",
        "processing",
        "live",
        "partially_live",
        "human_action_required",
        "blocked",
        "error",
        "paused",
        "archived",
    }
)
# ck_status_reason: the reasons each status may carry (None = no reason)
REASONS: dict[str, tuple[str | None, ...]] = {
    "created": (None,),
    "generating": (None,),
    "validated": (None,),
    "needs_review": (None,),
    "approved": (None,),
    "publishing": (None,),
    "published": (None,),
    "blocked": (
        "corrections_exhausted",
        "validation_errors",
        "rejected",
        "human_task_open",
        "marketplace_action_required",
        "publish_attempts_exhausted",
    ),
    "failed": ("generation_failed", "publish_failed", "publish_partial", "marketplace_rejected"),
}
MARKETPLACE_STATES = (
    "not_submitted",
    "processing",
    "pending_review",
    "live",
    "partially_live",
    "inactive",
    "rejected",
    "removed",
)
# DATABASE.md §6.3, the step column of the precedence table
STEP = {
    "created": "draft",
    "generating": "understanding",
    "validated": "ready_for_review",
    "needs_review": "ready_for_review",
    "approved": "ready_to_list",
    "publishing": "submitting",
    "published": "processing",
    "blocked": "blocked",
    "failed": "error",
}
HUMAN = {"human_task_open", "marketplace_action_required"}


def _allowed() -> list[tuple[str, str | None, str, bool, bool]]:
    """Every combination ck_status_reason, ck_published and ck_listings_pause_archive allow."""
    rows = []
    for status, reasons in REASONS.items():
        for reason, state, paused, archived in itertools.product(
            reasons, MARKETPLACE_STATES, (False, True), (False, True)
        ):
            if paused and archived:
                continue
            if (paused or archived) and status in ("generating", "publishing"):
                continue
            if status == "published" and state not in (
                "processing",
                "pending_review",
                "live",
                "partially_live",
            ):
                continue
            rows.append((status, reason, state, paused, archived))
    return rows


def _expected(status: str, reason: str | None, state: str, paused: bool, archived: bool) -> str:
    if archived:
        return "archived"
    if paused:
        return "paused"
    if status == "blocked" and reason in HUMAN:
        return "human_action_required"
    if state in ("live", "partially_live"):
        return state
    return STEP[status]


def test_display_golden_table(db: Conn) -> None:
    combos = _allowed()
    assert len(combos) > 300
    got = db.execute(
        "SELECT c.s, c.r, c.m, c.p, c.a, listing_display_status(c.s, c.r, c.m, c.p, c.a) "
        "FROM unnest(%s::text[], %s::text[], %s::text[], %s::boolean[], %s::boolean[]) "
        "AS c(s, r, m, p, a)",
        [list(col) for col in zip(*combos, strict=True)],
    ).fetchall()
    assert len(got) == len(combos)
    for status, reason, state, paused, archived, display in got:
        assert display in DISPLAY
        assert display == _expected(status, reason, state, paused, archived), (
            status,
            reason,
            state,
            paused,
            archived,
        )
    assert {row[5] for row in got} == DISPLAY - {"classifying"}  # reserved: no classify node


def test_display_step(db: Conn) -> None:
    for status, step in STEP.items():
        for state in MARKETPLACE_STATES:
            row = db.execute("SELECT listing_display_step(%s, %s)", (status, state)).fetchone()
            expected = (
                state if status == "published" and state in ("live", "partially_live") else step
            )
            assert row == (expected,), (status, state)


def test_display_functions_immutable(db: Conn) -> None:
    rows = db.execute(
        "SELECT proname, provolatile FROM pg_proc "
        "WHERE proname IN ('listing_display_status', 'listing_display_step') ORDER BY 1"
    ).fetchall()
    assert rows == [("listing_display_status", "i"), ("listing_display_step", "i")]


def test_views_security_invoker(db: Conn) -> None:
    rows = db.execute(
        "SELECT relname, reloptions FROM pg_class "
        "WHERE relname IN ('listing_status_display', 'product_status_display') ORDER BY 1"
    ).fetchall()
    assert rows == [
        ("listing_status_display", ["security_invoker=true"]),
        ("product_status_display", ["security_invoker=true"]),
    ]
    with as_app_rw(db):
        db.execute("SELECT * FROM listing_status_display LIMIT 1")
        db.execute("SELECT * FROM product_status_display LIMIT 1")
    privileges = db.execute(
        "SELECT DISTINCT privilege_type FROM information_schema.table_privileges "
        "WHERE table_name IN ('listing_status_display', 'product_status_display') "
        "AND grantee = 'app_rw'"
    ).fetchall()
    assert privileges == [("SELECT",)]


def test_listing_status_display_columns(db: Conn) -> None:
    fl = flow(db)
    force_state(db, fl.listing_id, "blocked", "human_task_open")
    row = db.execute(
        "SELECT display_status, display_step, is_live, human_action_required, live_with_draft "
        "FROM listing_status_display WHERE listing_id = %s",
        (fl.listing_id,),
    ).fetchone()
    assert row == ("human_action_required", "blocked", False, True, False)


def _product_with(db: Conn, states: list[tuple[str, str | None, dict[str, Any]]]) -> Any:
    tenant = f.tenant(db)
    product = f.product(db, tenant)
    for marketplace, (status, reason, cols) in zip(("amazon", "flipkart"), states, strict=False):
        listing = f.listing(db, tenant, product, marketplace=marketplace)
        force_state(db, listing, status, reason, **cols)
    row = db.execute(
        "SELECT overall_status, open_issue_count, human_action_required FROM "
        "product_status_display WHERE product_id = %s",
        (product,),
    ).fetchone()
    assert row is not None
    return row


@pytest.mark.parametrize(
    ("states", "overall"),
    [
        ([("created", None, {}), ("validated", None, {})], "ready_for_review"),
        ([("created", None, {}), ("approved", None, {})], "ready_to_list"),
        ([("blocked", "rejected", {}), ("approved", None, {})], "blocked"),
        ([("failed", "publish_failed", {}), ("approved", None, {})], "error"),
        (
            [
                ("validated", None, {"archived_at": "2026-10-01T00:00:00Z"}),
                ("validated", None, {"archived_at": "2026-10-01T00:00:00Z"}),
            ],
            "archived",
        ),
        (
            [
                ("validated", None, {"paused_at": "2026-10-01T00:00:00Z"}),
                ("validated", None, {"archived_at": "2026-10-01T00:00:00Z"}),
            ],
            "paused",
        ),
        ([("publishing", None, {}), ("created", None, {})], "processing"),
    ],
)
def test_product_overall_status(
    db: Conn, states: list[tuple[str, str | None, dict[str, Any]]], overall: str
) -> None:
    assert _product_with(db, states)[0] == overall


def test_product_overall_status_live(db: Conn) -> None:
    fl = flow(db)
    columns: dict[str, Any] = {
        "marketplace_state": "live",
        "published_version_id": fl.v1,
        "live_version_id": fl.v1,
        "first_published_at": "2026-10-01T00:00:00Z",
        "live_confirmed_at": "2026-10-01T00:00:00Z",
        "last_synced_at": "2026-10-01T00:00:00Z",
    }
    force_state(db, fl.listing_id, "published", **columns)
    f.listing_issue(db, fl.tenant_id, fl.listing_id, severity="warning")
    row = db.execute(
        "SELECT overall_status, open_issue_count, human_action_required FROM "
        "product_status_display WHERE product_id = %s",
        (fl.product_id,),
    ).fetchone()
    assert row == ("live", 1, False)
    other = f.listing(db, fl.tenant_id, fl.product_id, marketplace="flipkart")
    force_state(db, other, "validated")
    row = db.execute(
        "SELECT overall_status FROM product_status_display WHERE product_id = %s",
        (fl.product_id,),
    ).fetchone()
    assert row == ("partially_live",)
