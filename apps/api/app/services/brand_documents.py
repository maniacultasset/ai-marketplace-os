"""Brand document upload and delete (S16 T11; PLAN D1, D7, D8; CP4-CP7, CP10).

`accept_upload` never holds a transaction open while it validates or stores the file:

1. An unlocked `peek` of the Idempotency-Key: a completed key replays, a mismatch is 409.
2. An unlocked pre-check of the caps (2 live document jobs, 50 live documents): 429.
3. Validation: `.txt`/`.md` by extension, the filename cut to its basename (NFC, no Cc/Cf
   characters, 1..255), more than `MAX_DOCUMENT_BYTES` is 413 (nothing stored); then, in one
   `asyncio.to_thread` call, strict UTF-8 (a BOM is fine), no NUL, the T3 chunks
   (`chunk_document(limit=500)` + `check_chunks`) and the sha256 of the bytes. A 422 is stored
   under the key (`VALIDATION_FAILED`, field `file`) with nothing else.
4. Content dedupe on the sha256: a live copy that is not `failed` replays its job; a `failed`
   copy is re-queued (a new job, the same document).
5. `put` at `<tenant_id>/brand-documents/<document_id>`.
6. One `idempotent` transaction: the tenant's upload lock, the three caps (live document jobs
   < 2, live documents < 50, active document chunks + this file's ≤ 20,000), the `brand_embed`
   job, the row and the `brand_document.uploaded` audit row, then 202. A concurrent identical
   upload that won the live-hash unique makes this one replay it; its object stays an orphan.

The request hash covers `POST <path>` + NUL + `type` + NUL + the file bytes (CP7), so the same key
with another file or another type is 409. No file body, chunk text or filename is ever logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from mkos_schemas.jobs import BrandChunkType, JobAccepted

from app.core.errors import ConflictError, DomainError, LimitError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.brand_chunks import BrandChunkRepository
from app.repositories.brand_documents import BrandDocumentRepository
from app.repositories.idempotency import IdempotencyKeyRepository, Mismatch, Replay
from app.repositories.identity import AuditRepository
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.repositories.uow import tenant_uow
from app.services.brand_chunking import (
    MAX_DOCUMENT_CHUNKS,
    ChunkingError,
    check_chunks,
    chunk_document,
)
from app.services.brand_storage import (
    brand_bucket,
    brand_document_object_key,
    get_document_store,
)
from app.services.idempotency import idempotent, key_reused, request_hash

log = get_logger(__name__)

MAX_DOCUMENT_BYTES: Final = 10_485_760
LIVE_DOCUMENT_JOBS: Final = 2
MAX_LIVE_DOCUMENTS: Final = 50
MAX_ACTIVE_DOCUMENT_CHUNKS: Final = 20_000
FILENAME_MAX: Final = 255
CONTENT_TYPES: Final = {".txt": "text/plain", ".md": "text/markdown"}
SOURCE: Final = "brand_document"


class PayloadTooLargeError(DomainError):
    """The file is over `MAX_DOCUMENT_BYTES` (413 PAYLOAD_TOO_LARGE)."""


class BrandLimitError(LimitError):
    """A tenant cap is reached (429 RATE_LIMITED): live document jobs, live documents or chunks.
    A `LimitError`, so it carries the cap's `retry_after_s` (the 429's `Retry-After`)."""


@dataclass(frozen=True, slots=True)
class UploadResult:
    """`status` 202 with a `JobAccepted` body, or a stored 422 naming `field`."""

    status: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Checked:
    content_hash: str
    draft_count: int


def clean_filename(raw: str | None) -> str | None:
    """The basename after NFC with Cc and Cf characters removed, or None when nothing is left."""
    if not raw:
        return None
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if unicodedata.category(c) not in ("Cc", "Cf")).strip()
    if not name or name in (".", "..") or len(name) > FILENAME_MAX:
        return None
    return name


def _extension(name: str) -> str | None:
    lower = name.casefold()
    for ext in CONTENT_TYPES:
        if lower.endswith(ext) and len(lower) > len(ext):
            return ext
    return None


def validate_document(data: bytes, chunk_type: BrandChunkType) -> _Checked:
    """Decode, chunk and scan the file (CPU work: run it off the event loop)."""
    if not data:
        raise ChunkingError("file", "the file is empty")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ChunkingError("file", "the file is not UTF-8 text") from None
    if "\x00" in text:
        raise ChunkingError("file", "the file contains NUL")
    drafts = chunk_document(text, chunk_type, limit=MAX_DOCUMENT_CHUNKS, field="file")
    check_chunks(drafts, field="file", max_chunks=MAX_DOCUMENT_CHUNKS)
    return _Checked(hashlib.sha256(data).hexdigest(), len(drafts))


def _invalid(field: str) -> tuple[int, dict[str, Any]]:
    return 422, {"field": field, "code": ErrorCode.VALIDATION_FAILED.value}


def _accepted(job_id: uuid.UUID, status: str) -> tuple[int, dict[str, Any]]:
    body = JobAccepted(job_id=job_id, status=status, poll_url=f"/api/v1/jobs/{job_id}")
    return 202, body.model_dump(mode="json")


async def _caps(
    session: Any,
    tenant_id: uuid.UUID,
    *,
    new_document: bool,
    new_chunks: int,
    skip: uuid.UUID | None,
) -> None:
    jobs = JobRunRepository(session, tenant_id)
    documents = BrandDocumentRepository(session, tenant_id)
    chunks = BrandChunkRepository(session, tenant_id)
    if await jobs.count_live(JobKind.BRAND_EMBED, source=SOURCE) >= LIVE_DOCUMENT_JOBS:
        raise BrandLimitError("live document jobs")
    live = await documents.count_live_documents()
    if live + (1 if new_document else 0) > MAX_LIVE_DOCUMENTS:
        raise BrandLimitError("live documents")
    active = await chunks.count_active_of_type(SOURCE)
    if skip is not None:
        active -= await chunks.count_active(SOURCE, skip, _model())
    if active + new_chunks > MAX_ACTIVE_DOCUMENT_CHUNKS:
        raise BrandLimitError("active document chunks")


def _model() -> str:
    from app.core.config import get_settings

    return get_settings().embedding_model


def _spec(principal: Principal, document_id: uuid.UUID, chunk_type: str) -> EnqueueSpec:
    return EnqueueSpec(
        kind=JobKind.BRAND_EMBED,
        entity_type="brand_document",
        entity_id=document_id,
        created_by=principal.user_id,
        payload={"source": SOURCE, "document_id": str(document_id), "chunk_type": chunk_type},
    )


async def _audit(session: Any, principal: Principal, action: str, document_id: uuid.UUID) -> None:
    await AuditRepository(session).write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action=action,
        entity_type="brand_document",
        entity_id=document_id,
    )


async def _precheck(principal: Principal) -> None:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        if await uow.job_runs.count_live(JobKind.BRAND_EMBED, source=SOURCE) >= LIVE_DOCUMENT_JOBS:
            raise BrandLimitError("live document jobs")
        if await uow.brand_documents.count_live_documents() >= MAX_LIVE_DOCUMENTS:
            raise BrandLimitError("live documents")


async def _peek(
    principal: Principal, key: str, endpoint: str, digest: bytes
) -> UploadResult | None:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        found = await IdempotencyKeyRepository(uow.session, principal.tenant_id).peek(
            key, endpoint, digest, principal.user_id
        )
    if isinstance(found, Replay):
        return UploadResult(found.status, found.body)
    if isinstance(found, Mismatch):
        raise key_reused()
    return None


async def _dedupe(
    principal: Principal,
    key: str,
    path: str,
    payload: bytes,
    checked: _Checked,
    chunk_type: str,
) -> UploadResult | None:
    """A live copy of the same bytes: replay its job, or re-queue it when it `failed`."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        copy = await uow.brand_documents.get_live_by_hash(checked.content_hash)
        copy_job = (
            None
            if copy is None or copy.job_id is None
            else await uow.job_runs.get_by_id(copy.job_id)
        )
    if copy is None:
        return None
    copy_id = copy.id
    if copy.status != "failed":
        job_status = "queued" if copy_job is None else copy_job.status
        job_id = copy.job_id or uuid.uuid4()

        async def replay(_session: Any) -> tuple[int, dict[str, Any]]:
            return _accepted(job_id, job_status)

        status, body = await idempotent(principal, key, "POST", path, payload, replay)
        return UploadResult(status, body)

    async def requeue(session: Any) -> tuple[int, dict[str, Any]]:
        tenant = principal.tenant_id
        await JobRunRepository(session, tenant).lock_brand_upload()
        await _caps(
            session, tenant, new_document=False, new_chunks=checked.draft_count, skip=copy_id
        )
        job = (
            await JobRunRepository(session, tenant).enqueue(_spec(principal, copy_id, chunk_type))
        ).job
        if not await BrandDocumentRepository(session, tenant).requeue_failed(copy_id, job.id):
            # another request re-queued (or deleted) the copy first: roll back, read it again
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "brand_documents_status")
        await _audit(session, principal, "brand_document.uploaded", copy_id)
        return _accepted(job.id, job.status)

    status, body = await idempotent(principal, key, "POST", path, payload, requeue)
    return UploadResult(status, body)


