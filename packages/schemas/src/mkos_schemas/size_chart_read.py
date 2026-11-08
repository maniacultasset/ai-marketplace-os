"""AI seam payloads for one size-chart read (SI12).

The backend sends the seller's size-chart photo as one JPEG (at most 1024 px) only when the seller
asks; the pipeline answers with the chart it read and the records of its calls. The rows stay loose
on the wire: the backend normalises them with the seller size-chart bounds and stores the result as a
suggestion on the image. It becomes a fact only when the seller saves it, so a pipeline bug can never
reach a fact.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from .agent import AgentRunRecord
from .base import SCHEMA_VERSION, RequestId, Slug, WireModel
from .errors import ErrorReport
from .jobs import ContextImage

__all__ = [
    "MAX_SIZE_CHART_READ_ROWS",
    "SizeChartReadChart",
    "SizeChartReadRequest",
    "SizeChartReadResult",
]

# The seller size-chart cap (apps/api/app/services/intake_values.py): at most 40 rows.
MAX_SIZE_CHART_READ_ROWS: Final = 40


class SizeChartReadRequest(WireModel):
    """One read of one size-chart photo."""

    model_config = WireModel.model_config | ConfigDict(hide_input_in_errors=True)

    schema_version: str = SCHEMA_VERSION
    tenant_id: UUID
    product_id: UUID
    job_id: UUID
    request_id: RequestId
    attempt: Annotated[int, Field(ge=1, le=3)] = 1
    image: ContextImage
    category: Slug


class SizeChartReadChart(WireModel):
    """The chart as read: one row per size, keyed by measure name (for example `chest`)."""

    unit: Literal["cm", "in"]
    basis: Literal["garment", "body"]
    rows: Annotated[
        list[dict[str, str | float]],
        Field(min_length=1, max_length=MAX_SIZE_CHART_READ_ROWS),
    ]


class SizeChartReadResult(WireModel):
    """The pipeline's answer: a chart, or `unreadable`, plus the records of its calls."""

    schema_version: str = SCHEMA_VERSION
    chart: SizeChartReadChart | None = None
    confidence: Annotated[float | None, Field(ge=0.0, le=1.0, allow_inf_nan=False)] = None
    unreadable: bool = False
    agent_runs: Annotated[list[AgentRunRecord], Field(max_length=2)] = Field(default_factory=list)
    error: ErrorReport | None = None
