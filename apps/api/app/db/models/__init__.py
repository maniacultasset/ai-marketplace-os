"""The ORM models of revision 0001_core_tenancy (DATABASE.md §5.1, §5.2, §5.6).

Importing this package registers every table on `app.db.base.metadata`, which Alembic's
`env.py` compares against the database (`alembic check`). The DDL itself is hand-written in the
revision: CHECK constraints live only there, because Alembic does not compare them.
"""

from app.db.models import catalog, facts, tenancy

__all__ = ["catalog", "facts", "tenancy"]