async def accept_upload(
    principal: Principal,
    key: str,
    path: str,
    filename: str | None,
    data: bytes,
    chunk_type: BrandChunkType,
) -> UploadResult:
    payload = chunk_type.encode() + b"\x00" + data
    endpoint = f"POST {path}"
    digest = request_hash("POST", path, payload)
    replayed = await _peek(principal, key, endpoint, digest)
    if replayed is not None:
        return replayed
    await _precheck(principal)

    name = clean_filename(filename)
    ext = None if name is None else _extension(name)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise PayloadTooLargeError("file")
    if name is None or ext is None:
        return await _store_invalid(principal, key, path, payload, "file")
    try:
        checked = await asyncio.to_thread(validate_document, data, chunk_type)
    except ChunkingError as exc:
        return await _store_invalid(principal, key, path, payload, exc.field)

    try:
        found = await _dedupe(principal, key, path, payload, checked, chunk_type)
    except ConflictError as exc:
        if exc.code is ErrorCode.IDEMPOTENCY_KEY_REUSED:
            raise
        found = await _dedupe(principal, key, path, payload, checked, chunk_type)
    if found is not None:
        return found

    document_id = uuid.uuid4()
    object_key = brand_document_object_key(principal.tenant_id, document_id)
    await get_document_store().put(brand_bucket(), object_key, data, CONTENT_TYPES[ext])

    async def create(session: Any) -> tuple[int, dict[str, Any]]:
        tenant = principal.tenant_id
        jobs = JobRunRepository(session, tenant)
        await jobs.lock_brand_upload()
        await _caps(session, tenant, new_document=True, new_chunks=checked.draft_count, skip=None)
        job = (await jobs.enqueue(_spec(principal, document_id, chunk_type))).job
        await BrandDocumentRepository(session, tenant).create(
            id=document_id,
            filename=name,
            content_type=CONTENT_TYPES[ext],
            bytes_=len(data),
            content_hash=checked.content_hash,
            storage_key=object_key,
            job_id=job.id,
            created_by=principal.user_id,
        )
        await _audit(session, principal, "brand_document.uploaded", document_id)
        return _accepted(job.id, job.status)

    try:
        status, body = await idempotent(principal, key, "POST", path, payload, create)
    except ConflictError as exc:
        if exc.code is ErrorCode.IDEMPOTENCY_KEY_REUSED:
            raise
        # a concurrent identical upload won the live-hash unique: replay it
        log.info("brand_document.duplicate_upload", document_id=str(document_id))
        again = await _dedupe(principal, key, path, payload, checked, chunk_type)
        if again is None:
            raise
        return again
    return UploadResult(status, body)


