"""`GET /api/v1/imports/{import_id}` and `/rows` (S13 B13; Q8): the import report and its rows,
the caller's tenant only (another tenant's import reads as missing).

While the job is in flight (`queued`, `running`), `imported` and `failed` are the terminal
`import_rows` counted so far and `total_rows` is their sum, so the report's own check holds;
progress is the job envelope's (`result_json`). Once the import is terminal the stored totals are
returned. `rows` is always empty here, and no row's `raw_row` is ever read for the API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.imports import ImportPhotoTotals, ImportReport, ImportRowResult, PhotoLinkResult

from app.core.errors import NotFoundError
from app.core.rbac import Principal
from app.db.models.ops import Import, ImportRow
from app.repositories.imports import ImportRepository, ImportRowRepository
from app.repositories.transactions import unit_of_work
from app.repositories.uow import repositories_on
from app.services import photo_links

IN_FLIGHT = frozenset({"queued", "running"})


@dataclass(frozen=True, slots=True)
class RowsPage:
    rows: list[ImportRowResult]
    has_more: bool
    last: ImportRow | None


def _report(
    imp: Import, imported: int, failed: int, total: int, photos: ImportPhotoTotals | None = None
) -> ImportReport:
    error = (
        ErrorReport.model_validate(imp.error_report_json)
        if imp.error_report_json is not None
        else None
    )
    return ImportReport(
        import_id=imp.id,
        total_rows=total,
        imported=imported,
        failed=failed,
        rows=[],
        error=error,
        photos=photos,
    )


async def get_report(principal: Principal, import_id: uuid.UUID) -> ImportReport:
    async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
        imp = await ImportRepository(session, principal.tenant_id).get(import_id)
        if imp is None:
            raise NotFoundError("import_id")
        photos = await photo_links.import_totals(
            repositories_on(session, principal.tenant_id), imp.id
        )
        if imp.status in IN_FLIGHT or imp.total_rows is None:
            totals = await ImportRowRepository(session, principal.tenant_id).totals(imp.id)
            return _report(imp, totals.imported, totals.failed, totals.total, photos)
        return _report(
            imp, imp.imported_rows, imp.failed_rows, imp.imported_rows + imp.failed_rows, photos
        )


def _row(row: ImportRow, photos: list[PhotoLinkResult] | None = None) -> ImportRowResult:
    items: list[ErrorItem] = []
    if row.error_report_json is not None:
        items = ErrorReport.model_validate(row.error_report_json).errors
    return ImportRowResult(
        row_number=row.row_number,
        sku=row.sku,
        status="failed" if row.status == "failed" else "imported",
        product_id=row.product_id,
        errors=items,
        photos=photos or [],
    )


async def rows_page(
    principal: Principal,
    import_id: uuid.UUID,
    *,
    status: str | None,
    after_row: uuid.UUID | None,
    limit: int,
) -> RowsPage | None:
    """The page after the cursor's row (`after_row`, an `import_rows.id`); None when that row is
    not one of this import's (a forged or stale cursor)."""
    async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
        if await ImportRepository(session, principal.tenant_id).get(import_id) is None:
            raise NotFoundError("import_id")
        rows = ImportRowRepository(session, principal.tenant_id)
        after: int | None = None
        if after_row is not None:
            after = await rows.row_number_of(import_id, after_row)
            if after is None:
                return None
        found, more = await rows.page(import_id, status=status, after=after, limit=limit)
        # G2: the page's photo links in one SELECT
        photos = await photo_links.rows_photos(
            repositories_on(session, principal.tenant_id), import_id, [r.row_number for r in found]
        )
        return RowsPage(
            [_row(r, photos.get(r.row_number)) for r in found],
            more,
            found[-1] if found else None,
        )
