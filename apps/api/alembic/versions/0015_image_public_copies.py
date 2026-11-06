"""image_public_copies: the clean derivative and the public copy of an image (SI1, ADR-0026)

Ledger row claimed in CANON.md with the Tech Lead's OK on 2026-11-06. Expand only, as migrator, in
one transaction (`sql/0015/01_*`):

1. `images` gains `clean_content_hash`, `clean_mime_type` (the EXIF-free copy in the private
   bucket), `public_id` (26 lowercase base32 characters, unique across tenants) and
   `public_written_at` (the public object exists), with their CHECKs. A deleted row may keep
   `public_id` as a tombstone (CP16).
2. `images_public_delete_guard`: a `BEFORE DELETE` row trigger refuses (GRD01) while `public_id`
   is set, so a tenant purge runs only after the public copies are deleted (CP9).

Downgrade (local and CI only, RULES.md "No prod downgrade") drops the trigger, its function, the
index, the constraints and the columns.

Revision ID: 0015_image_public_copies
Revises: 0014_product_type_definitions
Create Date: 2026-11-06

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_image_public_copies"
down_revision: str | Sequence[str] | None = "0014_product_type_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0015"


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0014), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_images_public.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    _run_sql("01_images_public.down.sql")
