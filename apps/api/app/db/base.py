"""The one MetaData every model and Alembic's `target_metadata` share (CANON.md:93).

The naming convention is fixed before revision 0001 is written, so every generated constraint
and index name is deterministic. A CHECK constraint must be named (`%(constraint_name)s`); an
unnamed one fails at DDL time. From S6 on, the hand-written constraint names in DATABASE.md go
through `sqlalchemy.sql.elements.conv()`, which keeps them verbatim instead of prefixing them.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

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
