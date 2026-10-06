"""The 0004_integrity round trip on a scratch database through env.py (DATABASE.md §8.2, §8.3): up to
head, down to 0003b leaves no 0004 function, trigger, view, table, constraint or grant behind, and the
next upgrade rebuilds the same catalog; the full cycle passes `alembic check`."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from .conftest import ScratchDb
from .test_0002_catalog import _objects
from .test_0003_round_trip import _grants

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0003b_procrastinate"
REVISION = "0004_integrity"
FUNCTIONS_0004 = {
    "audit_allowlist",
    "audit_unknown_entity",
    "audit",
    "append_only_guard",
    "lv_guard",
    "last_owner_guard",
    "listings_status_guard",
    "listings_status_events",
    "review_bulk_backstop",
    "job_runs_guard",
    "publish_gate",
    "ls_guard",
    "li_guard",
    "listing_issues_guard",
    "human_tasks_guard",
    "variant_guard",
    "variant_matrix_guard",
    "variant_matrix_check_child",
    "variant_matrix_complete",
    "image_slot_guard",
    "rules_guard",
    "category_templates_guard",
    "error_map_guard",
    "dictionary_guard",
    "set_tenant_lifecycle",
    "expire_trials",
    "listing_display_step",
    "listing_display_status",
    "purge_tenant_order",
    "purge_tenant",
    "redact_user",
    "prune_audit_log",
    "prune_append_only",
    "prune_closed",
}


def _names(db: ScratchDb) -> dict[str, set[str]]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        functions = {
            str(r[0])
            for r in conn.execute(
                "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace"
            ).fetchall()
        }
        triggers = {
            str(r[0])
            for r in conn.execute(
                "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal"
            ).fetchall()
        }
        relations = {
            str(r[0])
            for r in conn.execute(
                "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace"
            ).fetchall()
        }
        constraints = {
            str(r[0])
            for r in conn.execute(
                "SELECT conname FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
            ).fetchall()
        }
    return {
        "functions": functions,
        "triggers": triggers,
        "relations": relations,
        "constraints": constraints,
    }


def test_0004_round_trip(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    at_previous, grants_previous = _objects(scratch_db), _grants(scratch_db)
    command.upgrade(cfg, REVISION)
    command.check(cfg)
    at_head, grants_head = _objects(scratch_db), _grants(scratch_db)
    names = _names(scratch_db)
    assert names["functions"] >= FUNCTIONS_0004
    assert {
        "listing_status_transitions",
        "listing_status_display",
        "product_status_display",
    } <= names["relations"]
    assert "ck_listings_last_event" in names["constraints"]
    command.downgrade(cfg, PREVIOUS)
    after = _names(scratch_db)
    assert not FUNCTIONS_0004 & after["functions"]
    assert (
        not {"listing_status_transitions", "listing_status_display", "product_status_display"}
        & after["relations"]
    )
    assert "ck_listings_last_event" not in after["constraints"]
    assert _objects(scratch_db) == at_previous
    assert _grants(scratch_db) == grants_previous
    command.upgrade(cfg, REVISION)
    command.check(cfg)
    assert _objects(scratch_db) == at_head
    assert _grants(scratch_db) == grants_head
