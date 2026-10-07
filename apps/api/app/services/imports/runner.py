"""The `bulk_import` job body (DATABASE.md:954-976; BACKEND-ARCHITECTURE §3.3; S13 B9-B12, PD10,
Q7, Q12).

`run_import(ctx)` reads the import's CSV object (or, for a rows retry, the failed rows' stored
cells), builds **units** and runs them in batches, one transaction per batch:

- A unit is one standalone row, or one whole family: every row whose `parent_style_id` names the
  same style, plus the row whose `seller_sku` is that style, in order of first appearance. A
  family never spans two commits, even when its rows are scattered through the file (Q12).
- A batch is whole units up to `BATCH_ROWS` rows (a larger family is a batch of its own). Each
  batch commit also writes the job's progress (`result_json = {import_id, progress_pct}`, Q8),
  fenced on the claimed attempt, so a worker whose lease was reaped stops at its next commit.
- A resumed run (after a crash or a shutdown abort) skips every unit whose rows all have a
  finished `import_rows` row and re-runs the rest; every write is an upsert, so a re-run is
  idempotent.

Each row (B4 → duplicate SKU → category lookup → writes) runs in its own SAVEPOINT: the product
upsert, its facts (FactWriter), its parties, its colourway and matrix cell, and its `imported`
`import_rows` row. A database refusal rolls back that row alone and writes a `failed` row whose
message names the A.6 column from a fixed table, never the cell or the driver message. Rows are
written `imported` or `failed`, never `pending`. The body never starts generation (T67).

A family is two passes (DATABASE.md:971-974, T17): pass 1 upserts the style (its title, brand
and classification must agree across the family, else `CROSS_FIELD_MISMATCH` on the row that
disagrees) and its colourways; pass 2 writes each child with its available cell, then one
derived `not_provided` cell per missing colour x size, so the matrix is complete. Parties
(B11): the manufacturer is matched on its dedupe key with the address kept whole (a PIN is
taken only when exactly one trails it); consumer care equal to the brand default is inherited
(no product override), otherwise a nameless consumer-care party, never an invented name.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, JobKind
from mkos_schemas.errors import MAX_ERROR_ITEMS, ErrorItem, ErrorReport

from app.adapters.storage import (
    StorageNotFound,
    StorageUnavailable,
    csv_bucket,
    get_storage,
)
from app.core.errors import InputError, RowCountError
from app.core.logging import get_logger
from app.db.models.ops import Import
from app.repositories.facts import FactWriter
from app.repositories.imports import (
    ImportRepository,
    ImportRowRepository,
    RowWrite,
    mapped_categories,
)
from app.repositories.jobs import JobRunRepository
from app.repositories.parties import BrandProfileRepository, BusinessPartyRepository, PartyFields
from app.repositories.products import ProductRepository, ProductUpsert
from app.repositories.transactions import RowDbError, row_savepoint, unit_of_work
from app.repositories.variants import ColourwayRepository, MatrixCellRepository
from app.services.imports.csv_file import CsvRow, ImportFileError, iter_rows
from app.services.imports.db_errors import column_for_field, row_db_item
from app.services.imports.row_model import (
    ProductImportRow,
    RowResult,
    normalize_row,
    row_error,
    validate_row,
)
from app.services.product_facts import fact_stamp
from app.workers.errors import JobFailed, RetryableJobError, system_report
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
BATCH_ROWS: Final = 100
MAX_SIZE_RUN: Final = 40
_PIN: Final = re.compile(r"(?<![0-9])([1-9][0-9]{5})(?![0-9])")
_TRAILING_PIN: Final = re.compile(r"(?<![0-9])([1-9][0-9]{5})[\s.,;:)-]*$")
_DIGITS: Final = re.compile(r"[^0-9]")


class StaleAttempt(Exception):  # noqa: N818 - control flow: this worker lost its lease
    """The job's progress write was fenced off: a newer attempt owns the job."""


# ---- units (Q12) ----


