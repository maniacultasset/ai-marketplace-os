from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import GenerationContext
from mkos_schemas.pipeline import ModelCredentials
from pydantic import BaseModel, ValidationError

from mkos_pipeline.exceptions import DeadlineExceeded, EmbeddingNotAllowed, ModelCallError
from mkos_pipeline.llm.cost import UnknownModelPricing, compute_cost
from mkos_pipeline.llm.deadline import MAX_MODEL_TIMEOUT, SKIP_MODEL_WHEN, seconds_left
from mkos_pipeline.llm.records import append_record, digest_prompt
from mkos_pipeline.llm.safety import render_user_message
from mkos_pipeline.schema_compat import AgentName, AgentTrigger, ModelPurpose, TokenCounts

logger = logging.getLogger("mkos_pipeline.llm")

PROVIDER_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
}


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    status_code: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


HttpPost = Callable[[dict[str, Any], float], Awaitable[LLMResponse]]

_transport: HttpPost | None = None


def set_transport(fn: HttpPost | None):
    global _transport
    previous = _transport
    _transport = fn
    return previous


def reset_transport(previous: HttpPost | None) -> None:
    global _transport
    _transport = previous


def _failed_report(code: ErrorCode, message: str) -> ErrorReport:
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=code
        in {
            ErrorCode.JSON_INVALID,
            ErrorCode.TIMEOUT,
            ErrorCode.RATE_LIMITED,
            ErrorCode.MODEL_UNAVAILABLE,
        },
        errors=[
            ErrorItem(
                field="listing",
                error_code=code,
                message=message,
                retryable=True,
                source="ai_model",
                severity="error",
            )
        ],
    )


def _timeout_for(credentials: ModelCredentials, deadline_at: datetime) -> float:
    remaining = seconds_left(deadline_at) - SKIP_MODEL_WHEN.total_seconds()
    return max(1.0, min(float(credentials.timeout_seconds), float(MAX_MODEL_TIMEOUT), remaining))


async def _default_http_post(
    payload: dict[str, Any],
    timeout: float,
    *,
    url: str,
    api_key: str,
) -> LLMResponse:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
    if response.status_code >= 400:
        # Do not include body or headers; they can carry the key.
        raise httpx.HTTPStatusError(
            f"provider HTTP {response.status_code}",
            request=response.request,
            response=response,
        )
    body = response.json()
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = body.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return LLMResponse(
        content=message.get("content") or "",
        status_code=response.status_code,
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        cached_input_tokens=details.get("cached_tokens"),
    )


