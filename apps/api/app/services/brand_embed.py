"""The `brand_embed` job body and hooks (S16 T12/T13; PLAN D1, D7; CP5, CP6, CP11).

`run_brand_embed(ctx)` dispatches on `payload["source"]`. Every unit of work is
`tenant_uow(ctx.tenant_id)` with no user, and none is open during a storage read or a model call.

Document (`{"source": "brand_document", "document_id", "chunk_type"}`):
1. `claim_for_job` (gone, or another job's now → succeeded with a skip).
2. The storage key must sit under `<tenant_id>/`, else `JobFailed(INTERNAL_ERROR)`.
3. `get` the object (missing with the document deleted → skip).
4. Decode and re-chunk (deterministic, the T3 rules).
5. Deactivate the active indexes whose hash changed or that no longer exist.
6. Embed only the missing drafts, at most 100 per call (each call metered); each batch commits
   in its own transaction behind `lock_live_for_job`, so a delete between batches stops the job
   and a crash resumes with only the rest.
7. `mark_ready(chunk_count = active chunks)`.
A retryable `EmbeddingFailed` is `RetryableJobError`; any other is `JobFailed` (source `ai_model`).

Profile (`{"source": "brand_profile"}`): read the voice fields, `chunk_profile` + `check_chunks`
(voice text written by any path is checked again), stop if the active profile set is already
exactly these drafts, reuse stored vectors by hash, embed the rest, then in one transaction under
the tenant's `brand_embed` advisory lock re-read the voice fields: if they changed, start again
from the new values (at most 3 passes, then retryable); else deactivate every active profile row
(the seed's included, CP11) and insert the new set under `source_id = tenant_id`.

`on_brand_embed_terminal` (S13 `Hook`) branches on `job.status`, never on `outcome`: a document
job that ends `failed`, `dead_letter` or `cancelled` marks its document `failed` with the job's
report and deactivates its chunks, fenced on the job id. `on_brand_embed_requeue` points the
document at the staff requeue's new job.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.ops import JobRun
from app.repositories.brand_chunks import BrandChunkRepository, NewChunk
from app.repositories.brand_documents import BrandDocumentRepository
from app.repositories.uow import tenant_uow
from app.services.brand_chunking import (
    MAX_DOCUMENT_CHUNKS,
    MAX_PROFILE_CHUNKS,
    ChunkDraft,
    ChunkingError,
    check_chunks,
    chunk_document,
    chunk_profile,
)
from app.services.brand_storage import StorageNotFound, brand_bucket, get_document_store
from app.services.embeddings import (
    MAX_INPUTS_PER_CALL,
    Embedder,
    EmbeddingFailed,
    embed_texts,
    get_embedder,
)
from app.workers.errors import JobFailed, RetryableJobError
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)

DOCUMENT: Final = "brand_document"
PROFILE: Final = "brand_profile"
PROFILE_PASSES: Final = 3
FAILED_STATES: Final = frozenset({"failed", "dead_letter", "cancelled"})
_CHUNK_TYPES: Final = frozenset({"tone_rule", "glossary", "compliance"})  # CP4


def embedder() -> Embedder:
    """The job's embedder (patched in tests)."""
    return get_embedder(get_settings())


def _report(code: ErrorCode, message: str, ctx: JobContext, source: ErrorSource) -> ErrorReport:
    item = ErrorItem(
        field="request", error_code=code, message=message, source=source, retryable=False
    )
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=False,
        attempts_used=ctx.attempt,
        max_attempts=3,
        errors=[item],
        request_id=ctx.request_id,
    )


def _failed(code: ErrorCode, message: str, ctx: JobContext, source: ErrorSource) -> JobFailed:
    return JobFailed(_report(code, message, ctx, source))


async def _embed(
    model: Embedder, drafts: Sequence[ChunkDraft], ctx: JobContext
) -> list[list[float]]:
    try:
        batches = await embed_texts(
            model,
            [d.text for d in drafts],
            tenant_id=ctx.tenant_id,
            job_id=ctx.job_run_id,
            attempt=ctx.attempt,
            request_id=ctx.request_id,
        )
    except EmbeddingFailed as exc:
        if exc.retryable:
            raise RetryableJobError(exc.code) from None
        raise _failed(exc.code, "the embedding call failed", ctx, ErrorSource.AI_MODEL) from None
    return [vector for batch in batches for vector in batch.vectors]


