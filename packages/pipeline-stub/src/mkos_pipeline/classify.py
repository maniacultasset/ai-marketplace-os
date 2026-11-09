"""The stub `classify_product` (SI14 T5): deterministic, no clock, no I/O.

It offers the first category the backend sent, at 0.92, with one `AgentRunRecord` of the shape the
real pipeline writes (agent `attribute`; trigger `vision` with photos, else `generate`; prompt
`classify/stub-1`). Title markers drive the backend's edge cases:

- `[lowconf]`: confidence 0.5 (below the 0.80 task threshold);
- `[none]`: no candidate;
- `[outside]`: one candidate the backend did not offer;
- `[bidi]`: a rationale holding a NUL and a right-to-left override;
- `[badrecord]`: the record names agent `content` (the backend must distrust it);
- `[boom]`: raises.

`LAST_REQUEST` keeps the last request so tests can assert what the backend sent.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import uuid5

from mkos_schemas.agent import AgentRunRecord, TokenUsage
from mkos_schemas.classify import (
    CLASSIFY_CONFIDENCE_TASK_THRESHOLD,
    CategoryCandidate,
    ClassifyRequest,
    ClassifyResult,
)
from mkos_schemas.jobs import ModelCredentials

PROMPT_VERSION: Final = "classify/stub-1"
STUB_CONFIDENCE: Final = 0.92
LOW_CONFIDENCE: Final = 0.5
STUB_COST: Final = Decimal("0.001")
STUB_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
OUTSIDE_CATEGORY: Final = "not_offered_category"

LAST_REQUEST: ClassifyRequest | None = None


def _record(request: ClassifyRequest, model: str) -> AgentRunRecord:
    title = request.text.title
    digest = hashlib.sha256(f"{PROMPT_VERSION}{request.product_id}{title}".encode()).hexdigest()
    return AgentRunRecord(
        id=uuid5(request.product_id, f"classify:{request.request_id}:{digest}"),
        tenant_id=request.tenant_id,
        product_id=request.product_id,
        request_id=request.request_id,
        job_id=request.job_id,
        agent_name="content" if "[badrecord]" in title else "attribute",
        trigger="vision" if request.images else "generate",
        attempt=1,
        model_used=model,
        prompt_version=PROMPT_VERSION,
        tokens=TokenUsage(),
        cost_usd=STUB_COST,
        status="completed",
        input_digest=digest,
        started_at=STUB_EPOCH,
        created_at=STUB_EPOCH,
    )


async def classify_product(
    request: ClassifyRequest, credentials: ModelCredentials
) -> ClassifyResult:
    global LAST_REQUEST
    LAST_REQUEST = request
    title = request.text.title
    if "[boom]" in title:
        raise RuntimeError("stub classify_product failure")
    record = _record(request, credentials.model)
    if "[none]" in title:
        candidates: list[CategoryCandidate] = []
    else:
        category = OUTSIDE_CATEGORY if "[outside]" in title else request.categories[0].category
        confidence = LOW_CONFIDENCE if "[lowconf]" in title else STUB_CONFIDENCE
        rationale = "The title\x00 names it ‮moc" if "[bidi]" in title else "The title fits."
        refs = ["text.title", *(f"images[{image.image_id}]" for image in request.images[:1])]
        candidates = [
            CategoryCandidate(
                category=category, confidence=confidence, rationale=rationale, evidence_refs=refs
            )
        ]
    top = candidates[0].confidence if candidates else 0.0
    return ClassifyResult(
        status="ok",
        candidates=candidates,
        needs_confirmation=top < CLASSIFY_CONFIDENCE_TASK_THRESHOLD,
        calibration_version="stub-1",
        samples=1,
        agent_runs=[record],
    )
