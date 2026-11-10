"""The photo read on upload seam (V1.1; CANON.md "The AI seam").

`call_read_photo_facts(request, credentials)` looks `read_photo_facts` up on the installed pipeline
at call time (`importlib`), so a pipeline pin without it gives None: the read ends `unavailable`,
nothing is spent, the photos are read at the next generation as before, and the worker still
boots. One of the modules that reach `mkos_pipeline` (`tests/unit/test_si7_seam_imports.py`).
"""

from __future__ import annotations

import importlib
from typing import Final

from mkos_schemas.jobs import ModelCredentials
from mkos_schemas.photo_facts_read import PhotoFactsReadRequest, PhotoFactsReadResult

PIPELINE_MODULE: Final = "mkos_pipeline"
ENTRY_POINT: Final = "read_photo_facts"


async def call_read_photo_facts(
    request: PhotoFactsReadRequest, credentials: ModelCredentials
) -> object | None:
    """The pipeline's answer, or None when the installed pipeline has no `read_photo_facts`. Any
    exception the pipeline raises propagates (the caller maps it). The answer is returned as the
    pipeline built it; the caller re-validates it through JSON."""
    module = importlib.import_module(PIPELINE_MODULE)
    entry = getattr(module, ENTRY_POINT, None)
    if entry is None or not callable(entry):
        return None
    result: PhotoFactsReadResult = await entry(request, credentials)
    return result
