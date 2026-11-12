"""The stub `photo_match` (V1.1, client feedback item 9): deterministic, no clock, no I/O.

The real entry point is mkos-pipeline 0.8.0's (`contracts.PhotoMatchRequest` in,
`PhotoMatchResult` out); the stub reads the same request attributes and answers the same JSON
shape, with one `image`/`vision` record (`photo_match/stub-1`, USD 0.001, id
`uuid5(job_id, "photo_match")`). It answers `match` (0.95, and 0.92 for the colour when one is
sent), so local runs and tests flow end to end. Title markers drive the backend's edge cases:

- `[photo-mismatch]`: `mismatch` (product 0.93, colour 0.05, seen "red");
- `[photo-unsure]`: `unsure` (product 0.9, colour 0.5);
- `[photo-failed]`: `failed` with an error report and a failed record;
- `[photo-boom]`: raises.

`LAST_REQUEST` keeps the last request so tests can assert what the backend sent.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal
from uuid import uuid5

from mkos_schemas.agent import AgentRunRecord, TokenUsage
from mkos_schemas.base import WireModel
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import ModelCredentials
from pydantic import Field

PROMPT_VERSION: Final = "photo_match/stub-1"
VERDICT_VERSION: Final = "photo-match-bands/v1"
STUB_COST: Final = Decimal("0.001000")
STUB_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
LAST_REQUEST: list[Any] = []


class PhotoMatchResult(WireModel):
    status: Literal["ok", "failed"]
    verdict: Literal["match", "mismatch", "unsure"] | None = None
    product_match: float | None = None
    colour_match: float | None = None
    seen_colour: str | None = None
    reason: str | None = None
    verdict_version: str = VERDICT_VERSION
    agent_runs: list[AgentRunRecord] = Field(default_factory=list)
    error: ErrorReport | None = None


def _record(request: Any, model: str, *, failed: bool) -> AgentRunRecord:
    ids = ",".join(
        str(image.image_id) for image in (request.reference, request.image) if image is not None
    )
    digest = hashlib.sha256(f"{PROMPT_VERSION}{ids}".encode()).hexdigest()
    return AgentRunRecord(
        id=uuid5(request.job_id, "photo_match"),
        tenant_id=request.tenant_id,
        product_id=request.product_id,
        request_id=request.request_id,
        job_id=request.job_id,
        agent_name="image",
        trigger="vision",
        attempt=1,
        model_used=model,
        prompt_version=PROMPT_VERSION,
        tokens=TokenUsage(),
        cost_usd=Decimal("0") if failed else STUB_COST,
        status="failed" if failed else "completed",
        input_digest=digest,
        started_at=STUB_EPOCH,
        created_at=STUB_EPOCH,
    )


def _failure() -> ErrorReport:
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.GENERATION_FAILED,
        retryable=False,
        errors=[
            ErrorItem(
                field="images[0]",
                error_code=ErrorCode.GENERATION_FAILED,
                message="The photo match check gave no usable answer.",
                retryable=False,
                source=ErrorSource.AI_MODEL,
                severity=Severity.ERROR,
            )
        ],
    )


async def photo_match(request: Any, credentials: ModelCredentials) -> PhotoMatchResult:
    LAST_REQUEST[:] = [request]
    title = request.title or ""
    if "[photo-boom]" in title:
        raise RuntimeError("stub photo_match failure")
    colour = request.colour is not None
    if "[photo-failed]" in title:
        return PhotoMatchResult(
            status="failed",
            agent_runs=[_record(request, credentials.model, failed=True)],
            error=_failure(),
        )
    if "[photo-mismatch]" in title:
        verdict, product, colour_score, seen = "mismatch", 0.93, 0.05, "red"
        reason = "Same design, but the photo shows another colour."
    elif "[photo-unsure]" in title:
        verdict, product, colour_score, seen = "unsure", 0.9, 0.5, "dark green"
        reason = "The colour is hard to tell in this light."
    else:
        verdict, product, colour_score, seen = "match", 0.95, 0.92, "green"
        reason = "The photo shows this product."
    return PhotoMatchResult(
        status="ok",
        verdict=verdict,
        product_match=product,
        colour_match=colour_score if colour else None,
        seen_colour=seen,
        reason=reason,
        agent_runs=[_record(request, credentials.model, failed=False)],
    )
