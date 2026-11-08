"""The size-chart read seam (SI12; CANON.md "The AI seam", third entry point).

`call_read_size_chart(request, credentials)` looks `read_size_chart` up on the installed pipeline
at call time (`importlib`), so a pipeline pin without it gives None: the read ends `unavailable`,
nothing is spent, and the worker still boots. With `app.workers.generation` and
`app.workers.image_edit_seam` it is one of the three modules that reach `mkos_pipeline`
(`tests/unit/test_si7_seam_imports.py`).
"""

from __future__ import annotations

import importlib
from typing import Final

from mkos_schemas.jobs import ModelCredentials
from mkos_schemas.size_chart_read import SizeChartReadRequest, SizeChartReadResult

PIPELINE_MODULE: Final = "mkos_pipeline"
ENTRY_POINT: Final = "read_size_chart"


async def call_read_size_chart(
    request: SizeChartReadRequest, credentials: ModelCredentials
) -> SizeChartReadResult | None:
    """The pipeline's answer, or None when the installed pipeline has no `read_size_chart`. Any
    exception the pipeline raises propagates (the caller maps it)."""
    module = importlib.import_module(PIPELINE_MODULE)
    entry = getattr(module, ENTRY_POINT, None)
    if entry is None or not callable(entry):
        return None
    result: SizeChartReadResult = await entry(request, credentials)
    return result
