"""AI seam payloads for one size-chart read (SI12).

The backend sends the seller's size-chart photo as one JPEG (at most 1024 px) only when the seller
asks; the pipeline answers with the chart it read and the records of its calls. The rows stay loose
on the wire: the backend normalises them with the seller size-chart bounds and stores the result as a
suggestion on the image. It becomes a fact only when the seller saves it, so a pipeline bug can never
reach a fact.

The unit is `unknown` when the chart prints none, or when the pipeline cannot back the unit it read
with a printed line. A unit is never guessed: the seller picks `cm` or `in` before saving, and a
read unit (`cm` or `in`, with `unit_source_line`) is only pre-selected for the seller to confirm.

The seller size-chart bounds live here, so the backend (`intake_values`) and the pipeline's screening
share one source: the measurement names, the size-label length, the largest measurement and its
decimals, and the row cap.
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
    "MAX_SIZE_CHART_MEASURE",
    "MAX_SIZE_CHART_READ_ROWS",
    "MAX_SIZE_CHART_SIZE_CHARS",
    "MAX_SIZE_CHART_UNIT_LINE_CHARS",
    "SIZE_CHART_MEASURE_DECIMALS",
    "SIZE_CHART_MEASURE_KEYS",
    "SizeChartReadChart",
    "SizeChartReadRequest",
    "SizeChartReadResult",
    "SizeChartReadUnit",
]

# The seller size-chart bounds (apps/api/app/services/intake_values.py imports them): at most 40
# rows; a size label of 1..20 characters; the `size_measurements` vocabulary minus `basis`
# (attribute_dictionary.json; a backend test pins it); a measurement above 0 and at most 500 with at
# most one decimal.
MAX_SIZE_CHART_READ_ROWS: Final = 40
MAX_SIZE_CHART_SIZE_CHARS: Final = 20
SIZE_CHART_MEASURE_KEYS: Final = ("chest", "waist", "hip", "length", "shoulder", "sleeve", "inseam")
MAX_SIZE_CHART_MEASURE: Final = 500
SIZE_CHART_MEASURE_DECIMALS: Final = 1
# The printed line that backs a read unit, quoted to the seller ("Read from: ...").
MAX_SIZE_CHART_UNIT_LINE_CHARS: Final = 160

SizeChartReadUnit = Literal["cm", "in", "unknown"]


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
    """The chart as read: one row per size, keyed by measure name (for example `chest`).

    `unit` is `unknown` when no unit is printed or the read unit is not backed by a printed line;
    `unit_source_line` is that printed line when the unit is `cm` or `in` (null otherwise)."""

    unit: SizeChartReadUnit
    basis: Literal["garment", "body"]
    rows: Annotated[
        list[dict[str, str | float]],
        Field(min_length=1, max_length=MAX_SIZE_CHART_READ_ROWS),
    ]
    unit_source_line: Annotated[
        str | None, Field(min_length=1, max_length=MAX_SIZE_CHART_UNIT_LINE_CHARS)
    ] = None


class SizeChartReadResult(WireModel):
    """The pipeline's answer: a chart, or `unreadable`, plus the records of its calls."""

    schema_version: str = SCHEMA_VERSION
    chart: SizeChartReadChart | None = None
    confidence: Annotated[float | None, Field(ge=0.0, le=1.0, allow_inf_nan=False)] = None
    unreadable: bool = False
    agent_runs: Annotated[list[AgentRunRecord], Field(max_length=2)] = Field(default_factory=list)
    error: ErrorReport | None = None
