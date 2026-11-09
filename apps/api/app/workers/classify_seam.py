"""The product-classification seam (SI14; CANON.md "The AI seam", fourth entry point).

`call_classify_product(request, credentials)` looks `classify_product` up on the installed pipeline
at call time (`importlib`), so a pipeline pin without it gives None: the suggestion ends
`unavailable`, nothing is spent, and the worker still boots. One of the modules that reach
`mkos_pipeline` (`tests/unit/test_si7_seam_imports.py`).
"""

from __future__ import annotations

import importlib
from typing import Final

from mkos_schemas.classify import ClassifyRequest, ClassifyResult
from mkos_schemas.jobs import ModelCredentials

PIPELINE_MODULE: Final = "mkos_pipeline"
ENTRY_POINT: Final = "classify_product"


async def call_classify_product(
    request: ClassifyRequest, credentials: ModelCredentials
) -> ClassifyResult | None:
    """The pipeline's answer, or None when the installed pipeline has no `classify_product`. Any
    exception the pipeline raises propagates (the caller maps it)."""
    module = importlib.import_module(PIPELINE_MODULE)
    entry = getattr(module, ENTRY_POINT, None)
    if entry is None or not callable(entry):
        return None
    result: ClassifyResult = await entry(request, credentials)
    return result
