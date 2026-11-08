"""The stub `edit_image` (SI7 T10, CP-D7): deterministic, Pillow-free, no clock, no I/O.

It echoes the backend's square JPEG back as the "edited" image, with one `AgentRunRecord` of the
shape the real pipeline writes, so the backend's own whitening, fidelity and framing decide the
outcome: the echo of an off-white photo passes and the echo of a grey one fails `not_white`.

Scenarios through the request's `category` prefix `stub:<name>` (as the graph's
`user_instructions`):
- `stub:credit`: `failed` with the pipeline's 402 report (`BUDGET_EXCEEDED`, out of credit);
- `stub:noimage`: `failed` `GENERATION_FAILED` (the model answered without an image);
- anything else: the echo.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import uuid5

from mkos_schemas.agent import AgentRunRecord, TokenUsage
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, RetryClass, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.image_edit import ImageEditRequest, ImageEditResult
from mkos_schemas.jobs import ModelCredentials

PROMPT_VERSION: Final = "image_edit/white_bg/v1"
STUB_COST: Final = Decimal("0.039000")
STUB_TOKENS: Final = TokenUsage(input=300, output=1290)
STUB_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
STUB_PROVIDER: Final = "stub"
_PREFIX: Final = "stub:"
CREDIT_MESSAGE: Final = "The AI provider account is out of credit (HTTP 402)."
NO_IMAGE_MESSAGE: Final = "The model answered without an image."


def _scenario(request: ImageEditRequest) -> str:
    category = request.category
    return category[len(_PREFIX) :] if category.startswith(_PREFIX) else "echo"


def _record(request: ImageEditRequest, model: str, *, failed: ErrorReport | None) -> AgentRunRecord:
    digest = hashlib.sha256(f"{PROMPT_VERSION}{request.image.image_id}".encode()).hexdigest()
    return AgentRunRecord(
        id=uuid5(request.job_id, "image_edit"),
        tenant_id=request.tenant_id,
        product_id=request.product_id,
        request_id=request.request_id,
        job_id=request.job_id,
        agent_name="image",
        trigger="vision",
        attempt=request.attempt,
        model_used=model,
        prompt_version=PROMPT_VERSION,
        tokens=STUB_TOKENS if failed is None else TokenUsage(),
        cost_usd=STUB_COST if failed is None else Decimal("0"),
        status="completed" if failed is None else "failed",
        error_report=failed,
        input_digest=digest,
        started_at=STUB_EPOCH,
        created_at=STUB_EPOCH,
    )


def _report(request: ImageEditRequest, code: ErrorCode, message: str) -> ErrorReport:
    meta = ERROR_CODE_META[code]
    retryable = meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT)
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=retryable,
        attempts_used=1,
        max_attempts=1,
        errors=[
            ErrorItem(
                field="request",
                error_code=code,
                severity=Severity.ERROR,
                message=message,
                source=ErrorSource.AI_MODEL,
                retryable=retryable,
                retry_class=meta.retry_class,
            )
        ],
        request_id=request.request_id,
    )


async def edit_image(request: ImageEditRequest, credentials: ModelCredentials) -> ImageEditResult:
    scenario = _scenario(request)
    if scenario == "credit":
        error = _report(request, ErrorCode.BUDGET_EXCEEDED, CREDIT_MESSAGE)
        return ImageEditResult(status="failed", error=error)
    if scenario == "noimage":
        error = _report(request, ErrorCode.GENERATION_FAILED, NO_IMAGE_MESSAGE)
        return ImageEditResult(
            status="failed",
            error=error,
            served_provider=STUB_PROVIDER,
            agent_runs=[_record(request, credentials.model, failed=error)],
        )
    return ImageEditResult(
        status="ok",
        image_b64=request.image.data_b64,
        mime="image/jpeg",
        served_provider=STUB_PROVIDER,
        agent_runs=[_record(request, credentials.model, failed=None)],
    )
