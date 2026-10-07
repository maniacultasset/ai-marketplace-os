"""`POST /api/v1/products/bulk` (BACKEND-ARCHITECTURE.md:536-615; DATABASE.md:1610-1641; S13 B8,
Q3, Q10, Q11).

`accept_upload(principal, key, path, filename, data)` runs in this order, and no transaction is
open while the file is parsed or the object is written:

1. `peek` the Idempotency-Key in its own short read: a completed replay returns the stored
   response; another body, member or endpoint is 409 `IDEMPOTENCY_KEY_REUSED`.
2. The unlocked live-import pre-check (PD13): over the cap is 429 and nothing is stored.
3. Parse the file and check its header. A file or header problem stores the one 422
   `IMPORT_FAILED` report under the key, in one short `idempotent` transaction; no object, no
   `imports` row and no job.
4. A new `import_id`, and the object `put` at `<tenant_id>/imports/<import_id>.csv`.
5. One short `idempotent` transaction: the key's `begin` (a concurrent first writer makes this a
   replay, and this request's object stays an orphan, PLAN §6), the tenant import lock and the
   cap, the `bulk_import` job (`http_key=None`, Q11), the `imports` row; then 202.

The request hash covers `POST <path>`, NUL and the file part's bytes (Q3). Logs carry ids and
counts only, never a cell or the filename.
"""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final

from mkos_schemas.enums import JobKind
from mkos_schemas.errors import ErrorReport

from app.adapters.storage import csv_bucket, get_storage, import_object_key
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.idempotency import IdempotencyKeyRepository, Mismatch, Replay
from app.repositories.imports import ImportRepository
from app.repositories.jobs import EnqueueSpec, JobRunRepository
from app.repositories.transactions import unit_of_work
from app.services.idempotency import idempotent, key_reused, request_hash
from app.services.imports.cap import enforce_live_cap
from app.services.imports.csv_file import ImportFileError, parse_upload
from app.services.imports.headers import check_header, header_report

log = get_logger(__name__)
API_V1: Final = "/api/v1"
CSV_CONTENT_TYPE: Final = "text/csv"
MAX_FILENAME: Final = 255
DEFAULT_FILENAME: Final = "upload.csv"


@dataclass(frozen=True, slots=True)
class UploadResult:
    """`(status, body)`: 202 with the `ImportAccepted` fields, or 422 with `{"report": ...}`
    (the file-level `ErrorReport` the route puts in the error envelope)."""

    status: int
    body: dict[str, Any]

    @property
    def report(self) -> ErrorReport | None:
        if self.status != 422:
            return None
        return ErrorReport.model_validate(self.body["report"])


def safe_filename(raw: str | None) -> str:
    """The basename only (no directories, either slash), NFC, control and format characters
    removed, at most 255 characters; a name with nothing left is `upload.csv`."""
    text = unicodedata.normalize("NFC", raw or "").replace("\\", "/")
    name = PurePosixPath(text).name
    name = "".join(ch for ch in name if unicodedata.category(ch) not in {"Cc", "Cf"}).strip()
    if name in {"", ".", ".."}:
        return DEFAULT_FILENAME
    return name[:MAX_FILENAME]


def accepted_body(import_id: uuid.UUID, job_id: uuid.UUID) -> dict[str, Any]:
    return {
        "import_id": str(import_id),
        "job_id": str(job_id),
        "status": "queued",
        "poll_url": f"{API_V1}/jobs/{job_id}",
    }


async def _peek_and_precheck(
    principal: Principal, key: str, path: str, data: bytes
) -> UploadResult | None:
    endpoint = f"POST {path}"
    digest = request_hash("POST", path, data)
    async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
        seen = await IdempotencyKeyRepository(session, principal.tenant_id).peek(
            key, endpoint, digest, principal.user_id
        )
        if isinstance(seen, Replay):
            return UploadResult(seen.status, seen.body)
        if isinstance(seen, Mismatch):
            raise key_reused()
        await enforce_live_cap(ImportRepository(session, principal.tenant_id))
    return None


def _file_problem(data: bytes) -> tuple[ErrorReport | None, int]:
    try:
        parsed = parse_upload(data)
    except ImportFileError as exc:
        return exc.report, 0
    problems = check_header(parsed.columns)
    return (header_report(problems) if problems else None), parsed.row_count


async def accept_upload(
    principal: Principal, key: str, path: str, filename: str | None, data: bytes
) -> UploadResult:
    replayed = await _peek_and_precheck(principal, key, path, data)
    if replayed is not None:
        return replayed

    report, row_count = _file_problem(data)
    if report is not None:
        refused = {"report": report.model_dump(mode="json")}

        async def store(_session: Any) -> tuple[int, dict[str, Any]]:
            return 422, refused

        status, body = await idempotent(principal, key, "POST", path, data, store)
        log.info("import.refused", tenant_id=str(principal.tenant_id), status=status)
        return UploadResult(status, body)

    tenant = principal.tenant_id
    import_id = uuid.uuid4()
    object_key = import_object_key(tenant, import_id)
    await get_storage().put(csv_bucket(), object_key, data, CSV_CONTENT_TYPE)
    name = safe_filename(filename)

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        imports = ImportRepository(session, tenant)
        await imports.lock_tenant_imports()
        await enforce_live_cap(imports)
        enqueued = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(
                kind=JobKind.BULK_IMPORT,
                entity_type="import",
                entity_id=import_id,
                created_by=principal.user_id,
            )
        )
        await imports.create(
            import_id=import_id,
            storage_key=object_key,
            filename=name,
            job_id=enqueued.job.id,
            created_by=principal.user_id,
        )
        return 202, accepted_body(import_id, enqueued.job.id)

    status, body = await idempotent(principal, key, "POST", path, data, handler)
    log.info(
        "import.accepted",
        tenant_id=str(tenant),
        import_id=body.get("import_id"),
        job_id=body.get("job_id"),
        rows=row_count,
    )
    return UploadResult(status, body)
