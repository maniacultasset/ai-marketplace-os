"""`ImageRepository`: a product's stored image reports (DATABASE.md:483; S14b T5, D6).

`images.image_report_json` is keyed by marketplace; `reports_for_product` reads that key for the
product's live images that have one, as wire `ImageReport`s by image id. Nothing here runs a check.
"""

from __future__ import annotations

from uuid import UUID

from mkos_schemas.images import ImageReport
from sqlalchemy import select

from app.db.models.catalog_assets import Image
from app.repositories.base import TenantScopedRepository


class ImageRepository(TenantScopedRepository[Image]):
    model = Image

    async def reports_for_product(
        self, product_id: UUID, marketplace: str
    ) -> dict[UUID, ImageReport]:
        if not isinstance(product_id, UUID):
            raise ValueError("product_id must be a UUID")
        inner = (
            self._select()
            .where(
                Image.product_id == product_id,
                Image.deleted_at.is_(None),
                Image.image_report_json.has_key(marketplace),
            )
            .cte("own_images")
        )
        stmt = self._scoped_outer(
            select(inner.c.id, inner.c.image_report_json[marketplace]).order_by(inner.c.id), inner
        )
        rows = (await self.db.execute(stmt)).all()
        return {row[0]: ImageReport.model_validate(row[1]) for row in rows}
