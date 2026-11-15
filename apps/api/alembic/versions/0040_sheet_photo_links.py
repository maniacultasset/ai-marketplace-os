"""sheet_photo_links: photos from spreadsheet links (G2)

A bulk import's `main_image` and `additional_images` links were checked and kept on the product
(`products.source_image_urls`) but never downloaded. G2 downloads them in the background and
stores each photo through the upload path. Expand only, as migrator, in one transaction with
`lock_timeout = '5s'`.

1. `sql/0040/01_photo_links`: the new `photo_links` table (one row per link per target product
   and colourway, deduped on the direct link's sha256), its CHECKs, indexes, trigger, the 0005
   RLS template, app_rw's column UPDATE grant, and the read roles' SELECT without `url` and
   `url_hash` (a link can carry a signed token).
2. `sql/0040/02_job_entity_type`: `ck_job_runs_entity_type` is the head's list (0035's, unchanged
   by 0036 to 0039) plus `photo_links` (an `image_compliance` job about one product; JobKind
   stays closed). NOT VALID, then validated.
3. `sql/0040/03_purge_order`: `purge_tenant_order()` is 0034's body with `photo_links` first.

Downgrade (local and CI only, RULES.md "No prod downgrade") refuses while a `photo_links` job
exists, then restores the 0034 purge body and the 0035 entity list and drops the table.

Revision ID: 0040_sheet_photo_links
Revises: 0039_product_archive
Create Date: 2026-11-15

"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040_sheet_photo_links"
down_revision: str | Sequence[str] | None = "0039_product_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR: Final = Path(__file__).resolve().parents[1] / "sql" / "0040"
STEPS: Final = ("01_photo_links", "02_job_entity_type", "03_purge_order")


def _run_sql(name: str) -> None:
    # Every colon is escaped (as in 0004, 0011 to 0035), so `text()` never reads one as a bind.
    op.execute(sa.text((SQL_DIR / name).read_text(encoding="utf-8").replace(":", "\\:")))


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    for step in STEPS:
        _run_sql(f"{step}.up.sql")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    # the entity list first: it refuses while a photo_links job exists, before anything is dropped
    _run_sql("02_job_entity_type.down.sql")
    _run_sql("03_purge_order.down.sql")
    _run_sql("01_photo_links.down.sql")
