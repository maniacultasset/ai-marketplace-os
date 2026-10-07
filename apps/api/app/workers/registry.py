"""The job body registry: one async body per `JobKind`, run by the wrapper (`wrapper.py`).

A body gets a `JobContext` (ids and the payload only; the tenant is already bound) and returns an
`Outcome` (`succeeded` or `partial`, with an optional `result_json`); any failure is an exception
(`app.workers.errors`). Bodies are registered by the step that owns the kind (S13b `bulk_import`,
S18 generation, S21 publish); a kind with no body ends `failed` + `INTERNAL_ERROR`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from mkos_schemas.enums import JobKind

OutcomeStatus = Literal["succeeded", "partial"]


@dataclass(frozen=True, slots=True)
class JobContext:
    job_run_id: UUID
    tenant_id: UUID
    kind: JobKind
    request_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class Outcome:
    status: OutcomeStatus
    result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in ("succeeded", "partial"):
            raise ValueError("an Outcome is succeeded or partial; failures are exceptions")


Body = Callable[[JobContext], Awaitable[Outcome]]
REGISTRY: dict[JobKind, Body] = {}


def register(kind: JobKind, body: Body) -> None:
    try:
        known = JobKind(kind)
    except ValueError:
        raise ValueError(f"unknown job kind {kind!r}") from None
    REGISTRY[known] = body


def body_for(kind: JobKind) -> Body | None:
    return REGISTRY.get(kind)
