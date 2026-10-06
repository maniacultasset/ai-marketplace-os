"""0004/01_transitions (DATABASE.md §6.1, :1376-1400): `listing_status_transitions` and its 63-row
seed, plus `ck_listings_last_event`. Every expected value here is written from DATABASE.md, never
read back from the SQL file."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from . import factories as f
from .conftest import expect_sqlstate
from .integrity_support import as_app_rw

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

STATUSES = (
    "created",
    "generating",
    "validated",
    "needs_review",
    "approved",
    "publishing",
    "published",
    "blocked",
    "failed",
)
# DATABASE.md:1380-1398, one row per (from, to, event) and its actor.
SEED: tuple[tuple[str, str, str, str], ...] = (
    ("created", "generating", "generate", "user"),
    ("generating", "validated", "generation_clean", "worker"),
    ("generating", "needs_review", "generation_warnings", "worker"),
    ("generating", "blocked", "corrections_exhausted", "worker"),
    ("generating", "blocked", "human_task_opened", "worker"),
    ("generating", "failed", "generation_failed", "worker"),
    ("validated", "blocked", "human_task_opened", "any"),
    ("needs_review", "blocked", "human_task_opened", "any"),
    ("approved", "blocked", "human_task_opened", "any"),
    ("failed", "blocked", "human_task_opened", "any"),
    ("published", "blocked", "human_task_opened", "any"),
    ("blocked", "blocked", "human_task_opened", "any"),
    ("validated", "approved", "approve", "user"),
    ("needs_review", "approved", "approve", "user"),
    ("validated", "blocked", "reject", "user"),
    ("needs_review", "blocked", "reject", "user"),
    ("validated", "generating", "reject_regenerate", "user"),
    ("needs_review", "generating", "reject_regenerate", "user"),
    ("validated", "generating", "regenerate", "user"),
    ("needs_review", "generating", "regenerate", "user"),
    ("approved", "generating", "regenerate", "user"),
    ("blocked", "generating", "regenerate", "user"),
    ("failed", "generating", "regenerate", "user"),
    ("published", "generating", "regenerate", "user"),
    ("validated", "needs_review", "edit", "user"),
    ("validated", "blocked", "edit", "user"),
    ("needs_review", "validated", "edit", "user"),
    ("needs_review", "blocked", "edit", "user"),
    ("approved", "validated", "edit", "user"),
    ("approved", "needs_review", "edit", "user"),
    ("approved", "blocked", "edit", "user"),
    ("blocked", "validated", "edit", "user"),
    ("blocked", "needs_review", "edit", "user"),
    ("failed", "validated", "edit", "user"),
    ("failed", "needs_review", "edit", "user"),
    ("failed", "blocked", "edit", "user"),
    ("published", "validated", "edit", "user"),
    ("published", "needs_review", "edit", "user"),
    ("published", "blocked", "edit", "user"),
    ("validated", "needs_review", "revalidate", "any"),
    ("validated", "blocked", "revalidate", "any"),
    ("needs_review", "validated", "revalidate", "any"),
    ("needs_review", "blocked", "revalidate", "any"),
    ("approved", "needs_review", "revalidate", "any"),
    ("approved", "blocked", "revalidate", "any"),
    ("blocked", "validated", "revalidate", "any"),
    ("blocked", "needs_review", "revalidate", "any"),
    ("publishing", "needs_review", "revalidate", "any"),
    ("publishing", "blocked", "revalidate", "any"),
    ("blocked", "generating", "task_answered", "user"),
    ("blocked", "validated", "task_answered", "user"),
    ("blocked", "needs_review", "task_answered", "user"),
    ("approved", "publishing", "publish", "user"),
    ("publishing", "published", "submission_accepted", "worker"),
    ("publishing", "failed", "submission_failed", "any"),
    ("publishing", "blocked", "submission_attempts_exhausted", "worker"),
    ("publishing", "blocked", "marketplace_action_required", "worker"),
    ("published", "failed", "marketplace_rejected", "worker"),
    ("published", "blocked", "marketplace_action_required", "worker"),
    ("published", "blocked", "sync_exhausted", "worker"),
    ("failed", "publishing", "retry_publish", "user"),
    ("failed", "publishing", "auto_resubmit", "worker"),
    ("blocked", "publishing", "resume_publish", "user"),
)
TRANSITION_EVENTS = frozenset(event for _, _, event, _ in SEED)
NO_CHANGE_EVENTS = frozenset(
    {"create", "sync", "refresh_requested", "pause", "unpause", "archive", "unarchive"}
)
VOCABULARY = TRANSITION_EVENTS | NO_CHANGE_EVENTS


def _rows(db: Conn) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT from_status, to_status, event, actor, requires_current_approval, voids_approval, "
        "allowed_reasons, requires_from_reason FROM listing_status_transitions"
    ).fetchall()


def test_transitions_seed_exact(db: Conn) -> None:
    assert len(SEED) == 63
    assert len(TRANSITION_EVENTS) == 23
    assert sorted(r[:4] for r in _rows(db)) == sorted(SEED)


def test_transitions_flags(db: Conn) -> None:
    rows = {r[:3]: r for r in _rows(db)}
    current = {k for k, r in rows.items() if r[4]}
    assert {k[2] for k in current} == {
        "publish",
        "retry_publish",
        "auto_resubmit",
        "resume_publish",
    }
    assert len(current) == 4
    voids = {k for k, r in rows.items() if r[5]}
    expected_voids = {k for k in rows if k[2] in {"regenerate", "reject_regenerate", "edit"}}
    expected_voids |= {
        ("approved", "blocked", "human_task_opened"),
        ("approved", "needs_review", "revalidate"),
        ("approved", "blocked", "revalidate"),
        ("publishing", "needs_review", "revalidate"),
        ("publishing", "blocked", "revalidate"),
    }
    assert voids == expected_voids


def test_transitions_reason_columns(db: Conn) -> None:
    rows = {r[:3]: (r[6], r[7]) for r in _rows(db)}
    for key, (allowed, _) in rows.items():
        assert (allowed is not None) == (key[1] in {"blocked", "failed"}), key
    assert rows[("blocked", "blocked", "human_task_opened")] == (
        ["human_task_open"],
        ["validation_errors"],
    )
    assert rows[("publishing", "failed", "submission_failed")][0] == [
        "publish_failed",
        "publish_partial",
    ]
    assert rows[("failed", "publishing", "retry_publish")][1] == [
        "publish_failed",
        "publish_partial",
        "marketplace_rejected",
    ]
    assert rows[("blocked", "publishing", "resume_publish")][1] == ["marketplace_action_required"]
    for to in ("generating", "validated", "needs_review"):
        assert rows[("blocked", to, "task_answered")][1] == ["human_task_open"]
    with_from = {k for k, (_, need) in rows.items() if need is not None}
    assert len(with_from) == 6
    for wanted in ("edit", "revalidate"):
        for (src, to, event), (allowed, _) in rows.items():
            if event == wanted and to == "blocked":
                assert allowed == ["validation_errors"], (src, to, event)


def test_transitions_checks(db: Conn) -> None:
    insert = (
        "INSERT INTO listing_status_transitions (from_status, to_status, event, actor, "
        "allowed_reasons) VALUES (%s, %s, %s, %s, %s)"
    )
    expect_sqlstate(db, "23514", insert, ("created", "created", "edit", "user", None))
    expect_sqlstate(db, "23514", insert, ("created", "blocked", "edit", "user", None))
    expect_sqlstate(db, "23514", insert, ("created", "validated", "edit", "user", ["x"]))
    expect_sqlstate(db, "23514", insert, ("created", "validated", "nope", "user", None))
    expect_sqlstate(db, "23514", insert, ("created", "validated", "edit", "admin", None))
    expect_sqlstate(db, "23514", insert, ("draft", "validated", "edit", "user", None))


def test_transitions_event_vocab_exact(db: Conn) -> None:
    definition = db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_listing_status_transitions_event'"
    ).fetchone()
    assert definition is not None
    expected = {
        "generate",
        "generation_clean",
        "generation_warnings",
        "corrections_exhausted",
        "human_task_opened",
        "generation_failed",
        "approve",
        "reject",
        "reject_regenerate",
        "regenerate",
        "edit",
        "revalidate",
        "task_answered",
        "publish",
        "submission_accepted",
        "submission_failed",
        "submission_attempts_exhausted",
        "marketplace_action_required",
        "marketplace_rejected",
        "sync_exhausted",
        "retry_publish",
        "auto_resubmit",
        "resume_publish",
        "create",
        "sync",
        "refresh_requested",
        "pause",
        "unpause",
        "archive",
        "unarchive",
    }
    assert len(expected) == 30
    assert expected == VOCABULARY
    for event in expected:
        assert f"'{event}'::text" in str(definition[0]), event


def test_listings_last_event_check(db: Conn) -> None:
    tenant = f.tenant(db)
    expect_sqlstate(
        db,
        "23514",
        "INSERT INTO listings (tenant_id, product_id, marketplace, category, last_event) "
        "VALUES (%s, %s, 'amazon', 't_shirts', 'teleport')",
        (tenant, f.product(db, tenant)),
        constraint="ck_listings_last_event",
    )
    listing = f.listing(db, tenant)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE listings SET last_event = 'teleport' WHERE id = %s",
        (listing,),
        constraint="ck_listings_last_event",
    )
    for event in sorted(VOCABULARY):
        with db.transaction():
            db.execute("UPDATE listings SET last_event = %s WHERE id = %s", (event, listing))


def test_last_event_vocab_matches_transitions(db: Conn) -> None:
    tenant = f.tenant(db)
    listing = f.listing(db, tenant)
    seeded = {
        str(r[0]) for r in db.execute("SELECT DISTINCT event FROM listing_status_transitions")
    }
    assert seeded == TRANSITION_EVENTS
    for event in seeded:
        with db.transaction():
            db.execute("UPDATE listings SET last_event = %s WHERE id = %s", (event, listing))
    assert VOCABULARY - seeded == NO_CHANGE_EVENTS


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO listing_status_transitions (from_status, to_status, event, actor) "
        "VALUES ('created', 'validated', 'edit', 'user')",
        "UPDATE listing_status_transitions SET actor = 'any'",
        "DELETE FROM listing_status_transitions",
        "TRUNCATE listing_status_transitions",
    ],
)
def test_transitions_app_rw_select_only(db: Conn, statement: str) -> None:
    with as_app_rw(db):
        assert db.execute("SELECT count(*) FROM listing_status_transitions").fetchone() == (63,)
        expect_sqlstate(db, "42501", statement)


def test_transitions_no_rls(db: Conn) -> None:
    row = db.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE oid = 'public.listing_status_transitions'::regclass"
    ).fetchone()
    assert row == (False, False)
    grants = {
        str(r[0])
        for r in db.execute(
            "SELECT grantee FROM information_schema.table_privileges "
            "WHERE table_name = 'listing_status_transitions' AND privilege_type = 'SELECT'"
        )
    }
    assert {"app_rw", "app_ro", "app_platform_ro"} <= grants
