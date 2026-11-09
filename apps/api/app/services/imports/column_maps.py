"""Spreadsheet column maps (SI14, AC7-AC8): `POST /imports/column-maps`, `GET
/imports/column-maps/{map_id}` and `POST /imports/column-maps/{map_id}/confirm`.

Create (Idempotency-Key): the headers are normalised (`normalize_header`) and must be distinct
(422 `VALIDATION_FAILED` at `headers`), then mapped by `map_headers` (exact, alias, fuzzy).
- Nothing left unmapped: 201 `ready`, no samples kept.
- Leftovers while `COLUMN_MAP_AI_ENABLED` is on and the SI10 admission lets it through: the map is
  `mapping` and one `image_compliance` job (entity `column_map`) asks the pipeline for the
  leftovers only, never a sample cell: 202.
- Leftovers otherwise (switch off, AI paused, a cap): 201 `ready` with the leftovers unmapped for
  the seller. The import never depends on the AI.
The samples, when there are leftovers, are kept for the seller's review only (cleared at confirm
or after 7 days).

Read: a `mapping` map whose job ended without an answer (a dead letter, a cancel; the reaper or the
queue reconcile runs no worker code) is settled to `failed` first, with the deterministic mappings
kept, so it never stays `mapping`.

Confirm (Idempotency-Key): only from `ready` or `failed` (409 `INVALID_STATUS_TRANSITION`
otherwise). Answers are keyed by column position; every column needing review is answered; a key
is a `TARGETS` name, the column's own bullet or export-only name, or null; two columns never share
a key identity. The result must hold the required columns (`seller_sku`, `product_name`,
`category`), else 422 `IMPORT_FAILED` at `file.header` naming the missing ones. Confirm clears the
samples.

Raw header text never leaves the API: each column shows as itself only when it is a plain name,
else as `column N` (`headers._shown`).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from mkos_schemas.errors import ErrorReport

from app.core.config import get_settings
from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.db.models.ops import ImportColumnMap
from app.repositories.idempotency import IdempotencyKeyRepository, Mismatch, Replay
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow
from app.services.budget import admit
from app.services.idempotency import idempotent, key_reused, request_hash
from app.services.imports.column_map import TARGETS, Mapped, identity_of, map_headers
from app.services.imports.headers import (
    IGNORED_WITH_WARNING,
    REQUIRED,
    _shown,
    header_report,
    is_bullet,
    normalize_header,
    row_key,
)
from app.workers.budget import COLUMN_MAP_RESERVATION_USD
from app.workers.policy import COLUMN_MAP_ENTITY, lock_for

__all__ = [
    "ColumnMapRefused",
    "confirm_map",
    "create_map",
    "entry_dict",
    "get_map",
    "headers_digest",
    "map_body",
]

log = get_logger(__name__)
METHOD: Final = "POST"
TERMINAL_JOB: Final = frozenset({"dead_letter", "cancelled", "failed"})
_TARGET_SET: Final = frozenset(TARGETS)


class ColumnMapRefused(Exception):  # noqa: N818 - a refusal carrying its report
    """A 422 `IMPORT_FAILED` with the one file-level report (the required columns)."""

    def __init__(self, report: ErrorReport) -> None:
        super().__init__("column map refused")
        self.report = report


def headers_digest(headers: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(headers).encode()).hexdigest()


def entry_dict(entry: Mapped) -> dict[str, Any]:
    return {
        "position": entry.position,
        "key": entry.key,
        "method": entry.method,
        "confidence": entry.confidence,
        "needs_review": entry.needs_review,
    }


def map_body(row: ImportColumnMap) -> dict[str, Any]:
    headers = list(row.headers or [])
    entries = []
    for entry in row.mappings or []:
        position = int(entry["position"])
        name = headers[position - 1] if 0 < position <= len(headers) else ""
        entries.append(
            {
                "position": position,
                "header_shown": _shown(name, position),
                "key": entry.get("key"),
                "method": entry.get("method", "none"),
                "confidence": float(entry.get("confidence", 0.0)),
                "needs_review": bool(entry.get("needs_review", False)),
            }
        )
    return {
        "id": str(row.id),
        "status": row.status,
        "mappings": entries,
        "needs_review_count": sum(1 for e in entries if e["needs_review"]),
        "created_at": row.created_at.isoformat(),
    }


async def _peek(
    principal: Principal, key: str, path: str, payload: bytes
) -> tuple[int, dict[str, Any]] | None:
    digest = request_hash(METHOD, path, payload)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        seen = await IdempotencyKeyRepository(uow.session, principal.tenant_id).peek(
            key, f"{METHOD} {path}", digest, principal.user_id
        )
    if isinstance(seen, Mismatch):
        raise key_reused()
    if isinstance(seen, Replay):
        return seen.status, seen.body
    return None


def _normalised(headers: Sequence[str]) -> list[str]:
    names = [normalize_header(h) for h in headers]
    if len(set(names)) != len(names):
        raise InputError(ErrorCode.VALIDATION_FAILED, "headers")
    return names


async def create_map(
    principal: Principal,
    headers: Sequence[str],
    samples: Mapping[str, Sequence[str]],
    idem_key: str,
    path: str,
) -> tuple[int, dict[str, Any]]:
    tenant = principal.tenant_id
    payload = json.dumps(
        {"headers": list(headers), "samples": {k: list(v) for k, v in samples.items()}},
        sort_keys=True,
        ensure_ascii=False,
    ).encode()
    replay = await _peek(principal, idem_key, path, payload)
    if replay is not None:
        return replay
    names = _normalised(headers)
    entries = map_headers(headers)
    leftovers = [e for e in entries if e.key is None]
    kept_samples = (
        {normalize_header(k): list(v)[:3] for k, v in samples.items()} if leftovers else None
    )
    ask_ai = bool(leftovers) and get_settings().column_map_ai_enabled
    if ask_ai:
        now = datetime.now(UTC)
        refusal = await admit(tenant, now, amount=COLUMN_MAP_RESERVATION_USD)
        if refusal is not None:
            log.info("column_map.ai_not_admitted", tenant_id=str(tenant), refusal=refusal)
            ask_ai = False

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, tenant)
        map_id = uuid.uuid4()
        row = await uow.column_maps.create(
            map_id=map_id,
            status="mapping" if ask_ai else "ready",
            headers=names,
            headers_digest=headers_digest(names),
            mappings=[entry_dict(e) for e in entries],
            samples=kept_samples,
            created_by=principal.user_id,
        )
        if ask_ai:
            enqueued = await uow.job_runs.enqueue(
                EnqueueSpec(
                    kind=JobKind.IMAGE_COMPLIANCE,
                    entity_type=COLUMN_MAP_ENTITY,
                    entity_id=map_id,
                    created_by=principal.user_id,
                    payload={"column_map": {"map_id": str(map_id)}},
                    lock=lock_for(JobKind.IMAGE_COMPLIANCE, COLUMN_MAP_ENTITY, tenant),
                )
            )
            await uow.column_maps.set_job(map_id, enqueued.job.id)
            await session.refresh(row)
            return 202, map_body(row)
        await session.refresh(row)
        return 201, map_body(row)

    status, body = await idempotent(principal, idem_key, METHOD, path, payload, handler)
    log.info(
        "column_map.created",
        tenant_id=str(tenant),
        map_id=body.get("id"),
        status=body.get("status"),
        columns=len(names),
        leftovers=len(leftovers),
    )
    return status, body


def _settled_failed(row: ImportColumnMap) -> list[dict[str, Any]]:
    """The deterministic mappings kept; a column the AI was asked about stays unmapped."""
    return [dict(entry) for entry in row.mappings or []]


async def _settle_dead_job(uow: TenantUnitOfWork, row: ImportColumnMap) -> ImportColumnMap:
    if row.status != "mapping" or row.job_id is None:
        return row
    job = await uow.job_runs.get_by_id(row.job_id)
    if job is None or job.status not in TERMINAL_JOB:
        return row
    locked = await uow.column_maps.get_for_update(row.id)
    if locked is None or locked.status != "mapping":
        return locked or row
    await uow.column_maps.set_result(row.id, status="failed", mappings=_settled_failed(locked))
    log.info("column_map.settled_failed", map_id=str(row.id), job_status=job.status)
    refreshed = await uow.column_maps.get_for_update(row.id)
    return refreshed or locked


async def get_map(principal: Principal, map_id: uuid.UUID) -> dict[str, Any]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.column_maps.get(map_id)
        if row is None:
            raise NotFoundError("map_id")
        row = await _settle_dead_job(uow, row)
        return map_body(row)


def _allowed_key(name: str, key: str) -> bool:
    """A key a column may take: an accepted target, or the column's own bullet or export-only
    name."""
    if key in _TARGET_SET:
        return True
    return key == name and (is_bullet(name) or name in IGNORED_WITH_WARNING)


def _answered(row: ImportColumnMap, answers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    headers = list(row.headers or [])
    entries = {int(e["position"]): dict(e) for e in row.mappings or []}
    for answer in answers:
        position = int(answer["position"])
        if position not in entries:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"mappings.{position}")
        key = answer.get("key")
        if key is not None and not _allowed_key(headers[position - 1], str(key)):
            raise InputError(ErrorCode.VALIDATION_FAILED, f"mappings.{position}")
        entry = entries[position]
        entry["key"] = key
        entry["needs_review"] = False
        if entry.get("method") == "none" and key is not None:
            entry["confidence"] = 1.0
    for position, entry in sorted(entries.items()):
        if entry.get("needs_review"):
            raise InputError(ErrorCode.VALIDATION_FAILED, f"mappings.{position}")
    seen: set[str] = set()
    for position, entry in sorted(entries.items()):
        key = entry.get("key")
        if key is None:
            continue
        identity = identity_of(str(key))
        if identity in seen:
            raise InputError(ErrorCode.VALIDATION_FAILED, f"mappings.{position}")
        seen.add(identity)
    present = {row_key(str(e["key"])) for e in entries.values() if e.get("key") is not None}
    missing = [name for name in REQUIRED if name not in present]
    if missing:
        raise ColumnMapRefused(
            header_report(
                [(ErrorCode.HEADER_MISSING_COLUMN, f"missing column {name}") for name in missing]
            )
        )
    return [entries[p] for p in sorted(entries)]


async def confirm_map(
    principal: Principal,
    map_id: uuid.UUID,
    answers: Sequence[Mapping[str, Any]],
    idem_key: str,
    path: str,
) -> tuple[int, dict[str, Any]]:
    tenant = principal.tenant_id
    payload = json.dumps(
        {"mappings": [dict(a) for a in answers]}, sort_keys=True, ensure_ascii=False
    ).encode()
    replay = await _peek(principal, idem_key, path, payload)
    if replay is not None:
        return replay
    async with tenant_uow(tenant, user_id=principal.user_id) as uow:  # a dead job settles first
        row = await uow.column_maps.get(map_id)
        if row is None:
            raise NotFoundError("map_id")
        await _settle_dead_job(uow, row)

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, tenant)
        row = await uow.column_maps.get_for_update(map_id)
        if row is None:
            raise NotFoundError("map_id")
        if row.status not in ("ready", "failed"):
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "column_map")
        mappings = _answered(row, answers)
        await uow.column_maps.confirm(map_id, mappings=mappings, at=datetime.now(UTC))
        await session.refresh(row)
        return 200, map_body(row)

    status, body = await idempotent(principal, idem_key, METHOD, path, payload, handler)
    log.info("column_map.confirmed", tenant_id=str(tenant), map_id=str(map_id), status=status)
    return status, body