async def _store_invalid(
    principal: Principal, key: str, path: str, payload: bytes, field: str
) -> UploadResult:
    async def handler(_session: Any) -> tuple[int, dict[str, Any]]:
        return _invalid(field)

    status, body = await idempotent(principal, key, "POST", path, payload, handler)
    return UploadResult(status, body)


async def delete_document(principal: Principal, document_id: uuid.UUID) -> None:
    """Soft-delete the document and deactivate its chunks in one transaction (with the audit
    row), then a best-effort delete of the object; a failure there is logged and leaves an
    orphan (CP10)."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        document = await uow.brand_documents.get_live(document_id)
        if document is None or not await uow.brand_documents.soft_delete(document_id):
            raise NotFoundError("document_id")
        storage_key = document.storage_key
        await uow.brand_chunks.deactivate_source(SOURCE, document_id)
        await _audit(uow.session, principal, "brand_document.deleted", document_id)
    try:
        await get_document_store().delete(brand_bucket(), storage_key)
    except Exception as exc:  # best effort: the row is already gone for the tenant
        log.warning(
            "brand_document.object_delete_failed",
            document_id=str(document_id),
            error_class=type(exc).__name__,
        )


def stored_invalid_field(body: dict[str, Any]) -> str:
    field = body.get("field")
    return field if isinstance(field, str) and field else "file"


__all__ = [
    "MAX_DOCUMENT_BYTES",
    "BrandLimitError",
    "PayloadTooLargeError",
    "UploadResult",
    "accept_upload",
    "delete_document",
]


@dataclass(frozen=True, slots=True)
class DocumentView:
    id: uuid.UUID
    filename: str
    status: str
    chunk_count: int
    error_report: dict[str, Any] | None
    created_at: Any


@dataclass(frozen=True, slots=True)
class DocumentList:
    items: list[DocumentView]
    has_more: bool


async def list_documents(
    principal: Principal, *, limit: int, after: tuple[Any, uuid.UUID] | None
) -> DocumentList:
    """The tenant's live documents, newest first (keyset on `created_at DESC, id DESC`)."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        page = await uow.brand_documents.list_page(limit=limit, after=after)
    items = [
        DocumentView(
            id=d.id,
            filename=d.filename,
            status=d.status,
            chunk_count=d.chunk_count,
            error_report=d.error_report_json if d.status == "failed" else None,
            created_at=d.created_at,
        )
        for d in page.items
    ]
    return DocumentList(items=items, has_more=page.has_more)
