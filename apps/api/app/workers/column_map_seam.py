"""The column-mapping seam (SI14; CANON.md "The AI seam", fifth entry point).

`call_map_columns(request, credentials)` looks `map_columns` up on the installed pipeline at call
time (`importlib`), so a pipeline pin without it gives None: the leftover headers stay unmapped
for the seller, nothing is spent, and the worker still boots. One of the modules that reach
`mkos_pipeline` (`tests/unit/test_si7_seam_imports.py`).
"""

from __future__ import annotations

import importlib
from typing import Final

from mkos_schemas.columns import ColumnMapRequest, ColumnMapResult
from mkos_schemas.jobs import ModelCredentials

PIPELINE_MODULE: Final = "mkos_pipeline"
ENTRY_POINT: Final = "map_columns"


async def call_map_columns(
    request: ColumnMapRequest, credentials: ModelCredentials
) -> ColumnMapResult | None:
    """The pipeline's answer, or None when the installed pipeline has no `map_columns`. Any
    exception the pipeline raises propagates (the caller maps it)."""
    module = importlib.import_module(PIPELINE_MODULE)
    entry = getattr(module, ENTRY_POINT, None)
    if entry is None or not callable(entry):
        return None
    result: ColumnMapResult = await entry(request, credentials)
    return result
