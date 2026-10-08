"""What the backend makes of a pipeline result (S18 T13, T14; D9; CP1, CP3; DATABASE.md:827).

Pure: no I/O.

`check_result(result, *, ctx, claim_started_at, now)` decides whether the result can be trusted:
- it is re-validated through JSON (`model_validate_json(model_dump_json())`), which defeats a
  `model_construct` that skipped validation; an unknown extra key is dropped (`WireModel`
  `extra="ignore"`, T100), never stored;
- `job_id`/`request_id` must be the job's; the listing's product, marketplace, category, locale and
  (when set) listing id must be the context's; at most 100 agent runs;
- each record takes its identity from the job (tenant, product, listing, job, request), and a
  `started_at` outside `[claim_started_at, now]` is clamped to `claim_started_at`, so a backdated
  record cannot move spend to another budget day;
- the listing gets the CP3 stamps it can take before the persist: `listing_id`, `created_at` and
  `generated_by_agent_run_id` (kept only when it names one of the records). `version` and `status`
  are stamped by the persist, under the listing lock.
An untrusted result is `Untrusted(report)`: `INTERNAL_ERROR`, field `listing`, `source=system`, the
message naming the check only (the single code for a non-validating result; never
`SCHEMA_INVALID`).

`recheck(listing, rules, product)` re-runs the Rules Engine on the result (invariant 2): the
pipeline's own verdict never validates a listing. `map_outcome(...)` then maps (DATABASE.md:827):
1. no listing, or terminal `failed` → `failed/generation_failed`;
2. any `error` item in the re-check → `blocked/corrections_exhausted`;
3. a warning, a failed block, or a terminal `needs_review`/`blocked` (CP1) → `needs_review`;
4. otherwise → `validated`. Never `approved` or `publishing` (invariant 1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import AgentBlockStatus, GenerationContext, GenerationResult
from mkos_schemas.listing import CanonicalListing
from pydantic import ValidationError

from app.services.rules_engine import evaluate
from app.services.rules_loader import LoadedRules

MAX_AGENT_RUNS: Final = 100


@dataclass(frozen=True, slots=True)
class Trusted:
    result: GenerationResult
    listing: CanonicalListing | None
    records: tuple[AgentRunRecord, ...]


@dataclass(frozen=True, slots=True)
class Untrusted:
    report: ErrorReport
    check: str


@dataclass(frozen=True, slots=True)
class Move:
    to: str
    event: str
    reason: str | None = None

    @property
    def saves_version(self) -> bool:
        return self.to != "failed"


FAILED = Move("failed", "generation_failed", "generation_failed")
BLOCKED = Move("blocked", "corrections_exhausted", "corrections_exhausted")
NEEDS_REVIEW = Move("needs_review", "generation_warnings")
VALIDATED = Move("validated", "generation_clean")


def untrusted_report(check: str, *, request_id: str | None = None) -> ErrorReport:
    meta = ERROR_CODE_META[ErrorCode.INTERNAL_ERROR]
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.INTERNAL_ERROR,
        retryable=False,
        errors=[
            ErrorItem(
                field="listing",
                error_code=ErrorCode.INTERNAL_ERROR,
                severity=Severity.ERROR,
                message=f"The pipeline result failed the {check} check.",
                source=ErrorSource.SYSTEM,
                retryable=False,
                retry_class=meta.retry_class,
            )
        ],
        request_id=request_id,
    )


def _untrusted(check: str, ctx: GenerationContext) -> Untrusted:
    return Untrusted(untrusted_report(check, request_id=ctx.request_id), check)


def stamp_record(
    record: AgentRunRecord,
    ctx: GenerationContext,
    *,
    claim_started_at: datetime,
    now: datetime,
) -> AgentRunRecord:
    started = record.started_at
    if not claim_started_at <= started <= now:
        started = claim_started_at
    return record.model_copy(
        update={
            "tenant_id": ctx.tenant_id,
            "product_id": ctx.product_id,
            "listing_id": ctx.listing_id,
            "listing_version_id": None,
            "job_id": ctx.job_id,
            "request_id": ctx.request_id,
            "started_at": started,
        }
    )


def salvage_records(
    result: object,
    ctx: GenerationContext,
    *,
    claim_started_at: datetime,
    now: datetime,
) -> list[AgentRunRecord] | None:
    """CP10: an untrusted result's records, when each validates as an `AgentRunRecord` and there
    are at most 100 of them (identity stamped from the job), else None (nothing is inserted and
    the reservation stays counted)."""
    raw = getattr(result, "agent_runs", None)
    if not isinstance(raw, list | tuple) or len(raw) > MAX_AGENT_RUNS:
        return None
    out: list[AgentRunRecord] = []
    for item in raw:
        try:
            body = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            record = AgentRunRecord.model_validate(body)
        except (ValidationError, TypeError, ValueError):
            return None
        out.append(stamp_record(record, ctx, claim_started_at=claim_started_at, now=now))
    return out


def check_result(
    result: object,
    *,
    ctx: GenerationContext,
    claim_started_at: datetime,
    now: datetime,
) -> Trusted | Untrusted:
    try:
        dumped = result.model_dump_json()  # type: ignore[attr-defined]
        checked = GenerationResult.model_validate_json(dumped)
    except (ValidationError, AttributeError, TypeError, ValueError):
        return _untrusted("schema", ctx)
    if checked.job_id != ctx.job_id:
        return _untrusted("job_id", ctx)
    if checked.request_id != ctx.request_id:
        return _untrusted("request_id", ctx)
    if len(checked.agent_runs) > MAX_AGENT_RUNS:
        return _untrusted("agent_runs", ctx)
    listing = checked.listing
    if listing is not None:
        if listing.listing_id is not None and listing.listing_id != ctx.listing_id:
            return _untrusted("listing_id", ctx)
        if listing.product_id != ctx.product_id:
            return _untrusted("product", ctx)
        if listing.marketplace is not ctx.marketplace:
            return _untrusted("marketplace", ctx)
        if listing.category != ctx.category:
            return _untrusted("category", ctx)
        if listing.locale != ctx.locale:
            return _untrusted("locale", ctx)
    records = tuple(
        stamp_record(r, ctx, claim_started_at=claim_started_at, now=now) for r in checked.agent_runs
    )
    stamped: CanonicalListing | None = None
    if listing is not None:
        record_ids = {r.id for r in records}
        run_id = listing.generated_by_agent_run_id
        stamped = listing.model_copy(
            update={
                "listing_id": ctx.listing_id,
                "created_at": now,
                "generated_by_agent_run_id": run_id if run_id in record_ids else None,
            }
        )
    return Trusted(result=checked, listing=stamped, records=records)


# ---- T14: the re-check and the status mapping ----


def _internal(listing: CanonicalListing) -> ErrorReport:
    report = untrusted_report("rules")
    return report.model_copy(update={"listing_id": listing.listing_id})


def recheck(
    listing: CanonicalListing, rules: LoadedRules, product: Mapping[str, object] | None
) -> ErrorReport:
    """The backend's own Rules Engine run on the result. An engine refusal is a failed report
    (fail closed), never a pass."""
    try:
        return evaluate(listing, rules.ruleset, template=rules.template, product=product)
    except Exception:  # the engine raises for an out-of-scope listing or an unsafe regex
        return _internal(listing)


def map_outcome(
    terminal_state: str,
    listing: CanonicalListing | None,
    report: ErrorReport | None,
    blocks: Sequence[AgentBlockStatus] | Sequence[Any] = (),
) -> Move:
    if listing is None or terminal_state == "failed" or report is None:
        return FAILED
    if report.blocking:
        return BLOCKED
    failed_block = any(getattr(b, "status", None) == "failed" for b in blocks)
    if report.errors or failed_block or terminal_state in ("needs_review", "blocked"):
        return NEEDS_REVIEW
    return VALIDATED