@dataclass
class Prepared:
    """One CSV row after the row model: its cells as uploaded, and either the typed row or its
    errors (a row the CSV parser already refused carries only that error)."""

    row_number: int
    raw: dict[str, str]
    sku: str | None
    parent_sku: str | None
    result: RowResult
    errors: list[ErrorItem] = field(default_factory=list)

    @property
    def row(self) -> ProductImportRow | None:
        return None if self.errors else self.result.row


@dataclass
class Unit:
    family: str | None
    rows: list[Prepared]

    @property
    def row_numbers(self) -> set[int]:
        return {p.row_number for p in self.rows}


def _prepare(csv_row: CsvRow) -> Prepared:
    if csv_row.error is not None:
        code, message = csv_row.error
        item = row_error(code, message)
        return Prepared(csv_row.row_number, {}, None, None, RowResult(None, [item]), [item])
    norm = normalize_row(csv_row.cells)
    result = validate_row(norm)
    return Prepared(
        csv_row.row_number,
        dict(csv_row.cells),
        norm.get("seller_sku"),
        norm.get("parent_style_id"),
        result,
        list(result.errors),
    )


def build_units(rows: Iterable[CsvRow]) -> list[Unit]:
    prepared = [_prepare(r) for r in rows]
    families = {p.parent_sku for p in prepared if p.parent_sku}
    units: list[Unit] = []
    by_family: dict[str, Unit] = {}
    for p in prepared:
        key = p.parent_sku or (p.sku if p.sku in families else None)
        if key is None:
            units.append(Unit(None, [p]))
            continue
        if key not in by_family:
            by_family[key] = Unit(key, [])
            units.append(by_family[key])
        by_family[key].rows.append(p)
    return units


def batches(units: Sequence[Unit], size: int | None = None) -> list[list[Unit]]:
    size = BATCH_ROWS if size is None else size
    out: list[list[Unit]] = []
    current: list[Unit] = []
    rows = 0
    for unit in units:
        if current and rows + len(unit.rows) > size:
            out.append(current)
            current, rows = [], 0
        current.append(unit)
        rows += len(unit.rows)
    if current:
        out.append(current)
    return out


def mark_duplicates(units: Sequence[Unit], earlier: dict[str, int] | None = None) -> None:
    """`DUPLICATE_SKU_IN_FILE` on every repeat of a SKU after its first row (file order); a
    rows retry passes the imported rows' SKUs, which stay the first occurrences."""
    first: dict[str, int] = dict(earlier or {})
    for p in sorted((p for u in units for p in u.rows), key=lambda p: p.row_number):
        if p.row is None or p.sku is None:
            continue
        seen = first.get(p.sku)
        if seen is not None and seen < p.row_number:
            p.errors.append(
                row_error(ErrorCode.DUPLICATE_SKU_IN_FILE, "seller_sku repeats an earlier row")
            )
        elif seen is None:
            first[p.sku] = p.row_number


# ---- facts ----


