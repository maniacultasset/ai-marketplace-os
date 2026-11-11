"""Revision 0030_brand_voice (client feedback item 19): the single head, the round trip of
`brand_profiles.brand_voice` and the widened audit allow-list, the CHECK, and the guarded
downgrade."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from . import factories as f
from .conftest import ScratchDb
from .integrity_support import scalar

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]
MakeConfig = Callable[[Path | None], Config]

PREVIOUS = "0029_admin_google_link"
REVISION = "0030_brand_voice"
HAS_COLUMN = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'brand_profiles' AND column_name = 'brand_voice'"
)


def test_single_head_on_0029() -> None:
    from app.db.migrations import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == [REVISION]
    rev = script.get_revision(REVISION)
    assert rev is not None and rev.down_revision == PREVIOUS


def test_upgrade_downgrade_upgrade(alembic_env: MakeConfig, scratch_db: ScratchDb) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, HAS_COLUMN) == 0
        assert scalar(conn, "SELECT audit_allowlist('brand_profile')") == ["provenance"]
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, HAS_COLUMN) == 1
        assert scalar(conn, "SELECT audit_allowlist('brand_profile')") == [
            "provenance",
            "brand_voice",
        ]
        assert scalar(conn, "SELECT audit_allowlist('tenant')") == [
            "name",
            "tone",
            "guidelines",
            "glossary",
        ]
    command.check(cfg)
    command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, HAS_COLUMN) == 0
        assert scalar(conn, "SELECT audit_allowlist('brand_profile')") == ["provenance"]
    command.upgrade(cfg, "head")
    command.check(cfg)


@pytest.mark.parametrize(
    ("voice", "ok"),
    [
        ('{"audience": "students"}', True),
        ("{}", True),
        ('["not", "an", "object"]', False),
        ('"text"', False),
        ('{"example_copy": "' + "x" * 17000 + '"}', False),
    ],
)
def test_check_refuses_a_non_object_or_oversized_voice(
    alembic_env: MakeConfig, scratch_db: ScratchDb, voice: str, ok: bool
) -> None:
    command.upgrade(alembic_env(None), REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        tenant = f.tenant(conn)
        conn.execute("INSERT INTO brand_profiles (tenant_id) VALUES (%s)", [tenant])
        conn.commit()
        statement = "UPDATE brand_profiles SET brand_voice = %s::jsonb WHERE tenant_id = %s"
        if ok:
            conn.execute(statement, [voice, tenant])
        else:
            with pytest.raises(
                psycopg.errors.CheckViolation, match="ck_brand_profiles_brand_voice"
            ):
                conn.execute(statement, [voice, tenant])


def test_downgrade_refuses_while_a_voice_is_stored(
    alembic_env: MakeConfig, scratch_db: ScratchDb
) -> None:
    cfg = alembic_env(None)
    command.upgrade(cfg, REVISION)
    with psycopg.connect(scratch_db.libpq) as conn:
        tenant = f.tenant(conn)
        conn.execute(
            "INSERT INTO brand_profiles (tenant_id, brand_voice) VALUES (%s, '{}'::jsonb)",
            [tenant],
        )
        conn.commit()
    with pytest.raises(Exception, match="0030 downgrade refused"):
        command.downgrade(cfg, PREVIOUS)
    with psycopg.connect(scratch_db.libpq, autocommit=True) as conn:
        assert scalar(conn, "SELECT version_num FROM alembic_version") == REVISION
