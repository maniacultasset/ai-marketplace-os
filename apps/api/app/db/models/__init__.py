"""The ORM models of revisions 0001_core_tenancy (DATABASE.md §5.1, §5.2, §5.6) and
0002_listing_pipeline (§5.1-§5.7).

Importing this package registers every table on `app.db.base.metadata`, which Alembic's
`env.py` compares against the database (`alembic check`). The DDL itself is hand-written in the
revisions: CHECK constraints live only there, because Alembic does not compare them.
"""

from app.db.models import catalog, catalog_assets, facts, listings, rules, tenancy

__all__ = ["catalog", "catalog_assets", "facts", "listings", "rules", "tenancy"]
