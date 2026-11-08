"""Revision 0020_status_truth (SI9 T1): the round trip, `ck_published` with `(published, inactive)`,
the display mapping of that pair, the `products.photo_rejections` CHECK, and the downgrade that
refuses while a `(published, inactive)` listing exists."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import as_app_rw, flow, force_state, scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0018_image_generation"
REVISION = "0020_status_truth"
V1_KEYS = (
    "neckline",
    "sleeve_length",
    "fit",
    "garment_length",
    "embroidery",
    "occasion",
    "color",
    "pattern",
    "number_of_pieces",
)


def _has_column(libpq: str) -> bool:
    with psycopg.connect(libpq, autocommit=True) as conn:
        return bool(
            scalar(
                conn,
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'products' AND column_name = 'photo_rejections'",
            )
        )


def _published(db: Conn, state: str) -> UUID:
    fl = flow(db)
    force_state(
        db,
        fl.listing_id,
        "published",
        marketplace_state=state,
        published_version_id=fl.v1,
        first_published_at="2026-11-07T00:00:00Z",
    )
    return fl.listing_id


# ---- round trip ----


def test_upgrade_downgrade_upgrade_clean(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    assert not _has_column(scratch_db.libpq)
    command.upgrade(cfg, REVISION)
    assert _has_column(scratch_db.libpq)
    command.upgrade(
        cfg, "head"
    )  # 0019 (SI8), 0021 (SI10), 0022 (SI11) sit on top: `check` at the head
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert not _has_column(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert (
            scalar(conn, "SELECT count(*) FROM pg_proc WHERE proname = 'photo_rejections_valid'")
            == 0
        )
        assert (
            scalar(
                conn, "SELECT listing_display_status('published', NULL, 'inactive', false, false)"
            )
            == "processing"
        )
    command.upgrade(cfg, "head")
    command.check(cfg)


def test_downgrade_refused_with_published_inactive(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        # the scratch database has no synced dictionary: skip the facts guard for the set-up rows
        conn.execute("SET session_replication_role = replica")
        _published(conn, "inactive")
        conn.commit()
    with pytest.raises(Exception, match="downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    assert _has_column(scratch_db.libpq)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT version_num FROM alembic_version") == REVISION


# ---- ck_published ----


def test_published_inactive_allowed(db: Conn) -> None:
    listing = _published(db, "inactive")
    assert scalar(db, "SELECT marketplace_state FROM listings WHERE id = %s", (listing,)) == (
        "inactive"
    )


@pytest.mark.parametrize("state", ["rejected", "not_submitted", "removed"])
def test_published_other_states_refused(db: Conn, state: str) -> None:
    listing = _published(db, "processing")
    expect_sqlstate(
        db,
        "23514",
        "UPDATE listings SET marketplace_state = %s WHERE id = %s",
        (state, listing),
        constraint="ck_published",
    )


# ---- display ----


def test_display_published_inactive(db: Conn) -> None:
    assert (
        scalar(db, "SELECT listing_display_status('published', NULL, 'inactive', false, false)")
        == "human_action_required"
    )
    assert (
        scalar(db, "SELECT listing_display_status('published', NULL, 'inactive', true, false)")
        == "paused"
    )
    assert (
        scalar(db, "SELECT listing_display_status('published', NULL, 'inactive', false, true)")
        == "archived"
    )
    # the step stays the plain step (CP-D10)
    assert scalar(db, "SELECT listing_display_step('published', 'inactive')") == "processing"


def test_view_human_action_required(db: Conn) -> None:
    inactive = _published(db, "inactive")
    processing = _published(db, "processing")
    rows: dict[UUID, bool] = dict(
        db.execute(
            "SELECT listing_id, human_action_required FROM listing_status_display "
            "WHERE listing_id = ANY(%s)",
            ([inactive, processing],),
        ).fetchall()
    )
    assert rows == {inactive: True, processing: False}


def test_display_grants_survive(db: Conn) -> None:
    for role in ("app_rw", "app_ro"):
        assert scalar(
            db,
            "SELECT has_function_privilege(%s, "
            "'listing_display_status(text, text, text, boolean, boolean)', 'EXECUTE')",
            (role,),
        )
    with as_app_rw(db):
        db.execute("SELECT * FROM listing_status_display LIMIT 1")


# ---- products.photo_rejections ----


def _product(db: Conn) -> UUID:
    return f.product(db, f.tenant(db))


def test_photo_rejections_default(db: Conn) -> None:
    product = _product(db)
    assert scalar(db, "SELECT photo_rejections FROM products WHERE id = %s", (product,)) == {}


@pytest.mark.parametrize(
    "good",
    [
        {"neckline": "v neck"},
        dict.fromkeys(V1_KEYS, "x"),
        {"neckline": "é" * 40},
        {"fit": "a" * 40},
    ],
)
def test_photo_rejections_accepted(db: Conn, good: dict[str, str]) -> None:
    product = _product(db)
    db.execute("UPDATE products SET photo_rejections = %s WHERE id = %s", (Jsonb(good), product))
    assert scalar(db, "SELECT photo_rejections FROM products WHERE id = %s", (product,)) == good


@pytest.mark.parametrize(
    "bad",
    [
        [],
        "x",
        None,
        {"brand": "x"},
        {"neckline": ""},
        {"neckline": "a" * 41},
        {"neckline": 3},
        {"neckline": "é" * 41},
        {"neckline": None},
    ],
)
def test_photo_rejections_refused(db: Conn, bad: Any) -> None:
    product = _product(db)
    if bad is None:
        expect_sqlstate(
            db, "23502", "UPDATE products SET photo_rejections = NULL WHERE id = %s", (product,)
        )
        return
    expect_sqlstate(
        db,
        "23514",
        "UPDATE products SET photo_rejections = %s WHERE id = %s",
        (Jsonb(bad), product),
        constraint="ck_products_photo_rejections",
    )


def test_photo_rejections_valid_never_raises(db: Conn) -> None:
    assert scalar(db, "SELECT photo_rejections_valid(NULL)") is False
    assert scalar(db, "SELECT photo_rejections_valid('{}'::jsonb)") is True
    assert scalar(db, "SELECT photo_rejections_valid('[1]'::jsonb)") is False
