"""The variant photo match seam (V1.1, client feedback item 9; CANON.md "The AI seam").

`call_photo_match(request, credentials)` looks `photo_match` up on the installed pipeline at call
time (`importlib`), so a pipeline pin without it (before mkos-pipeline 0.8.0) gives None: the check
ends `unavailable`, nothing is spent, and the worker still boots. One of the modules that reach
`mkos_pipeline` (`tests/unit/test_si7_seam_imports.py`).

The payload types are the pipeline's own (`mkos_pipeline.contracts.PhotoMatchRequest` and
`PhotoMatchResult`, schema `contracts/PhotoMatch*.schema.json` in mkos-pipeline 0.8.0), mirrored
here field for field rather than added to `mkos_schemas`: the pipeline pins an older
`mkos_schemas`, and the seam is duck-typed (the pipeline reads the request's attributes, and the
caller re-validates the answer through JSON into `PhotoMatchAnswer`).
"""

from __future__ import annotations

import importlib
from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.base import RequestId, Slug, WireModel
from mkos_schemas.errors import ErrorReport
from mkos_schemas.jobs import ContextImage, ModelCredentials
from pydantic import ConfigDict, Field, StringConstraints

PIPELINE_MODULE: Final = "mkos_pipeline"
ENTRY_POINT: Final = "photo_match"
# The pipeline's seam contract version for its own payload types.
CONTRACT_VERSION: Final = "1.1.0"
Confidence = Annotated[float, Field(ge=0, le=1)]


class PhotoMatchRequest(WireModel):
    """Does `image` show this product, in this colour? `reference` is the product's main photo (its
    design is compared, never its colour); without one, `title` names the product. `colour` is
    the variant's colour name, or None for a photo every colour shares."""

    model_config = WireModel.model_config | ConfigDict(hide_input_in_errors=True)

    schema_version: str = CONTRACT_VERSION
    tenant_id: UUID
    product_id: UUID
    job_id: UUID | None = None
    request_id: RequestId
    image: ContextImage
    reference: ContextImage | None = None
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    category: Slug | None = None
    colour: Annotated[str, StringConstraints(min_length=1, max_length=60)] | None = None


class PhotoMatchAnswer(WireModel):
    """The pipeline's `PhotoMatchResult`, re-validated (unknown keys from a newer pipeline drop)."""

    status: Literal["ok", "failed"]
    verdict: Literal["match", "mismatch", "unsure"] | None = None
    product_match: Confidence | None = None
    colour_match: Confidence | None = None
    seen_colour: Annotated[str, StringConstraints(max_length=40)] | None = None
    reason: Annotated[str, StringConstraints(max_length=160)] | None = None
    verdict_version: Annotated[str, StringConstraints(max_length=64)] = ""
    agent_runs: list[AgentRunRecord] = Field(default_factory=list)
    error: ErrorReport | None = None


async def call_photo_match(
    request: PhotoMatchRequest, credentials: ModelCredentials
) -> object | None:
    """The pipeline's answer, or None when the installed pipeline has no `photo_match`. Any
    exception the pipeline raises propagates (the caller maps it). The answer is returned as the
    pipeline built it; the caller re-validates it through JSON."""
    module = importlib.import_module(PIPELINE_MODULE)
    entry = getattr(module, ENTRY_POINT, None)
    if entry is None or not callable(entry):
        return None
    result: object = await entry(request, credentials)
    return result