def _rows(
    drafts: Sequence[ChunkDraft],
    vectors: Sequence[Sequence[float]],
    *,
    model: str,
    source_type: str,
    source_id: uuid.UUID,
) -> list[NewChunk]:
    return [
        NewChunk(
            type=d.type,
            text=d.text,
            embedding=v,
            embedding_model=model,
            content_hash=d.content_hash,
            source_type=source_type,
            source_id=source_id,
            chunk_index=d.index,
        )
        for d, v in zip(drafts, vectors, strict=True)
    ]


async def run_brand_embed(ctx: JobContext) -> Outcome:
    source = ctx.payload.get("source")
    if source == DOCUMENT:
        return await _run_document(ctx)
    if source == PROFILE:
        return await _run_profile(ctx)
    raise _failed(ErrorCode.INTERNAL_ERROR, "unknown brand_embed source", ctx, ErrorSource.SYSTEM)


# ---- documents (T12) ----


def _document_args(ctx: JobContext) -> tuple[uuid.UUID, Any]:
    try:
        document_id = uuid.UUID(str(ctx.payload["document_id"]))
    except (KeyError, ValueError):
        raise _failed(
            ErrorCode.INTERNAL_ERROR, "bad brand_embed payload", ctx, ErrorSource.SYSTEM
        ) from None
    chunk_type = ctx.payload.get("chunk_type", "tone_rule")
    if chunk_type not in _CHUNK_TYPES:
        raise _failed(ErrorCode.INTERNAL_ERROR, "bad brand_embed payload", ctx, ErrorSource.SYSTEM)
    return document_id, chunk_type


async def _document_gone(ctx: JobContext, document_id: uuid.UUID) -> bool:
    async with tenant_uow(ctx.tenant_id) as uow:
        return await uow.brand_documents.get_live(document_id) is None


async def _run_document(ctx: JobContext) -> Outcome:
    document_id, chunk_type = _document_args(ctx)
    tenant, job = ctx.tenant_id, ctx.job_run_id
    skipped = Outcome("succeeded", {"skipped": "document_gone"})
    async with tenant_uow(tenant) as uow:
        key = await uow.brand_documents.claim_for_job(document_id, job)
    if key is None:
        return skipped
    if not key.startswith(f"{tenant}/"):
        raise _failed(
            ErrorCode.INTERNAL_ERROR,
            "the object key is outside the tenant",
            ctx,
            ErrorSource.SYSTEM,
        )
    try:
        data = await get_document_store().get(brand_bucket(), key)
    except StorageNotFound:
        if await _document_gone(ctx, document_id):
            return skipped
        raise _failed(
            ErrorCode.INTERNAL_ERROR, "the stored document is missing", ctx, ErrorSource.SYSTEM
        ) from None
    try:
        drafts = chunk_document(data.decode("utf-8"), chunk_type, limit=MAX_DOCUMENT_CHUNKS)
        check_chunks(drafts, field="file", max_chunks=MAX_DOCUMENT_CHUNKS)
    except (UnicodeDecodeError, ChunkingError):
        raise _failed(
            ErrorCode.VALIDATION_FAILED, "the document cannot be chunked", ctx, ErrorSource.SYSTEM
        ) from None

    model = embedder()
    async with tenant_uow(tenant) as uow:
        active = await uow.brand_chunks.active_for_source(DOCUMENT, document_id, model.model)
        stale = [i for i, h in active.items() if i >= len(drafts) or drafts[i].content_hash != h]
        await uow.brand_chunks.deactivate_source(DOCUMENT, document_id, indexes=stale)
    missing = [d for d in drafts if active.get(d.index) != d.content_hash]
    for start in range(0, len(missing), MAX_INPUTS_PER_CALL):
        batch = missing[start : start + MAX_INPUTS_PER_CALL]
        vectors = await _embed(model, batch, ctx)
        async with tenant_uow(tenant) as uow:
            if not await uow.brand_documents.lock_live_for_job(document_id, job):
                return skipped
            rows = _rows(
                batch, vectors, model=model.model, source_type=DOCUMENT, source_id=document_id
            )
            await uow.brand_chunks.insert_many(rows)
    async with tenant_uow(tenant) as uow:
        if not await uow.brand_documents.lock_live_for_job(document_id, job):
            return skipped
        count = await uow.brand_chunks.count_active(DOCUMENT, document_id, model.model)
        await uow.brand_documents.mark_ready(document_id, job, count)
    return Outcome("succeeded", {"chunks": count, "embedded": len(missing)})


