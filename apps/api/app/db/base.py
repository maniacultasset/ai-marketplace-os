"""The one MetaData every model and Alembic's `target_metadata` share (CANON.md:93).

The naming convention is fixed before revision 0001 is written, so every generated constraint
and index name is deterministic. A CHECK constraint must be named (`%(constraint_name)s`); an
unnamed one fails at DDL time. From S6 on, the hand-written constraint names in DATABASE.md go
through `sqlalchemy.sql.elements.conv()`, which keeps them verbatim instead of prefixing them.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import MetaData, Text
from sqlalchemy.orm import DeclarativeBase, MappedColumn, mapped_column

from app.core.request_context import current_request_id

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


metadata: Final[MetaData] = Base.metadata


def request_id_column() -> MappedColumn[Any]:
    """A `request_id text` column stamped with the current request id on INSERT when the caller
    gives none (S11). Python-side only: the DDL (and so `alembic check`) is unchanged, and its
    nullability follows the `Mapped[...]` annotation. A raw SQL INSERT never sees this default,
    so these tables are written through the ORM or a Core `Table` only."""
    return mapped_column(Text, default=current_request_id)
