"""products.archived_at: "Archive product", apart from Delete (client Chrome pass 2, 12 Nov 2026)

The tester could only delete a test product: the product menu had no archive. An archived product
keeps its row, photos and history, leaves the product list (`GET /products?archived=true` lists
it), and its listings are archived with it (DATABASE.md "Pause and archive"); unarchive lifts
both. Expand only, as migrator, `lock_timeout = '5s'`: one nullable column, a catalog-only add.
The products grants are table-level, so app_rw writes it and the read roles read it as they do
every other products column. Downgrade (local and CI only, RULES.md "No prod downgrade") drops it.

Revision ID: 0039_product_archive
Revises: 0038_d5
Create Date: 2026-11-12

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0039_product_archive"
# Chained on the wave D train's head (0038_d5); a release train renumbers it.
down_revision: str | Sequence[str] | None = "0038_d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE public.products ADD COLUMN archived_at timestamptz")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE public.products DROP COLUMN archived_at")
