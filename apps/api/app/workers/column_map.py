"""The column-map job body (SI14, AC8): `run_column_map(ctx)`.

A column map's AI step is an `image_compliance` job (`entity_type='column_map'`, entity = the
map) whose payload names `column_map` (`app.workers.image_jobs` dispatches). It asks the pipeline's
`map_columns` about the headers the deterministic rungs left unmapped, and never anything else:
- the request holds those headers only (the first `MAX_LEFTOVER_HEADERS` by position; the rest stay
  unmapped for the seller), `samples` always empty (seller cells may hold addresses or names),
  the importer's accepted names as the dictionary and the names already taken;
- each record is checked and restamped from the job (`trust_records`: agent `attribute`, trigger
  `generate`, no product, prompt `map_columns/`) and stored first (invariant 7);
- a key outside the dictionary, onto an identity a deterministic column holds, or for a header the
  request did not hold is dropped; of two answers on one identity the higher confidence wins;
- a kept answer is method `ai`, needing review below 0.80. The map becomes `ready`.
Switched off, no credentials, or no `map_columns` in the pipeline: `ready` with the leftovers
unmapped and nothing spent. A timeout or a seam exception is retried by the job policy; when the
job ends without an answer the terminal hook settles the map `failed` with its deterministic
mappings, and the seller maps the rest by hand.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.columns import (
    MAX_LEFTOVER_HEADERS,
    ColumnMapRequest,
    ColumnMapResult,
    ColumnSample,
    DictionaryEntry,
)
from mkos_schemas.enums import JobKind
from pydantic import ValidationError

from app.core.logging import get_logger
from app.repositories.uow import tenant_uow
from app.services.image_edit_result import trust_records
from app.services.imports.column_map import FUZZY_MIN, TARGETS, identity_of
from app.workers.errors import JobFailed, RetryableJobError, system_report
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
PROMPT_PREFIX: Final = "map_columns/"
AGENT: Final = "attribute"
TRIGGERS: Final = frozenset({"generate"})
SEAM_GRACE_S: Final = 15
_TARGET_SET: Final = frozenset(TARGETS)
DICTIONARY: Final = tuple(
    DictionaryEntry(key=name, label=name.replace("_", " ")) for name in TARGETS
)


@dataclass(frozen=True, slots=True)
class _Map:
    map_id: UUID
    headers: list[str]
    mappings: list[dict[str, Any]]
    claim_started_at: datetime


def _failed(message: str, ctx: JobContext) -> JobFailed:
    return JobFailed(
        system_report(
            ErrorCode.INTERNAL_ERROR,
            message,
            attempt=ctx.attempt,
            max_attempts=POLICIES[JobKind.IMAGE_COMPLIANCE].max_attempts,
            request_id=ctx.request_id,
        )
    )


def _map_id(ctx: JobContext) -> UUID:
    payload = ctx.payload.get("column_map")
    try:
        return UUID(str(payload["map_id"]))  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        raise _failed("the column-map job has no map id", ctx) from None


def _done(map_id: UUID, status: str) -> Outcome:
    return Outcome("succeeded", {"map_id": str(map_id), "status": status}, zero_reservation=True)


async def _read(ctx: JobContext, map_id: UUID) -> _Map | None:
    async with tenant_uow(ctx.tenant_id) as uow:
        row = await uow.column_maps.get(map_id)
        if row is None or row.status != "mapping":
            return None
        job = await uow.job_runs.get_by_id(ctx.job_run_id)
        return _Map(
            map_id=row.id,
            headers=list(row.headers or []),
            mappings=[dict(m) for m in row.mappings or []],
            claim_started_at=(job.started_at if job else None) or datetime.now(UTC),
        )


async def _settle(
    ctx: JobContext, found: _Map, mappings: list[dict[str, Any]], status: str = "ready"
) -> Outcome:
    async with tenant_uow(ctx.tenant_id) as uow:
        written = await uow.column_maps.set_result(found.map_id, status=status, mappings=mappings)
    log.info(
        "column_map.mapped",
        map_id=str(found.map_id),
        job_run_id=str(ctx.job_run_id),
        written=written,
        ai_mapped=sum(1 for m in mappings if m.get("method") == "ai"),
    )
    return _done(found.map_id, status if written else "skipped")


def _leftovers(found: _Map) -> list[tuple[int, str]]:
    """(position, header) of the unmapped columns the request may hold, by position."""
    out: list[tuple[int, str]] = []
    for entry in sorted(found.mappings, key=lambda m: int(m["position"])):
        position = int(entry["position"])
        header = found.headers[position - 1] if 0 < position <= len(found.headers) else ""
        if entry.get("key") is None and header.strip():
            out.append((position, header))
    return out[:MAX_LEFTOVER_HEADERS]


def _taken(found: _Map) -> list[str]:
    return sorted({str(m["key"]) for m in found.mappings if m.get("key") is not None})


def _request(ctx: JobContext, found: _Map, leftovers: list[tuple[int, str]]) -> ColumnMapRequest:
    return ColumnMapRequest(
        tenant_id=ctx.tenant_id,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        headers=[ColumnSample(header=header, samples=[]) for _, header in leftovers],
        dictionary=list(DICTIONARY),
        taken_keys=[k for k in _taken(found) if k in _TARGET_SET],
    )


def _checked(ctx: JobContext, found: _Map, result: object) -> ColumnMapResult | str:
    if not isinstance(result, ColumnMapResult):
        return "type"
    try:
        checked = ColumnMapResult.model_validate_json(result.model_dump_json())
    except (ValidationError, ValueError, TypeError):
        return "schema"
    records = trust_records(
        checked.agent_runs,
        prompt_prefix=PROMPT_PREFIX,
        tenant_id=ctx.tenant_id,
        product_id=None,
        job_id=ctx.job_run_id,
        request_id=ctx.request_id,
        attempt=min(max(ctx.attempt, 1), 3),
        claim_started_at=found.claim_started_at,
        now=datetime.now(UTC),
        agent_name=AGENT,
        triggers=TRIGGERS,
    )
    if isinstance(records, str):
        return records
    return checked.model_copy(update={"agent_runs": list(records)})


def merge(
    found: _Map, leftovers: list[tuple[int, str]], result: ColumnMapResult
) -> list[dict[str, Any]]:
    """The map's entries with the AI's kept answers (module docstring rules)."""
    held = {identity_of(str(m["key"])) for m in found.mappings if m.get("key") is not None}
    asked = {header: position for position, header in leftovers}
    best: dict[str, tuple[int, str, float]] = {}
    for header, answer in result.mappings.items():
        position = asked.get(header)
        if position is None or answer.key is None or answer.key not in _TARGET_SET:
            continue
        identity = identity_of(answer.key)
        if identity in held:
            continue
        seen = best.get(identity)
        if seen is None or answer.confidence > seen[2]:
            best[identity] = (position, answer.key, answer.confidence)
    by_position = {position: (key, conf) for position, key, conf in best.values()}
    out: list[dict[str, Any]] = []
    for entry in found.mappings:
        merged = dict(entry)
        hit = by_position.get(int(entry["position"]))
        if hit is not None:
            key, confidence = hit
            merged.update(
                key=key,
                method="ai",
                confidence=round(confidence, 4),
                needs_review=confidence < FUZZY_MIN,
            )
        out.append(merged)
    return out


async def run_column_map(ctx: JobContext) -> Outcome:
    from app.core.config import get_settings
    from app.services.pipeline_deps import SeamPortError, column_map_credentials
    from app.workers import column_map_seam
    from app.workers.budget import COLUMN_MAP_RESERVATION_USD, BudgetRefused, reserve

    map_id = _map_id(ctx)
    found = await _read(ctx, map_id)
    if found is None:
        log.info("column_map.skipped", map_id=str(map_id), job_run_id=str(ctx.job_run_id))
        return _done(map_id, "skipped")
    leftovers = _leftovers(found)
    settings = get_settings()
    if not settings.column_map_ai_enabled or not leftovers:
        return await _settle(ctx, found, found.mappings)
    try:
        credentials = column_map_credentials(settings)
    except SeamPortError:
        return await _settle(ctx, found, found.mappings)
    try:
        reserved = await reserve(
            ctx.tenant_id, ctx.job_run_id, ctx.attempt, amount=COLUMN_MAP_RESERVATION_USD
        )
    except BudgetRefused:
        return await _settle(ctx, found, found.mappings)
    if not reserved:
        raise _failed("the column-map job lost its claim", ctx)
    request = _request(ctx, found, leftovers)
    try:
        result = await asyncio.wait_for(
            column_map_seam.call_map_columns(request, credentials),
            timeout=credentials.timeout_seconds + SEAM_GRACE_S,
        )
    except Exception as error:  # a timeout or a pipeline bug: the job policy retries
        log.info(
            "column_map.seam_failed",
            map_id=str(map_id),
            job_run_id=str(ctx.job_run_id),
            error=type(error).__name__,
        )
        raise RetryableJobError(ErrorCode.JOB_TIMEOUT) from None
    if result is None:  # no map_columns in the installed pipeline: no call was made
        return await _settle(ctx, found, found.mappings)
    checked = _checked(ctx, found, result)
    if isinstance(checked, str):
        log.info(
            "column_map.untrusted",
            map_id=str(map_id),
            job_run_id=str(ctx.job_run_id),
            check=checked,
        )
        raise _failed("the column-map answer was not trusted", ctx)
    async with tenant_uow(ctx.tenant_id) as uow:  # invariant 7: the records first
        for record in checked.agent_runs:
            await uow.agent_runs.insert(record)
    if checked.status == "failed" or checked.error is not None:
        return await _settle(ctx, found, found.mappings, status="failed")
    return await _settle(ctx, found, merge(found, leftovers, checked))
