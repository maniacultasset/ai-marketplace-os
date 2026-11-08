"""What the backend makes of an image-edit result (SI7 T12). Pure: no I/O.

`check_edit_result(result, *, ...)` decides whether the result can be trusted, as `check_result`
does for generation (`app/services/generation_result.py`):
- it is re-validated through JSON, which defeats a `model_construct` that skipped validation;
- each record must name this job's tenant and job (`ImageEditResult` carries no ids of its own),
  be an `image` agent and carry an `image_edit/` prompt version;
- each record then takes its identity from the job (tenant, product, job, request, attempt; no
  listing), and a `started_at` outside `[claim_started_at, now]` is clamped to
  `claim_started_at`, so a stub or backdated record books to today's budget day.
An untrusted result stores no record and keeps the reservation counted.

SI12: `trust_records(records, *, prompt_prefix, ...)` is that record check on its own, shared with
the size-chart read (`app/workers/size_chart_read.py`, prompt prefix `size_chart_read/`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.image_edit import ImageEditResult
from pydantic import ValidationError

__all__ = ["EditTrusted", "EditUntrusted", "check_edit_result", "trust_records"]

PROMPT_PREFIX: Final = "image_edit/"


@dataclass(frozen=True, slots=True)
class EditTrusted:
    result: ImageEditResult
    records: tuple[AgentRunRecord, ...]


@dataclass(frozen=True, slots=True)
class EditUntrusted:
    check: str


def trust_records(
    records: Sequence[AgentRunRecord],
    *,
    prompt_prefix: str,
    tenant_id: UUID,
    product_id: UUID,
    job_id: UUID,
    request_id: str,
    attempt: int,
    claim_started_at: datetime,
    now: datetime,
) -> tuple[AgentRunRecord, ...] | str:
    """The records restamped from the job, or the name of the first check one failed."""
    out: list[AgentRunRecord] = []
    for record in records:
        if record.tenant_id != tenant_id or record.job_id != job_id:
            return "identity"
        if record.agent_name != "image":
            return "agent"
        if not (record.prompt_version or "").startswith(prompt_prefix):
            return "prompt_version"
        started = record.started_at
        if not claim_started_at <= started <= now:
            started = claim_started_at
        out.append(
            record.model_copy(
                update={
                    "tenant_id": tenant_id,
                    "product_id": product_id,
                    "listing_id": None,
                    "listing_version_id": None,
                    "job_id": job_id,
                    "request_id": request_id,
                    "attempt": attempt,
                    "started_at": started,
                }
            )
        )
    return tuple(out)


def check_edit_result(
    result: object,
    *,
    tenant_id: UUID,
    product_id: UUID,
    job_id: UUID,
    request_id: str,
    attempt: int,
    claim_started_at: datetime,
    now: datetime,
) -> EditTrusted | EditUntrusted:
    if not isinstance(result, ImageEditResult):
        return EditUntrusted("type")
    try:
        checked = ImageEditResult.model_validate_json(result.model_dump_json())
    except (ValidationError, ValueError, TypeError):
        return EditUntrusted("schema")
    records = trust_records(
        checked.agent_runs,
        prompt_prefix=PROMPT_PREFIX,
        tenant_id=tenant_id,
        product_id=product_id,
        job_id=job_id,
        request_id=request_id,
        attempt=attempt,
        claim_started_at=claim_started_at,
        now=now,
    )
    if isinstance(records, str):
        return EditUntrusted(records)
    return EditTrusted(checked.model_copy(update={"agent_runs": list(records)}), records)