async def complete_json(
    *,
    credentials: ModelCredentials,
    purpose: Literal["generation", "vision", "embedding"] | ModelPurpose,
    prompt_version: str,
    system: str,
    developer: str,
    data: dict[str, Any],
    response_model: type[BaseModel],
    images: list[bytes] | None = None,
    deadline_at: datetime,
    agent_name: AgentName,
    trigger: AgentTrigger,
    attempt: int,
    records: list[AgentRunRecord],
    ctx: GenerationContext,
) -> BaseModel:
    del images  # Vision bytes are Developer 2's extractor. Unused in v1 generation.

    if purpose == "embedding":
        raise EmbeddingNotAllowed("complete_json must not be used for embeddings")

    user_message = render_user_message(developer, data)
    rendered = f"{system}\n\n{user_message}"
    prompt_digest = digest_prompt(rendered)

    if seconds_left(deadline_at) <= SKIP_MODEL_WHEN.total_seconds():
        append_record(
            records,
            ctx=ctx,
            agent_name=agent_name,
            trigger=trigger,
            attempt=attempt,
            status="timeout",
            cost_usd="0.000000",
            started_at=datetime.now(UTC),
            duration_ms=0,
            model_used=credentials.model,
            prompt_version=prompt_version,
            input_digest=prompt_digest,
            error_report=_failed_report(
                ErrorCode.TIMEOUT, "Deadline too close to start a model call."
            ),
        )
        raise DeadlineExceeded("deadline within 5 seconds")

    payload = {
        "model": credentials.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    base = credentials.api_base or PROVIDER_DEFAULTS[credentials.provider]
    url = f"{base.rstrip('/')}/chat/completions"
    timeout = _timeout_for(credentials, deadline_at)
    api_key = credentials.api_key.get_secret_value()

    last_error: Exception | None = None
    for http_attempt in (1, 2):
        started = datetime.now(UTC)
        try:
            transport = _transport
            if transport is not None:
                raw = await transport(payload, timeout)
            else:
                raw = await _default_http_post(payload, timeout, url=url, api_key=api_key)
        except httpx.TimeoutException as exc:
            last_error = exc
            duration = int((datetime.now(UTC) - started).total_seconds() * 1000)
            append_record(
                records,
                ctx=ctx,
                agent_name=agent_name,
                trigger=trigger,
                attempt=attempt,
                status="timeout",
                cost_usd="0.000000",
                started_at=started,
                duration_ms=duration,
                model_used=credentials.model,
                prompt_version=prompt_version,
                input_digest=prompt_digest,
                error_report=_failed_report(ErrorCode.TIMEOUT, "Model call timed out."),
            )
            if http_attempt == 1 and seconds_left(deadline_at) > SKIP_MODEL_WHEN.total_seconds():
                continue
            raise DeadlineExceeded("model call timed out") from None
        except httpx.HTTPStatusError as exc:
            last_error = exc
            duration = int((datetime.now(UTC) - started).total_seconds() * 1000)
            status_code = exc.response.status_code if exc.response is not None else 0
            retryable = status_code == 429 or status_code >= 500
            code = ErrorCode.RATE_LIMITED if status_code == 429 else ErrorCode.MODEL_UNAVAILABLE
            append_record(
                records,
                ctx=ctx,
                agent_name=agent_name,
                trigger=trigger,
                attempt=attempt,
                status="failed",
                cost_usd="0.000000",
                started_at=started,
                duration_ms=duration,
                model_used=credentials.model,
                prompt_version=prompt_version,
                input_digest=prompt_digest,
                error_report=_failed_report(code, f"Provider returned HTTP {status_code}."),
            )
            if (
                retryable
                and http_attempt == 1
                and seconds_left(deadline_at) > SKIP_MODEL_WHEN.total_seconds()
            ):
                continue
            raise ModelCallError(f"provider HTTP {status_code}") from None
        except Exception as exc:
            last_error = exc
            duration = int((datetime.now(UTC) - started).total_seconds() * 1000)
            append_record(
                records,
                ctx=ctx,
                agent_name=agent_name,
                trigger=trigger,
                attempt=attempt,
                status="failed",
                cost_usd="0.000000",
                started_at=started,
                duration_ms=duration,
                model_used=credentials.model,
                prompt_version=prompt_version,
                input_digest=prompt_digest,
                error_report=_failed_report(ErrorCode.GENERATION_FAILED, "Model transport failed."),
            )
            raise ModelCallError("model transport failed") from None

        duration = int((datetime.now(UTC) - started).total_seconds() * 1000)
        usage_present = raw.input_tokens is not None and raw.output_tokens is not None
        tokens = TokenCounts()
        cost = "0.000000"
        if usage_present:
            tokens = TokenCounts(
                input=int(raw.input_tokens or 0),
                output=int(raw.output_tokens or 0),
                cached_input=int(raw.cached_input_tokens or 0),
            )
            try:
                cost = compute_cost(
                    credentials.provider,
                    credentials.model,
                    input_tokens=tokens.input,
                    output_tokens=tokens.output,
                    cached_input_tokens=tokens.cached_input,
                )
            except UnknownModelPricing:
                logger.error(
                    "unknown model pricing provider=%s model=%s",
                    credentials.provider,
                    credentials.model,
                )
                raise

        try:
            parsed = response_model.model_validate_json(raw.content)
        except (ValidationError, ValueError):
            append_record(
                records,
                ctx=ctx,
                agent_name=agent_name,
                trigger=trigger,
                attempt=attempt,
                status="failed",
                cost_usd=cost if usage_present else "0.000000",
                started_at=started,
                duration_ms=duration,
                model_used=credentials.model,
                prompt_version=prompt_version,
                input_digest=prompt_digest,
                tokens=tokens if usage_present else TokenCounts(),
                error_report=_failed_report(ErrorCode.JSON_INVALID, "Model returned invalid JSON."),
            )
            if http_attempt == 1 and seconds_left(deadline_at) > SKIP_MODEL_WHEN.total_seconds():
                continue
            raise ModelCallError("invalid JSON") from None

        if not usage_present:
            append_record(
                records,
                ctx=ctx,
                agent_name=agent_name,
                trigger=trigger,
                attempt=attempt,
                status="failed",
                cost_usd="0.000000",
                started_at=started,
                duration_ms=duration,
                model_used=credentials.model,
                prompt_version=prompt_version,
                input_digest=prompt_digest,
                error_report=_failed_report(
                    ErrorCode.GENERATION_FAILED, "Provider omitted token usage."
                ),
            )
            raise ModelCallError("provider omitted usage")

        append_record(
            records,
            ctx=ctx,
            agent_name=agent_name,
            trigger=trigger,
            attempt=attempt,
            status="completed",
            cost_usd=cost,
            started_at=started,
            duration_ms=duration,
            model_used=credentials.model,
            prompt_version=prompt_version,
            input_digest=prompt_digest,
            tokens=tokens,
        )
        return parsed

    raise ModelCallError(str(last_error) if last_error else "model call failed")
