"""The stub `complete_json` (SI13 T12): the pipeline's one-call JSON helper, deterministic, no
clock, no I/O beyond its fixture.

It answers `response_model.model_validate(<the fixture of prompt_version>)` (the backend's
`error-diagnosis-1` draft) and appends one priced-at-zero `AgentRunRecord` of the shape the real
helper writes, stamped with the ids of `ctx`. An unknown prompt version raises `LookupError`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from typing import Any, Final
from uuid import uuid5

from mkos_schemas.agent import AgentRunRecord, TokenUsage
from mkos_schemas.jobs import ModelCredentials
from pydantic import BaseModel

FIXTURES: Final = {"error-diagnosis-1": "fixtures/error_diagnosis.json"}
STUB_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)


async def complete_json(
    *,
    credentials: ModelCredentials,
    purpose: str,
    prompt_version: str,
    system: str,
    developer: str,
    data: dict[str, Any],
    response_model: type[BaseModel],
    agent_name: str,
    trigger: str,
    attempt: int,
    records: list[AgentRunRecord],
    ctx: Any,
    **_: Any,
) -> BaseModel:
    name = FIXTURES.get(prompt_version)
    if name is None:
        raise LookupError(f"no stub answer for {prompt_version}")
    raw = files("mkos_pipeline").joinpath(name).read_text(encoding="utf-8")
    rendered = json.dumps(
        {"system": system, "developer": developer, "data": data}, sort_keys=True, default=str
    )
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    records.append(
        AgentRunRecord(
            id=uuid5(ctx.job_id, f"{prompt_version}:{attempt}"),
            tenant_id=ctx.tenant_id,
            product_id=ctx.product_id,
            listing_id=ctx.listing_id,
            request_id=ctx.request_id,
            job_id=ctx.job_id,
            agent_name=agent_name,
            trigger=trigger,
            attempt=attempt,
            model_used=credentials.model,
            prompt_version=prompt_version,
            tokens=TokenUsage(),
            cost_usd=Decimal("0"),
            status="completed",
            input_digest=digest,
            started_at=STUB_EPOCH,
            created_at=STUB_EPOCH,
        )
    )
    return response_model.model_validate(json.loads(raw))