class Facts:
    """Seller facts for one run: `source=seller`, `captured_via=import`, one timestamp."""

    def __init__(self, stamp: str) -> None:
        self.stamp = stamp

    def provenance(self) -> dict[str, Any]:
        return {"source": "seller", "captured_via": "import", "updated_at": self.stamp}

    def fact(self, value: Any = None, normalized: Any = None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if value is not None:
            out["value"] = value
        if normalized is not None:
            out["normalized"] = normalized
        return {**out, **self.provenance()}


# ---- one run ----


@dataclass
class Run:
    ctx: JobContext
    imp: Import
    facts: Facts
    total: int
    done: int = 0
    mapped: set[str] = field(default_factory=set)
    brand_care: Any = None
    brand_care_loaded: bool = False

    @property
    def tenant(self) -> uuid.UUID:
        return self.ctx.tenant_id

    @property
    def evidence(self) -> str:
        return f"import:{self.imp.id}"


def _failure(code: ErrorCode, message: str, ctx: JobContext) -> JobFailed:
    return JobFailed(
        system_report(
            code,
            message,
            attempt=ctx.attempt,
            max_attempts=POLICIES[JobKind.BULK_IMPORT].max_attempts,
            request_id=ctx.request_id,
        )
    )


def _report(errors: Sequence[ErrorItem], warnings: Sequence[ErrorItem]) -> dict[str, Any] | None:
    items = list(errors) + list(warnings)
    if not items:
        return None
    report = ErrorReport(
        status="failed" if errors else "passed",
        error_code=errors[0].error_code if errors else None,
        retryable=False,
        errors=items[:MAX_ERROR_ITEMS],
    )
    return report.model_dump(mode="json")


async def _load(ctx: JobContext) -> tuple[Import, set[int], list[CsvRow] | None, dict[str, int]]:
    """The import (while this job owns it), the finished rows, and for a rows retry the failed
    rows' stored cells plus the imported rows' SKUs."""
    async with unit_of_work(ctx.tenant_id) as session:
        jobs = JobRunRepository(session, ctx.tenant_id)
        job = await jobs.get_by_id(ctx.job_run_id)
        if job is None:
            raise _failure(ErrorCode.IMPORT_FAILED, "the import job is missing", ctx)
        imports = ImportRepository(session, ctx.tenant_id)
        imp = await imports.get_for_job(job.entity_id, ctx.job_run_id)
        if imp is None:
            raise _failure(ErrorCode.IMPORT_FAILED, "the import belongs to another job", ctx)
        await imports.mark_running(imp.id, ctx.job_run_id)
        rows = ImportRowRepository(session, ctx.tenant_id)
        if not ctx.payload.get("retry"):
            return imp, await rows.finished_row_numbers(imp.id), None, {}
        failed = await rows.failed_rows(imp.id)
        retry = [
            CsvRow(r.row_number, {str(k): str(v) for k, v in (r.raw_row or {}).items()})
            for r in failed
        ]
        imported = await rows.imported_skus(imp.id)
        return imp, set(), retry, imported


async def _read_object(run_ctx: JobContext, imp: Import) -> bytes:
    if not imp.storage_key.startswith(f"{run_ctx.tenant_id}/imports/"):
        raise _failure(ErrorCode.IMPORT_FAILED, "the import object is outside the tenant", run_ctx)
    try:
        return await get_storage().get(csv_bucket(), imp.storage_key)
    except StorageNotFound:
        raise _failure(ErrorCode.IMPORT_FAILED, "the import file is missing", run_ctx) from None
    except StorageUnavailable:
        raise RetryableJobError(ErrorCode.TIMEOUT) from None


async def run_import(ctx: JobContext) -> Outcome:
    imp, finished, retry_rows, imported_skus = await _load(ctx)
    if retry_rows is None:
        data = await _read_object(ctx, imp)
        try:
            csv_rows = list(iter_rows(data))
        except ImportFileError:
            raise _failure(
                ErrorCode.IMPORT_FAILED, "the import file is not readable", ctx
            ) from None
    else:
        csv_rows = retry_rows
    units = build_units(csv_rows)
    mark_duplicates(units, imported_skus)
    todo = [u for u in units if not u.row_numbers <= finished]
    run = Run(ctx, imp, Facts(fact_stamp(datetime.now(UTC))), total=len(csv_rows))
    run.done = sum(len(u.rows) for u in units) - sum(len(u.rows) for u in todo)
    for batch in batches(todo):
        await _run_batch(run, batch)
    async with unit_of_work(ctx.tenant_id) as session:
        totals = await ImportRowRepository(session, ctx.tenant_id).totals(imp.id)
    result = {
        "import_id": str(imp.id),
        "progress_pct": 100,
        "total_rows": totals.total,
        "imported": totals.imported,
        "failed": totals.failed,
    }
    log.info(
        "import.finished",
        import_id=str(imp.id),
        imported=totals.imported,
        failed=totals.failed,
        retry=bool(retry_rows is not None),
    )
    return Outcome("partial" if totals.failed else "succeeded", result)


async def _run_batch(run: Run, batch: list[Unit]) -> None:
    async with unit_of_work(run.tenant) as session:
        categories = {p.row.category for u in batch for p in u.rows if p.row is not None}
        run.mapped = await mapped_categories(session, categories)
        writer = _Writer(run, session)
        for unit in batch:
            if unit.family is None:
                await writer.standalone(unit.rows[0])
            else:
                await writer.family(unit)
        run.done += sum(len(u.rows) for u in batch)
        progress = min(99, (100 * run.done) // max(run.total, 1))
        fenced = await JobRunRepository(session, run.tenant).set_result(
            run.ctx.job_run_id,
            run.ctx.attempt,
            {"import_id": str(run.imp.id), "progress_pct": progress},
        )
        if not fenced:
            raise StaleAttempt("a newer attempt owns the job")
    log.info("import.batch_committed", import_id=str(run.imp.id), rows=run.done)


# ---- the writes of one batch ----


class _Writer:
    def __init__(self, run: Run, session: Any) -> None:
        self.run = run
        self.session = session
        tenant = run.tenant
        self.products = ProductRepository(session, tenant)
        self.facts = FactWriter(session, tenant)
        self.rows = ImportRowRepository(session, tenant)
        self.parties = BusinessPartyRepository(session, tenant)
        self.colourways = ColourwayRepository(session, tenant)
        self.cells = MatrixCellRepository(session, tenant)

    # -- import_rows --

    async def _write_row(
        self,
        p: Prepared,
        status: str,
        product_id: uuid.UUID | None = None,
        errors: Sequence[ErrorItem] = (),
    ) -> None:
        await self.rows.write(
            self.run.imp.id,
            self.run.ctx.job_run_id,
            RowWrite(
                row_number=p.row_number,
                status=status,
                raw_row=p.raw,
                sku=p.sku[:500] if p.sku else None,
                parent_sku=p.parent_sku[:500] if p.parent_sku else None,
                product_id=product_id,
                error_report_json=_report(errors, p.result.warnings),
            ),
        )

    async def _fail(self, p: Prepared, errors: Sequence[ErrorItem]) -> None:
        await self._write_row(p, "failed", errors=errors)

    def _precheck(self, p: Prepared) -> list[ErrorItem]:
        if p.errors:
            return p.errors
        row = p.row
        assert row is not None  # noqa: S101 - no errors means a typed row
        if row.category not in self.run.mapped:
            return [
                row_error(
                    ErrorCode.CATEGORY_NOT_MAPPED,
                    "category has no marketplace template yet",
                    source=ErrorSource.RULES_ENGINE,
                )
            ]
        return []

    async def _guarded(self, p: Prepared, write: Any) -> uuid.UUID | None:
        """`write()` in the row's savepoint; a refusal becomes the row's `failed` result."""
        try:
            async with row_savepoint(self.session):
                product_id: uuid.UUID = await write()
                await self._write_row(p, "imported", product_id)
            return product_id
        except RowDbError as exc:
            await self._fail(p, [row_db_item(exc.sqlstate, exc.constraint)])
        except InputError as exc:
            await self._fail(
                p,
                [
                    row_error(
                        exc.code, f"{column_for_field(exc.field)} was refused by the dictionary"
                    )
                ],
            )
        except RowCountError:
            await self._fail(
                p, [row_error(ErrorCode.VALUE_NOT_ALLOWED, "the row could not be written")]
            )
        return None

    # -- standalone (B9) --

    async def standalone(self, p: Prepared) -> None:
        problems = self._precheck(p)
        if problems:
            await self._fail(p, problems)
            return
        row = p.row
        assert row is not None  # noqa: S101

        async def write() -> uuid.UUID:
            done = await self.products.upsert_by_sku(self._upsert(row))
            await self._row_facts(done.id, row, level="standalone")
            return done.id

        await self._guarded(p, write)

    def _upsert(self, row: ProductImportRow, **family: Any) -> ProductUpsert:
        return ProductUpsert(
            sku=family.pop("sku", row.sku),
            title=row.title,
            category=row.category,
            stamp=self.run.facts.provenance(),
            brand_name=row.brand_name,
            sub_category=row.sub_category,
            product_type=row.product_type,
            gender=row.gender,
            source_image_urls=row.image_urls,
            **family,
        )

    async def _row_facts(self, product_id: uuid.UUID, row: ProductImportRow, *, level: str) -> None:
        f = self.run.facts
        w = self.facts
        for column in ("mrp", "selling_price", "tax_rate", "hsn_code", "country_of_origin"):
            value = getattr(row, column)
            if value is not None:
                await w.set_typed(product_id, column, value, f.provenance())
        if row.identifier_value is not None and level != "style":
            await w.set_typed_group(
                product_id,
                {
                    "identifier_type": row.identifier_type,
                    "identifier_value": row.identifier_value,
                    "identifier_status": "provided",
                },
                f.provenance(),
            )
        if row.package_length_cm is not None:
            await w.set_typed_group(
                product_id,
                {
                    "package_length_cm": row.package_length_cm,
                    "package_breadth_cm": row.package_breadth_cm,
                    "package_height_cm": row.package_height_cm,
                },
                f.provenance(),
            )
        if row.package_weight_g is not None:
            await w.set_typed(product_id, "package_weight_g", row.package_weight_g, f.provenance())
        for key, value in sorted(row.attributes.items()):
            await w.set_attribute(product_id, key, f.fact(value))
        if level in ("standalone", "style") and row.size_system is not None:
            await w.set_attribute(product_id, "size_system", f.fact(row.size_system))
        if level == "standalone":
            if row.color_name is not None:
                await w.set_attribute(
                    product_id, "color", f.fact(row.color_name, row.normalized_color)
                )
            if row.size_name is not None or row.size_key is not None:
                await w.set_attribute(product_id, "size", f.fact(row.size_name, row.size_key))
        await self._parties(product_id, row)

    # -- parties (B11) --

    async def _parties(self, product_id: uuid.UUID, row: ProductImportRow) -> None:
        f = self.run.facts
        if row.manufacturer_name is not None:
            address = row.manufacturer_address
            party = await self.parties.match_or_create(
                PartyFields(
                    role="manufacturer",
                    legal_name=row.manufacturer_name,
                    address_line1=address,
                    postal_code=unambiguous_pin(address),
                )
            )
            await self.facts.set_typed(
                product_id,
                "manufacturer_party_id",
                party,
                f.fact(address if address is not None else row.manufacturer_name),
            )
        if row.consumer_care_email is not None or row.consumer_care_phone is not None:
            if await self._is_brand_care(row):
                return  # inherited from the brand default: no product override
            party = await self.parties.match_or_create(
                PartyFields(
                    role="consumer_care",
                    email=row.consumer_care_email,
                    phone=row.consumer_care_phone,
                )
            )
            await self.facts.set_typed(product_id, "consumer_care_party_id", party, f.provenance())

    async def _is_brand_care(self, row: ProductImportRow) -> bool:
        if not self.run.brand_care_loaded:
            self.run.brand_care = await BrandProfileRepository(
                self.session, self.run.tenant
            ).consumer_care()
            self.run.brand_care_loaded = True
        care = self.run.brand_care
        if care is None:
            return False
        return same_contact(
            care.email, care.phone, row.consumer_care_email, row.consumer_care_phone
        )

    # -- families (B10) --

    async def family(self, unit: Unit) -> None:
        key = unit.family
        assert key is not None  # noqa: S101
        for p in unit.rows:
            p.errors = self._precheck(p)
        valid = [p for p in unit.rows if not p.errors]
        style_row = next((p for p in valid if p.sku == key and p.parent_sku is None), None)
        children = [p for p in unit.rows if p is not style_row and p.parent_sku == key]
        orphans = [p for p in unit.rows if p is not style_row and p not in children]
        for p in orphans:  # a second row claiming the style SKU without a parent
            if not p.errors:
                p.errors = [
                    row_error(ErrorCode.DUPLICATE_SKU_IN_FILE, "seller_sku repeats an earlier row")
                ]
        lead = style_row or next((p for p in children if not p.errors), None)
        if lead is None or lead.row is None:
            for p in unit.rows:
                await self._fail(
                    p,
                    p.errors
                    or [row_error(ErrorCode.VARIATION_INVALID, "the family has no valid row")],
                )
            return
        style = lead.row
        live = [p for p in children if not p.errors]
        self._agree(style, live)
        live = [p for p in live if not p.errors]
        axes = self._axes(live)
        live = [p for p in live if not p.errors]
        sizes: list[str] = []
        colours: dict[str, str] = {}
        for p in live:
            row = p.row
            assert row is not None  # noqa: S101
            if "size" in axes and row.size_key not in sizes:
                sizes.append(row.size_key or "")
            if "color" in axes and row.colour_key not in colours:
                colours[row.colour_key or ""] = row.color_name or ""
        theme = {("size",): "size", ("color",): "color", ("color", "size"): "size_color"}.get(
            tuple(sorted(axes))
        )
        if theme is None or len(sizes) > MAX_SIZE_RUN:
            reason = (
                "a family needs size_name or color_name on every child"
                if theme is None
                else f"a family has at most {MAX_SIZE_RUN} sizes"
            )
            for p in unit.rows:
                await self._fail(p, p.errors or [row_error(ErrorCode.VARIATION_INVALID, reason)])
            return

        # pass 1: the style and its colourways
        style_id: uuid.UUID | None = None
        colourway_ids: dict[str, uuid.UUID] = {}

        async def write_style() -> uuid.UUID:
            nonlocal style_id
            done = await self.products.upsert_by_sku(
                self._upsert(
                    style,
                    sku=key,
                    variation_theme=theme,
                    size_run=tuple(sizes) if "size" in axes else None,
                )
            )
            style_id = done.id
            if style_row is not None:
                await self._row_facts(done.id, style, level="style")
            elif style.size_system is not None:
                await self.facts.set_attribute(
                    done.id, "size_system", self.run.facts.fact(style.size_system)
                )
            for position, (colour_key, name) in enumerate(colours.items(), start=1):
                normalized = next(
                    (
                        p.row.normalized_color
                        for p in live
                        if p.row is not None and p.row.colour_key == colour_key
                    ),
                    None,
                )
                colourway_ids[colour_key] = await self.colourways.upsert(
                    done.id, self.run.facts.fact(name, normalized), position
                )
            return done.id

        try:
            async with row_savepoint(self.session):
                await write_style()
                if style_row is not None:
                    await self._write_row(style_row, "imported", style_id)
        except (RowDbError, InputError, RowCountError) as exc:
            item = self._item(exc)
            for p in unit.rows:
                await self._fail(p, p.errors or [item])
            return
        assert style_id is not None  # noqa: S101

        # pass 2: the children and their cells
        for p in children:
            if p.errors:
                await self._fail(p, p.errors)
                continue
            await self._child(p, style_id, style, theme, colourway_ids)
        for p in orphans:
            await self._fail(p, p.errors)
        await self._fill_gaps(style_id, theme, sizes, colourway_ids)

    def _item(self, exc: Exception) -> ErrorItem:
        if isinstance(exc, RowDbError):
            return row_db_item(exc.sqlstate, exc.constraint)
        if isinstance(exc, InputError):
            return row_error(
                exc.code, f"{column_for_field(exc.field)} was refused by the dictionary"
            )
        return row_error(ErrorCode.VALUE_NOT_ALLOWED, "the row could not be written")

    def _agree(self, style: ProductImportRow, live: list[Prepared]) -> None:
        """Title, brand and classification must agree across the family (DATABASE.md:971)."""
        for p in live:
            row = p.row
            assert row is not None  # noqa: S101
            for column in (
                "title",
                "category",
                "brand_name",
                "sub_category",
                "product_type",
                "gender",
            ):
                mine, theirs = getattr(row, column), getattr(style, column)
                if mine is not None and theirs is not None and mine != theirs:
                    shown = "product_name" if column == "title" else column
                    p.errors = [
                        row_error(
                            ErrorCode.CROSS_FIELD_MISMATCH,
                            f"{shown} differs from the rest of its parent_style_id family",
                        )
                    ]
                    break

    def _axes(self, live: list[Prepared]) -> set[str]:
        """The family's axes (size, colour); a child without one of them, or one whose axes repeat
        another child's after case-folding (`M/Navy` vs `m/navy`), is `VARIATION_INVALID`."""
        axes: set[str] = set()
        for p in live:
            row = p.row
            assert row is not None  # noqa: S101
            if row.size_key is not None:
                axes.add("size")
            if row.colour_key is not None:
                axes.add("color")
        seen: set[tuple[str | None, str | None]] = set()
        for p in live:
            row = p.row
            assert row is not None  # noqa: S101
            mine = {
                a for a, v in (("size", row.size_key), ("color", row.colour_key)) if v is not None
            }
            if mine != axes:
                p.errors = [
                    row_error(
                        ErrorCode.VARIATION_INVALID,
                        "every child of a family needs the same size_name/color_name columns",
                    )
                ]
                continue
            variant = (row.size_key, row.colour_key)
            if variant in seen:
                p.errors = [
                    row_error(
                        ErrorCode.VARIATION_INVALID,
                        "size_name and color_name repeat another child of the family",
                    )
                ]
                continue
            seen.add(variant)
        return axes

    async def _child(
        self,
        p: Prepared,
        style_id: uuid.UUID,
        style: ProductImportRow,
        theme: str,
        colourway_ids: dict[str, uuid.UUID],
    ) -> None:
        row = p.row
        assert row is not None  # noqa: S101
        variant: dict[str, str] = {}
        if row.size_key is not None and theme in ("size", "size_color"):
            variant["size"] = row.size_key
        if row.colour_key is not None and theme in ("color", "size_color"):
            variant["color"] = row.colour_key

        async def write() -> uuid.UUID:
            upsert = ProductUpsert(
                sku=row.sku,
                title=row.title,
                category=style.category,
                stamp=self.run.facts.provenance(),
                variant_attributes=variant,
                source_image_urls=row.image_urls,
            )
            done = await self.products.upsert_by_sku(upsert, parent_product_id=style_id)
            if "size" in variant:
                await self.facts.set_attribute(
                    done.id, "size", self.run.facts.fact(row.size_name, row.size_key)
                )
            await self._row_facts(done.id, row, level="variant")
            await self.cells.upsert(
                style_id,
                colourway_ids.get(variant["color"]) if "color" in variant else None,
                variant.get("size"),
                done.id,
            )
            await self.cells.check_now()
            return done.id

        await self._guarded(p, write)

    async def _fill_gaps(
        self,
        style_id: uuid.UUID,
        theme: str,
        sizes: list[str],
        colourway_ids: dict[str, uuid.UUID],
    ) -> None:
        if theme != "size_color":
            return
        async with row_savepoint(self.session):
            for colourway in colourway_ids.values():
                for size in sizes:
                    await self.cells.fill_gap(style_id, colourway, size, self.run.evidence)


# ---- helpers ----


def unambiguous_pin(address: str | None) -> str | None:
    """The PIN of a one-cell address only when exactly one 6-digit PIN appears and it trails."""
    if address is None:
        return None
    if len(_PIN.findall(address)) != 1:
        return None
    match = _TRAILING_PIN.search(address)
    return match.group(1) if match else None


def same_contact(
    email_a: str | None, phone_a: str | None, email_b: str | None, phone_b: str | None
) -> bool:
    """Equal when every contact the row gives matches the stored one (case-folded email, phone
    digits only)."""
    if email_b is not None and (email_a or "").lower() != email_b.lower():
        return False
    return phone_b is None or _DIGITS.sub("", phone_a or "") == _DIGITS.sub("", phone_b)
