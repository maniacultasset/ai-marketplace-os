"""SI12: the size-chart read answer (`POST /products/{product_id}/size-chart-read`)."""

from __future__ import annotations

from uuid import UUID

from mkos_schemas.base import WireModel

from app.api.v1.schemas.products import SizeChartReadStatus


class SizeChartReadOut(WireModel):
    """202 with the job while it is queued or reading; 200 with `read` or `unreadable` when the
    current photo was already read (no job). Poll the product for `size_chart_suggestion`."""

    image_id: UUID
    status: SizeChartReadStatus
    job_id: UUID | None = None
    poll_url: str | None = None
