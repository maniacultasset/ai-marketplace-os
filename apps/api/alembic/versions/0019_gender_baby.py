"""gender_baby: the `baby` audience on products (SI8)

Ledger row claimed in CANON.md with the Tech Lead's OK on 2026-11-07 (V1 delegation). Expand
only, as migrator: `products.ck_products_gender` is dropped and re-added `NOT VALID` with the six
values (`men`, `women`, `unisex`, `boys`, `girls`, `baby`) under a 5 s lock timeout (a metadata
change), then validated in a trailing autocommit block (`SHARE UPDATE EXCLUSIVE`: reads and
writes go on), the 0011-0013 pattern.

Downgrade (local and CI only, RULES.md "No prod downgrade"), in one transaction: refuses while any
row has `gender = 'baby'`, then restores the five-value CHECK, validated (SI8 CP-D14).

Revision ID: 0019_gender_baby
Revises: 0015_image_public_copies
Create Date: 2026-11-07

"""

from collections.abc import Sequence
from typing import Final

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_gender_baby"
down_revision: str | Sequence[str] | None = "0015_image_public_copies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SIX: Final = "('men', 'women', 'unisex', 'boys', 'girls', 'baby')"
FIVE: Final = "('men', 'women', 'unisex', 'boys', 'girls')"

UPGRADE: Final = (
    "ALTER TABLE public.products DROP CONSTRAINT IF EXISTS ck_products_gender, "
    f"ADD CONSTRAINT ck_products_gender CHECK (gender IN {SIX}) NOT VALID"
)
VALIDATE: Final = "ALTER TABLE public.products VALIDATE CONSTRAINT ck_products_gender"
REFUSE_BABY: Final = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.products WHERE gender = 'baby') THEN
    RAISE EXCEPTION 'products hold gender baby; downgrade refused' USING ERRCODE = 'GRD01';
  END IF;
END
$$
"""
DOWNGRADE: Final = (
    "ALTER TABLE public.products DROP CONSTRAINT IF EXISTS ck_products_gender, "
    f"ADD CONSTRAINT ck_products_gender CHECK (gender IN {FIVE})"
)


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(UPGRADE)
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(VALIDATE)


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(REFUSE_BABY)
    op.execute(DOWNGRADE)