def _payload_document(job: JobRun) -> uuid.UUID | None:
    payload = job.payload_json or {}
    if payload.get("source") != DOCUMENT:
        return None
    try:
        return uuid.UUID(str(payload.get("document_id")))
    except ValueError:
        return None


def _cancel_report(job: JobRun) -> dict[str, Any]:
    item = ErrorItem(
        field="request",
        error_code=ErrorCode.INTERNAL_ERROR,
        message="embedding job cancelled",
        source=ErrorSource.SYSTEM,
        retryable=False,
    )
    report = ErrorReport(
        status="failed",
        error_code=ErrorCode.INTERNAL_ERROR,
        retryable=False,
        attempts_used=max(1, min(job.attempt, 3)),
        max_attempts=3,
        errors=[item],
        request_id=job.request_id or "job-cancelled",
    )
    return report.model_dump(mode="json")


async def on_brand_embed_terminal(session: Any, job: JobRun, outcome: Outcome | None) -> None:
    """In the terminal transaction, after the terminal write: branches on `job.status` only."""
    del outcome  # None on every path but success (S13 `Hook`)
    document_id = _payload_document(job)
    if document_id is None or job.status not in FAILED_STATES:
        return
    report = job.error_report_json or _cancel_report(job)
    if await BrandDocumentRepository(session, job.tenant_id).mark_failed(
        document_id, job.id, report
    ):
        await BrandChunkRepository(session, job.tenant_id).deactivate_source(DOCUMENT, document_id)


async def on_brand_embed_requeue(session: Any, old: JobRun, new: JobRun) -> None:
    document_id = _payload_document(old)
    if document_id is not None:
        await BrandDocumentRepository(session, old.tenant_id).point_at_job(
            document_id, old.id, new.id
        )


# ---- the profile (T13) ----


def _voice(tenant: Any) -> tuple[str | None, list[str], dict[str, str]]:
    return tenant.tone, list(tenant.guidelines or []), dict(tenant.glossary or {})


async def _read_voice(ctx: JobContext) -> tuple[str | None, list[str], dict[str, str]]:
    async with tenant_uow(ctx.tenant_id) as uow:
        tenant = await uow.tenants.get_profile()
    if tenant is None:
        raise _failed(ErrorCode.INTERNAL_ERROR, "the tenant is missing", ctx, ErrorSource.SYSTEM)
    return _voice(tenant)


async def _run_profile(ctx: JobContext) -> Outcome:
    tenant = ctx.tenant_id
    voice = await _read_voice(ctx)
    model = embedder()
    for _ in range(PROFILE_PASSES):
        drafts = chunk_profile(*voice)
        if drafts:
            try:
                check_chunks(drafts, field="profile", max_chunks=MAX_PROFILE_CHUNKS)
            except ChunkingError:
                raise _failed(
                    ErrorCode.VALIDATION_FAILED,
                    "the brand profile cannot be chunked",
                    ctx,
                    ErrorSource.SYSTEM,
                ) from None
        wanted = [(tenant, d.index, d.content_hash) for d in drafts]
        async with tenant_uow(tenant) as uow:
            current = await uow.brand_chunks.active_profile_set(model.model)
            if current == wanted:
                return Outcome("succeeded", {"chunks": len(drafts), "unchanged": True})
            reuse = await uow.brand_chunks.vectors_by_hash(
                PROFILE, tenant, [d.content_hash for d in drafts], model.model
            )
        missing = [d for d in drafts if d.content_hash not in reuse]
        if missing:
            for d, v in zip(missing, await _embed(model, missing, ctx), strict=True):
                reuse[d.content_hash] = v
        async with tenant_uow(tenant) as uow:
            await uow.brand_chunks.lock_profile_embed()
            row = await uow.tenants.get_profile()
            now = voice if row is None else _voice(row)
            if now != voice:
                voice = now  # superseded meanwhile: start again from what is stored now
                continue
            await uow.brand_chunks.deactivate_profile_all()
            vectors = [reuse[d.content_hash] for d in drafts]
            await uow.brand_chunks.insert_many(
                _rows(drafts, vectors, model=model.model, source_type=PROFILE, source_id=tenant)
            )
        return Outcome("succeeded", {"chunks": len(drafts), "embedded": len(missing)})
    raise RetryableJobError(ErrorCode.INTERNAL_ERROR)
