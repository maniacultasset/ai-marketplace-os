"""Revision 0015_image_public_copies (SI1 T1, ADR-0026): the round trip, the six CHECKs, the
cross-tenant unique `public_id`, the CP16 tombstone on deleted rows and the CP9 delete guard that
refuses a tenant purge while a public copy may exist. Run in the Docker batch (`make test-db`,
`make api-tests`)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from . import factories as f
from .conftest import ScratchDb, expect_sqlstate
from .integrity_support import as_migrator, scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0014_product_type_definitions"
REVISION = "0015_image_public_copies"
NEW_COLUMNS = ("clean_content_hash", "clean_mime_type", "public_id", "public_written_at")
HASH = "a" * 64
PUBLIC_ID = "abcdefghijklmnopqrstuvwxyz"  # 26 characters of [a-z]
FORCE_DATA = "SELECT purge_tenant(%s, 'data', force => true)"


def _columns(db: ScratchDb) -> set[str]:
    with psycopg.connect(db.libpq, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'images'"
        ).fetchall()
    return {str(r[0]) for r in rows}


def _image(db: Conn, **columns: Any) -> tuple[UUID, UUID]:
    tenant = f.tenant(db)
    product = f.product(db, tenant)
    return tenant, f.image(db, tenant, product, **columns)


# ---- round trip ----


def test_upgrade_downgrade_upgrade_clean(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    assert not set(NEW_COLUMNS) & _columns(scratch_db)
    command.upgrade(cfg, REVISION)
    assert set(NEW_COLUMNS) <= _columns(scratch_db)
    command.upgrade(cfg, "head")  # 0017 (SI5) and 0019 (SI8) sit on top: `check` runs at the head
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    assert not set(NEW_COLUMNS) & _columns(scratch_db)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert (
            scalar(
                conn, "SELECT count(*) FROM pg_proc WHERE proname = 'images_refuse_public_delete'"
            )
            == 0
        )
    command.upgrade(cfg, "head")
    command.check(cfg)


# ---- CHECKs ----


@pytest.mark.parametrize(
    "bad",
    ["a" * 25, "abcdefghijklmnopqrstuvwxyZ", "abcdefghijklmnopqrstuvwxy1", "a" * 27],
)
def test_bad_public_id_refused(db: Conn, bad: str) -> None:
    _, image_id = _image(db, clean_content_hash=HASH, clean_mime_type="image/jpeg")
    expect_sqlstate(
        db,
        "23514",
        "UPDATE images SET public_id = %s WHERE id = %s",
        (bad, image_id),
        constraint="ck_images_public_id",
    )


def test_duplicate_public_id_refused_across_tenants(db: Conn) -> None:
    clean = {"clean_content_hash": HASH, "clean_mime_type": "image/jpeg"}
    _, first = _image(db, public_id=PUBLIC_ID, **clean)
    _, second = _image(db, **clean)
    expect_sqlstate(
        db,
        "23505",
        "UPDATE images SET public_id = %s WHERE id = %s",
        (PUBLIC_ID, second),
        constraint="images_public_id_uq",
    )
    assert first != second


def test_public_id_needs_clean_hash(db: Conn) -> None:
    _, image_id = _image(db)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE images SET public_id = %s WHERE id = %s",
        (PUBLIC_ID, image_id),
        constraint="ck_images_public_needs_clean",
    )


def test_tombstone_public_id_allowed_on_deleted_row(db: Conn) -> None:
    _, image_id = _image(db, clean_content_hash=HASH, clean_mime_type="image/png")
    db.execute(
        "UPDATE images SET public_id = %s, deleted_at = now() WHERE id = %s",
        (PUBLIC_ID, image_id),
    )
    # the cleanup clears the clean pair and keeps the name (CP16)
    db.execute(
        "UPDATE images SET clean_content_hash = NULL, clean_mime_type = NULL WHERE id = %s",
        (image_id,),
    )
    assert scalar(db, "SELECT public_id FROM images WHERE id = %s", (image_id,)) == PUBLIC_ID


def test_written_at_needs_public_id(db: Conn) -> None:
    _, image_id = _image(db, clean_content_hash=HASH, clean_mime_type="image/jpeg")
    expect_sqlstate(
        db,
        "23514",
        "UPDATE images SET public_written_at = now() WHERE id = %s",
        (image_id,),
        constraint="ck_images_public_written",
    )


def test_clean_pair(db: Conn) -> None:
    _, image_id = _image(db)
    expect_sqlstate(
        db,
        "23514",
        "UPDATE images SET clean_content_hash = %s WHERE id = %s",
        (HASH, image_id),
        constraint="ck_images_clean_pair",
    )
    expect_sqlstate(
        db,
        "23514",
        "UPDATE images SET clean_mime_type = 'image/jpeg' WHERE id = %s",
        (image_id,),
        constraint="ck_images_clean_pair",
    )


@pytest.mark.parametrize(
    ("assignment", "value", "constraint"),
    [
        ("clean_content_hash", "a" * 63, "ck_images_clean_content_hash"),
        ("clean_content_hash", "A" * 64, "ck_images_clean_content_hash"),
        ("clean_mime_type", "image/webp", "ck_images_clean_mime_type"),
    ],
)
def test_bad_clean_hash_and_mime(db: Conn, assignment: str, value: str, constraint: str) -> None:
    _, image_id = _image(db, clean_content_hash=HASH, clean_mime_type="image/jpeg")
    expect_sqlstate(
        db,
        "23514",
        f"UPDATE images SET {assignment} = %s WHERE id = %s",  # noqa: S608
        (value, image_id),
        constraint=constraint,
    )


# ---- CP9 delete guard ----


def test_purge_refused_while_public_id_set(db: Conn) -> None:
    tenant, image_id = _image(
        db, clean_content_hash=HASH, clean_mime_type="image/jpeg", public_id=PUBLIC_ID
    )
    with as_migrator(db):
        error = expect_sqlstate(db, "GRD01", FORCE_DATA, (tenant,))
    assert "delete the public copy first" in str(error)
    assert scalar(db, "SELECT count(*) FROM images WHERE id = %s", (image_id,)) == 1
    # the CP9 step (purge_public_images) NULLs the name once the object is gone
    db.execute(
        "UPDATE images SET public_id = NULL, public_written_at = NULL WHERE id = %s", (image_id,)
    )
    with as_migrator(db):
        db.execute(FORCE_DATA, (tenant,))
    assert scalar(db, "SELECT count(*) FROM images WHERE id = %s", (image_id,)) == 0


def test_delete_without_public_id_passes(db: Conn) -> None:
    tenant, image_id = _image(db)
    with as_migrator(db):
        db.execute(FORCE_DATA, (tenant,))
    assert scalar(db, "SELECT count(*) FROM images WHERE id = %s", (image_id,)) == 0
