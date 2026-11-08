"""The image-edit seam (SI7 T12, ADR-0030 proposed; CANON.md "The AI seam", second entry point).

`call_edit_image(request, credentials)` looks `edit_image` up on the installed pipeline at call
time (`importlib`), so a pipeline pin without it (v0.3.0 in prod today) gives None, the candidate
ends `failed/model_failed`, and the worker still boots. Besides `app.workers.generation`, this is
the only module that reaches `mkos_pipeline` (`tests/unit/test_si7_seam_imports.py`).
"""

from __future__ import annotations

import importlib
from typing import Final

from mkos_schemas.image_edit import ImageEditRequest, ImageEditResult
from mkos_schemas.jobs import ModelCredentials

PIPELINE_MODULE: Final = "mkos_pipeline"
ENTRY_POINT: Final = "edit_image"


async def call_edit_image(
    request: ImageEditRequest, credentials: ModelCredentials
) -> ImageEditResult | None:
    """The pipeline's answer, or None when the installed pipeline has no `edit_image`. Any
    exception the pipeline raises propagates (the caller maps it)."""
    module = importlib.import_module(PIPELINE_MODULE)
    entry = getattr(module, ENTRY_POINT, None)
    if entry is None or not callable(entry):
        return None
    result: ImageEditResult = await entry(request, credentials)
    return result
