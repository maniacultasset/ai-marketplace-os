"""`validation_runs` (DATABASE.md:808-823; S18 T4): append-only, one row per stage per attempt in
the one `ErrorReport` shape (invariant 8).

- `insert(result, *, template_id, template_version, facts_digest)`: `passed` and the counts come
  from the report (`passed = blocking_count == 0`); a `rules_engine` row needs every pin
  (`vr_rules_pins`).
- `find_equal_pins(...)`: the newest `rules_engine` run of a version with exactly these pins (a
  repeated `/validate` writes nothing new).
- `summary_for(version_id)`: the version's runs as a `ValidationSummary` (the newest run decides
  `passed` and the counts), or None when it has none.
"""

from __future__ import annotations

import uuid
from typing import Any

from mkos_schemas.enums import ErrorSource, Marketplace
from mkos_schemas.errors import ErrorReport
from mkos_schemas.validation import ValidationResult, ValidationSummary

from app.db.models.runs import ValidationRun
from app.repositories.base import TenantScopedRepository


def _to_result(row: ValidationRun) -> ValidationResult:
    report = ErrorReport.model_validate(row.error_report_json)
    return ValidationResult(
        validation_run_id=row.id,
        tenant_id=row.tenant_id,
        listing_id=row.listing_id,
        listing_version_id=row.listing_version_id,
        marketplace=Marketplace(row.marketplace),
        source=ErrorSource(row.source),
        passed=report.passed,
        attempt=row.attempt,
        rules_version=row.rules_version,
        duration_ms=row.duration_ms or 0,
        report=report,
        created_at=row.created_at,
    )


class ValidationRunRepository(TenantScopedRepository[ValidationRun]):
    model = ValidationRun

    async def insert(
        self,
        result: ValidationResult,
        *,
        template_id: uuid.UUID | None,
        template_version: int | None,
        facts_digest: bytes | None,
        request_id: str | None = None,
    ) -> ValidationRun:
        report = result.report
        blocking = len(report.blocking)
        row = ValidationRun(
            id=result.validation_run_id or uuid.uuid4(),
            tenant_id=self.tenant_id,
            listing_id=result.listing_id,
            listing_version_id=result.listing_version_id,
            marketplace=Marketplace(result.marketplace).value,
            source=ErrorSource(result.source).value,
            passed=blocking == 0,
            error_report_json=report.model_dump(mode="json"),
            attempt=result.attempt,
            rules_version=result.rules_version,
            duration_ms=result.duration_ms,
            request_id=request_id,
            category_template_id=template_id,
            template_version=template_version,
            facts_digest=facts_digest,
            blocking_count=blocking,
            warning_count=len(report.errors) - blocking,
        )
        return await self.add(row)

    async def find_equal_pins(
        self,
        version_id: uuid.UUID,
        rules_version: int | None,
        template_id: uuid.UUID | None,
        template_version: int | None,
        facts_digest: bytes,
    ) -> ValidationRun | None:
        stmt = (
            self._select()
            .where(
                ValidationRun.listing_version_id == version_id,
                ValidationRun.source == ErrorSource.RULES_ENGINE.value,
                ValidationRun.rules_version.is_not_distinct_from(rules_version),
                ValidationRun.category_template_id.is_not_distinct_from(template_id),
                ValidationRun.template_version.is_not_distinct_from(template_version),
                ValidationRun.facts_digest == facts_digest,
            )
            .order_by(ValidationRun.created_at.desc(), ValidationRun.id.desc())
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def for_version(self, version_id: uuid.UUID) -> list[ValidationRun]:
        stmt = (
            self._select()
            .where(ValidationRun.listing_version_id == version_id)
            .order_by(ValidationRun.created_at, ValidationRun.attempt, ValidationRun.id)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def summary_for(
        self, version_id: uuid.UUID, *, request_id: str | None = None
    ) -> ValidationSummary | None:
        rows = await self.for_version(version_id)
        if not rows:
            return None
        return summary_of(rows, request_id=request_id)


def summary_of(rows: list[ValidationRun], *, request_id: str | None = None) -> ValidationSummary:
    """A summary over `rows` (oldest first); the newest row decides `passed` and the counts."""
    newest = rows[-1]
    stages: list[Any] = [_to_result(r) for r in rows]
    return ValidationSummary(
        listing_id=newest.listing_id,
        listing_version_id=newest.listing_version_id,
        passed=newest.blocking_count == 0,
        stages=stages[-20:],
        blocking_count=newest.blocking_count,
        warning_count=newest.warning_count,
        request_id=request_id,
    )
