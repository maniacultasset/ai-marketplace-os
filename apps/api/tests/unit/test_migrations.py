"""app.db.migrations: the render hook, the lock, the role switch, the version-table revoke and the
newer-database check (EXECUTION-PLAN.md:300-305; DATABASE.md:1813; CANON.md:75)."""

from __future__ import annotations

import configparser
import re
from typing import Any, cast

import pytest
from alembic.autogenerate.api import AutogenContext
from alembic.migration import MigrationContext
from app.db import migrations
from app.db.migrations import (
    ALEMBIC_INI,
    APP_ROLES,
    MIGRATION_LOCK_KEY,
    MIGRATION_ROLE,
    MigrationLockBusy,
    acquire_migration_lock,
    alembic_config,
    assume_migration_role,
    render_item,
    revoke_version_table_privileges,
    safe_revision,
)
from hypothesis import given
from hypothesis import strategies as st
from pgvector.sqlalchemy import Vector
from sqlalchemy import Connection, Integer
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

SAFE = re.compile(r"[0-9A-Za-z_]{1,32}")


def _autogen() -> AutogenContext:
    ctx = MigrationContext.configure(
        dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
        opts={"sqlalchemy_module_prefix": "sa.", "user_module_prefix": None},
    )
    return AutogenContext(ctx)


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class FakeConnection:
    """Records each statement's SQL and parameters; answers scalar queries from `answers`
    (first matching substring) and raises `errors` for statements containing a key."""

    def __init__(
        self,
        answers: dict[str, object] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.answers = answers or {}
        self.errors = errors or {}
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    def execute(self, statement: object, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        self.executed.append((sql, params))
        for key, error in self.errors.items():
            if key in sql:
                raise error
        for key, value in self.answers.items():
            if key in sql:
                return _Result(value)
        return _Result(None)

    @property
    def sql(self) -> list[str]:
        return [sql for sql, _ in self.executed]


def _conn(fake: FakeConnection) -> Connection:
    return cast(Connection, fake)


class _DriverError(Exception):
    def __init__(self, sqlstate: str, message: str = "driver detail") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def _operational(sqlstate: str) -> OperationalError:
    return OperationalError("SELECT pg_advisory_lock(%(key)s)", {}, _DriverError(sqlstate))


# ---- render_item (autogenerate writes the pgvector import; BUILD-SEQUENCE.md:82 item 4) ----


def test_render_item_vector_with_dim() -> None:
    ctx = _autogen()
    assert render_item("type", Vector(1536), ctx) == "Vector(1536)"
    assert "from pgvector.sqlalchemy import Vector" in ctx.imports


def test_render_item_vector_without_dim() -> None:
    ctx = _autogen()
    assert render_item("type", Vector(), ctx) == "Vector()"
    assert "from pgvector.sqlalchemy import Vector" in ctx.imports


def test_render_item_leaves_other_types() -> None:
    ctx = _autogen()
    assert render_item("type", Integer(), ctx) is False
    assert render_item("column", Vector(1536), ctx) is False
    assert not ctx.imports


# ---- the lock ----


def test_lock_key_is_bigint_outside_int4() -> None:
    assert int.from_bytes(b"mkosmigr", "big") == MIGRATION_LOCK_KEY
    assert 2**31 <= MIGRATION_LOCK_KEY < 2**63


def test_acquire_migration_lock_order() -> None:
    fake = FakeConnection()
    acquire_migration_lock(_conn(fake))
    assert fake.sql[0] == "SET lock_timeout = '5s'"
    assert "pg_advisory_lock(" in fake.sql[1]
    assert fake.executed[1][1] == {"key": MIGRATION_LOCK_KEY}
    assert len(fake.executed) == 2


def test_acquire_migration_lock_busy_is_migration_lock_busy() -> None:
    fake = FakeConnection(errors={"pg_advisory_lock": _operational("55P03")})
    with pytest.raises(MigrationLockBusy) as info:
        acquire_migration_lock(_conn(fake))
    assert str(info.value) == "another migration holds the migration lock"
    assert isinstance(info.value.__cause__, OperationalError)


@pytest.mark.parametrize("sqlstate", ["28P01", "57014"])
def test_acquire_migration_lock_other_errors_propagate(sqlstate: str) -> None:
    fake = FakeConnection(errors={"pg_advisory_lock": _operational(sqlstate)})
    with pytest.raises(OperationalError) as info:
        acquire_migration_lock(_conn(fake))
    assert not isinstance(info.value, MigrationLockBusy)


def test_acquire_migration_lock_timeout_on_set_propagates() -> None:
    # Only the lock statement's 55P03 means "another migration holds the lock".
    fake = FakeConnection(errors={"SET lock_timeout": _operational("55P03")})
    with pytest.raises(OperationalError):
        acquire_migration_lock(_conn(fake))


# ---- safe_revision ----


@pytest.mark.parametrize("value", ["0001", "a1b2c3d4e5f6", "0001_core_tenancy", "x" * 32])
def test_safe_revision_keeps_alembic_ids(value: str) -> None:
    assert safe_revision(value) == value


@pytest.mark.parametrize(
    "value", ["x'; drop", "0001\n", "a\nb", "x" * 33, "", "\x1b[31mred", "rev id"]
)
def test_safe_revision_replaces_hostile_text(value: str) -> None:
    assert safe_revision(value) == "<invalid revision id>"


@given(st.text())
def test_safe_revision_output_is_always_safe(value: str) -> None:
    out = safe_revision(value)
    assert out == "<invalid revision id>" or SAFE.fullmatch(out)
    assert out == value or out == "<invalid revision id>"


# ---- the config ----


def test_alembic_config_points_at_repo_files() -> None:
    assert ALEMBIC_INI.is_file()
    parser = configparser.ConfigParser()
    parser.read(ALEMBIC_INI, encoding="utf-8")
    assert parser.has_option("alembic", "script_location")
    cfg = alembic_config()
    location = cfg.get_main_option("script_location")
    assert location is not None
    assert location.replace("\\", "/").endswith("apps/api/alembic")
    assert (ALEMBIC_INI.parent / "alembic" / "env.py").is_file()


# ---- the role switch and the version-table revoke (CANON.md:75; alembic_version privileges) ----


def test_migration_roles() -> None:
    assert MIGRATION_ROLE == "migrator"
    assert APP_ROLES == ("app_rw", "app_ro", "app_platform_ro")


def test_assume_migration_role_noop_as_migrator() -> None:
    fake = FakeConnection(answers={"current_user": "migrator"})
    assume_migration_role(_conn(fake))
    assert fake.sql == ["SELECT current_user"]


def test_assume_migration_role_sets_role_otherwise() -> None:
    fake = FakeConnection(answers={"current_user": "postgres"})
    assume_migration_role(_conn(fake))
    assert fake.sql == ["SELECT current_user", "SET ROLE migrator"]


def test_revoke_version_table_privileges_only_when_table_exists() -> None:
    absent = FakeConnection(answers={"to_regclass": None})
    revoke_version_table_privileges(_conn(absent))
    assert len(absent.sql) == 1
    assert "to_regclass('alembic_version')" in absent.sql[0]

    present = FakeConnection(answers={"to_regclass": "alembic_version"})
    revoke_version_table_privileges(_conn(present))
    assert present.sql[1:] == [
        "REVOKE ALL ON TABLE alembic_version FROM app_rw, app_ro, app_platform_ro, PUBLIC"
    ]


def test_module_exports_errors_without_connection_detail() -> None:
    error = migrations.DatabaseAheadError(safe_revision("ffffffffffff"))
    assert error.revision == "ffffffffffff"
    assert str(error) == ("database is at revision ffffffffffff, which this code does not contain")
