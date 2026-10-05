"""The ORM models of revisions 0001_core_tenancy (DATABASE.md §5.1, §5.2, §5.6),
0002_listing_pipeline (§5.1-§5.7) and 0003_ops_tables (§5.1-§5.6).

Importing this package registers every table on `app.db.base.metadata`, which Alembic's
`env.py` compares against the database (`alembic check`). The DDL itself is hand-written in the
revisions: CHECK constraints live only there, because Alembic does not compare them.
"""

from app.db.models import (
    audit_brand,
    catalog,
    catalog_assets,
    facts,
    issues,
    listings,
    platform,
    rules,
    runs,
    tenancy,
)

__all__ = [
    "audit_brand",
    "catalog",
    "catalog_assets",
    "facts",
    "issues",
    "listings",
    "platform",
    "rules",
    "runs",
    "tenancy",
]
