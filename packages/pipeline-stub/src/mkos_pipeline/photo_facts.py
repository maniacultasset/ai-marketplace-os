"""The stub `read_photo_facts` (V1.1, read the photos on upload): deterministic, no clock, no I/O.

It answers as the listing stub's `photo_ok` scenario does: every `fill` target (never
`number_of_pieces`) at its first allowed value with confidence 0.95, citing the first photo sent,
with one `image`/`vision` record (`photo_facts/v1`, USD 0.004, id `uuid5(job_id, "photo:<n>")`),
so the backend's screening and storing run end to end with no model. The request carries no
product text, so the answer depends on the targets only.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import uuid5

from mkos_schemas.agent import AgentRunRecord, TokenUsage
from mkos_schemas.jobs import ModelCredentials, PhotoObservation
from mkos_schemas.photo_facts_read import PhotoFactsReadRequest, PhotoFactsReadResult

PROMPT_VERSION: Final = "photo_facts/v1"
STUB_CONFIDENCE: Final = 0.95
STUB_COST: Final = Decimal("0.004000")
STUB_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)


def _record(request: PhotoFactsReadRequest, model: str) -> AgentRunRecord:
    ids = ",".join(str(image.image_id) for image in request.images)
    digest = hashlib.sha256(f"{PROMPT_VERSION}{ids}".encode()).hexdigest()
    return AgentRunRecord(
        id=uuid5(request.job_id, f"photo:{request.attempt}"),
        tenant_id=request.tenant_id,
        product_id=request.product_id,
        request_id=request.request_id,
        job_id=request.job_id,
        agent_name="image",
        trigger="vision",
        attempt=request.attempt,
        model_used=model,
        prompt_version=PROMPT_VERSION,
        tokens=TokenUsage(),
        cost_usd=STUB_COST,
        status="completed",
        input_digest=digest,
        started_at=STUB_EPOCH,
        created_at=STUB_EPOCH,
    )


async def read_photo_facts(
    request: PhotoFactsReadRequest, credentials: ModelCredentials
) -> PhotoFactsReadResult:
    first = request.images[0].image_id
    observations = [
        PhotoObservation(
            key=target.key,
            value=target.allowed_values[0],
            confidence=STUB_CONFIDENCE,
            image_id=first,
        )
        for target in request.targets
        if target.mode == "fill" and target.key != "number_of_pieces"
    ]
    return PhotoFactsReadResult(
        observations=observations, agent_runs=[_record(request, credentials.model)]
    )
