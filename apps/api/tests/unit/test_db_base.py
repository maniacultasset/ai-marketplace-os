"""The MetaData home and its naming convention (CANON.md:93: set before revision 0001)."""

from __future__ import annotations

import pytest
from app.db.base import NAMING_CONVENTION, Base, metadata
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql.elements import conv

EXPECTED = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# SQLAlchemy's dialect constructors are untyped.
DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]


def _ddl(table: Table) -> str:
    return str(CreateTable(table).compile(dialect=DIALECT))


def _fresh() -> MetaData:
    return MetaData(naming_convention=NAMING_CONVENTION)


def test_naming_convention_exact_strings() -> None:
    assert NAMING_CONVENTION == EXPECTED


def test_naming_convention_index() -> None:
    table = Table("t", _fresh(), Column("c", Integer, index=True))
    (index,) = table.indexes
    sql = str(CreateIndex(index).compile(dialect=DIALECT))
    assert sql.startswith("CREATE INDEX ix_t_c ON t (c)")


def test_naming_convention_unique() -> None:
    table = Table("t", _fresh(), Column("c", Integer), UniqueConstraint("c"))
    assert "CONSTRAINT uq_t_c UNIQUE (c)" in _ddl(table)


def test_naming_convention_foreign_key() -> None:
    md = _fresh()
    Table("parent", md, Column("id", Integer, primary_key=True))
    table = Table("t", md, Column("c", Integer, ForeignKey("parent.id")))
    assert "CONSTRAINT fk_t_c_parent FOREIGN KEY(c) REFERENCES parent (id)" in _ddl(table)


def test_naming_convention_primary_key() -> None:
    table = Table("t", _fresh(), Column("id", Integer, primary_key=True))
    assert "CONSTRAINT pk_t PRIMARY KEY (id)" in _ddl(table)


def test_naming_convention_named_check() -> None:
    table = Table(
        "t", _fresh(), Column("c", Integer), CheckConstraint("c > 0", name="maker_checker")
    )
    assert "CONSTRAINT ck_t_maker_checker CHECK (c > 0)" in _ddl(table)


def test_naming_convention_conv_verbatim() -> None:
    # DATABASE.md's hand-written names go through conv() and are never prefixed again.
    table = Table(
        "t", _fresh(), Column("c", Integer), CheckConstraint("c > 0", name=conv("ck_maker_checker"))
    )
    ddl = _ddl(table)
    assert "CONSTRAINT ck_maker_checker CHECK (c > 0)" in ddl
    assert "ck_t_ck_maker_checker" not in ddl


def test_naming_convention_unnamed_check_refused() -> None:
    # SQLAlchemy refuses it as soon as the constraint joins the table, before any DDL.
    with pytest.raises(InvalidRequestError, match="constraint_name"):
        _ddl(Table("t", _fresh(), Column("c", Integer), CheckConstraint("c > 0")))


def test_base_metadata_is_conventional() -> None:
    assert metadata is Base.metadata
    assert Base.metadata.naming_convention == NAMING_CONVENTION
