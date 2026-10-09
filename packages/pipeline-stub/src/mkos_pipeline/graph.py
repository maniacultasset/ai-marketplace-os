"""The stub graph: `run_listing_pipeline` from fixtures, deterministically (ADR-0012 §3; T104).

No model, prompt, credential value, clock, randomness or I/O beyond the packaged fixture. Every
port of `PipelineDeps` is called at least once, in the canonical stage order, so the backend's
implementation of each port is exercised end to end. Port exceptions propagate unchanged.

Scenarios, chosen by a `user_instructions` prefix `stub:<name>` (anything else is `ok`):

- `ok`: a compliant listing. A rules pass → `generated`; still failing at the cap → `blocked`.
- `blocked`: a 221-character title on every attempt → `blocked` after `ctx.max_attempts`.
- `failed`: no listing, `GENERATION_FAILED`, every agent block `failed`.
- `vision_warn`: the `ok` listing carrying `ctx.previous_version.images`, with one vision warning
  report per image (`pass=true`, one failed `text_overlay` vision check). It never changes the
  terminal state.
- `warnings`: the `ok` listing with the word `stubwarn` in its title (S19). The stub decides
  nothing: a tenant rule that flags the word as a warning (the local `demo` seed has one for
  acme) makes it `needs_review` with one open warning issue; without such a rule it is `generated`.

Ids are `uuid5(ctx.job_id, "<agent>:<attempt>")` and every timestamp is `STUB_EPOCH`, so two runs
over the same context return byte-identical results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from typing import Any, Final, Literal, cast
from uuid import UUID, uuid5

from mkos_schemas.agent import AgentName, AgentRunRecord, Trigger
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode, ImageIssueCode
from mkos_schemas.enums import ErrorSource, PipelineStage, RetryClass, Severity
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.images import ImageCheck, ImageIssue, ImageReport
from mkos_schemas.jobs import (
    AgentBlockStatus,
    CategoryTemplate,
    GenerationContext,
    GenerationResult,
)
from mkos_schemas.listing import AttributeValue, CanonicalListing, ImageRef
from mkos_schemas.pipeline import PipelineDeps
from mkos_schemas.rules import RequiredAttribute
from mkos_schemas.validation import ValidationResult, ValidationSummary

Scenario = Literal["ok", "blocked", "failed", "vision_warn", "warnings"]
SCENARIOS: Final[frozenset[str]] = frozenset({"ok", "blocked", "failed", "vision_warn", "warnings"})
WARN_WORD: Final = "stubwarn"
STUB_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)
STUB_MODEL: Final = "stub"
LONG_TITLE_LENGTH: Final = 221
BRAND_K: Final = 8
AGENTS: Final[tuple[Literal["content", "keyword", "attribute", "image"], ...]] = (
    "content",
    "keyword",
    "attribute",
    "image",
)
_PREFIX: Final = "stub:"
S = PipelineStage


@dataclass(frozen=True)
class _Fixture:
    title: str
    long_title_unit: str
    bullets: tuple[str, ...]
    description: str
    brand_query: str


def _fixture() -> _Fixture:
    raw = json.loads(
        files("mkos_pipeline").joinpath("fixtures/listing.json").read_text(encoding="utf-8")
    )
    return _Fixture(
        title=raw["title"],
        long_title_unit=raw["long_title_unit"],
        bullets=tuple(raw["bullets"]),
        description=raw["description"],
        brand_query=raw["brand_query"],
    )


def scenario(ctx: GenerationContext) -> Scenario:
    """The `stub:<name>` prefix of `user_instructions`; `ok` by default and for unknown names."""
    text = (ctx.user_instructions or "").strip()
    if not text.startswith(_PREFIX):
        return "ok"
    name = text[len(_PREFIX) :].split(maxsplit=1)[0] if text[len(_PREFIX) :].strip() else ""
    return cast(Scenario, name) if name in SCENARIOS else "ok"


def stub_id(ctx: GenerationContext, agent: str, attempt: int) -> UUID:
    return uuid5(ctx.job_id, f"{agent}:{attempt}")


def _value_for(template: CategoryTemplate, key: str) -> AttributeValue:
    spec = template.specs.get(key)
    if spec is None:
        return "stub"
    if spec.value_type in ("text", "enum"):
        return spec.allowed_values[0] if spec.allowed_values else "stub"
    if spec.value_type == "number":
        return 1
    if spec.value_type == "boolean":
        return True
    return f"1 {spec.unit}" if spec.unit else "1"  # measurement, as one wire scalar


def _required_attributes(ctx: GenerationContext) -> dict[str, AttributeValue]:
    """The template's required keys and the RuleSet's enabled `required_attribute` rules, so the
    `ok` listing satisfies both (an `allowed_values` list gives its first value)."""
    template = ctx.category_template
    out: dict[str, AttributeValue] = {}
    keys = set(template.required_attributes)
    keys |= {k for k, s in template.specs.items() if s.requirement == "required"}
    for key in sorted(keys):
        out[key] = _value_for(template, key)
    for rule in ctx.marketplace_rules.rules:
        rule_def = rule.rule_def
        if (
            rule.enabled
            and isinstance(rule_def, RequiredAttribute)
            and rule_def.attribute not in out
        ):
            allowed = rule_def.allowed_values
            out[rule_def.attribute] = (
                allowed[0] if allowed else _value_for(template, rule_def.attribute)
            )
    return dict(sorted(out.items()))


def build_listing(
    ctx: GenerationContext,
    *,
    long_title: bool,
    images: list[ImageRef] | None = None,
    warn: bool = False,
) -> CanonicalListing:
    fixture = _fixture()
    title = f"{fixture.title} {WARN_WORD}" if warn else fixture.title
    if long_title:
        repeated = fixture.long_title_unit * (LONG_TITLE_LENGTH // len(fixture.long_title_unit) + 1)
        title = repeated[: LONG_TITLE_LENGTH - 1] + "."
    return CanonicalListing.model_validate(
        {
            "listing_id": ctx.listing_id,
            "product_id": ctx.product_id,
            "marketplace": ctx.marketplace,
            "category": ctx.category,
            "locale": ctx.locale,
            "content": {
                "title": title,
                "bullets": list(fixture.bullets),
                "description": fixture.description,
            },
            "attributes": _required_attributes(ctx),
            "images": [i.model_dump(mode="json") for i in images or []],
            "generated_by_agent_run_id": stub_id(ctx, "content", 1),
        }
    )


def _record(
    ctx: GenerationContext,
    agent: AgentName,
    trigger: Trigger,
    attempt: int,
    *,
    failed: ErrorReport | None = None,
) -> AgentRunRecord:
    return AgentRunRecord(
        id=stub_id(ctx, agent, attempt),
        tenant_id=ctx.tenant_id,
        product_id=ctx.product_id,
        listing_id=ctx.listing_id,
        request_id=ctx.request_id,
        job_id=ctx.job_id,
        agent_name=agent,
        trigger=trigger,
        attempt=attempt,
        model_used=STUB_MODEL,
        cost_usd=Decimal("0"),
        duration_ms=0,
        status="failed" if failed is not None else "completed",
        error_report=failed,
        started_at=STUB_EPOCH,
        created_at=STUB_EPOCH,
    )


def _agent_records(
    ctx: GenerationContext, *, failed: ErrorReport | None = None
) -> list[AgentRunRecord]:
    triggers: dict[str, Trigger] = {"image": "vision"}
    return [
        _record(ctx, agent, triggers.get(agent, "generate"), 1, failed=failed) for agent in AGENTS
    ]


def _blocks(
    records: list[AgentRunRecord], *, error: ErrorReport | None = None
) -> list[AgentBlockStatus]:
    by_agent = {r.agent_name: r for r in records}
    return [
        AgentBlockStatus(
            agent=agent,
            status="failed" if error is not None else "ok",
            agent_run_id=by_agent[agent].id,
            error=error,
        )
        for agent in AGENTS
    ]


def _generation_failed(ctx: GenerationContext) -> ErrorReport:
    meta = ERROR_CODE_META[ErrorCode.GENERATION_FAILED]
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.GENERATION_FAILED,
        retryable=meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT),
        attempts_used=1,
        max_attempts=ctx.max_attempts,
        errors=[
            ErrorItem(
                field="listing",
                error_code=ErrorCode.GENERATION_FAILED,
                severity=Severity.ERROR,
                message="The stub pipeline's failed scenario produced no listing.",
                source=ErrorSource.AI_MODEL,
                retryable=meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT),
                retry_class=meta.retry_class,
            )
        ],
        request_id=ctx.request_id,
        listing_id=ctx.listing_id,
    )


def _vision_reports(
    ctx: GenerationContext, listing: CanonicalListing, image_run: UUID
) -> list[ImageReport]:
    return [
        ImageReport.model_validate(
            {
                "image_id": ref.image_id,
                "marketplace": ctx.marketplace,
                "slot": ref.slot,
                "pass": True,
                "checks": [ImageCheck(check="text_overlay", method="vision", passed=False)],
                "issues": [
                    ImageIssue(
                        code=ImageIssueCode.TEXT_DETECTED,
                        check="text_overlay",
                        suggestion="stub vision warning",
                    )
                ],
                "model_used": STUB_MODEL,
                "agent_run_id": image_run,
                "processed_at": STUB_EPOCH,
            }
        )
        for ref in listing.images
    ]


def _stage(ctx: GenerationContext, report: ErrorReport, attempt: int) -> ValidationResult:
    return ValidationResult(
        tenant_id=ctx.tenant_id,
        listing_id=ctx.listing_id,
        marketplace=ctx.marketplace,
        source=ErrorSource.RULES_ENGINE,
        passed=report.passed,
        attempt=attempt,
        rules_version=ctx.marketplace_rules.rules_version,
        duration_ms=0,
        report=report,
        created_at=STUB_EPOCH,
    )


async def _step(deps: PipelineDeps, node: PipelineStage) -> None:
    await deps.emit_node_event(node, "started")
    await deps.emit_node_event(node, "ok")


async def run_listing_pipeline(ctx: GenerationContext, deps: PipelineDeps) -> GenerationResult:
    """The stub graph; see the module docstring."""
    chosen = scenario(ctx)
    fixture = _fixture()
    await deps.emit_node_event(S.LOAD_CONTEXT, "started")
    await deps.get_model_credentials("generation")
    await deps.emit_node_event(S.LOAD_CONTEXT, "ok")
    await deps.emit_node_event(S.RETRIEVE_BRAND, "started")
    await deps.retrieve_brand_chunks(ctx.tenant_id, fixture.brand_query, k=BRAND_K)
    await deps.emit_node_event(S.RETRIEVE_BRAND, "ok")

    if chosen == "failed":
        await deps.emit_node_event(S.CONTENT_AGENT, "started")
        await deps.emit_node_event(S.CONTENT_AGENT, "failed", {"scenario": "failed"})
        failure = _generation_failed(ctx)
        failed_runs = _agent_records(ctx, failed=failure)
        return GenerationResult(
            request_id=ctx.request_id,
            job_id=ctx.job_id,
            listing=None,
            agent_runs=failed_runs,
            blocks=_blocks(failed_runs, error=failure),
            attempts_used=1,
            terminal_state="failed",
            error=failure,
        )

    for node in (S.CONTENT_AGENT, S.KEYWORD_AGENT, S.ATTRIBUTE_AGENT, S.IMAGE_AGENT):
        await _step(deps, node)
    await _step(deps, S.MERGE_OUTPUTS)
    images: list[ImageRef] = []
    if chosen == "vision_warn" and ctx.previous_version is not None:
        images = list(ctx.previous_version.images)
    records = _agent_records(ctx)
    stages: list[ValidationResult] = []
    report: ErrorReport | None = None
    listing: CanonicalListing | None = None
    attempt = 0
    while attempt < ctx.max_attempts:
        attempt += 1
        if attempt > 1:
            await _step(deps, S.CORRECT)
            records.append(_record(ctx, "correction", "correct", attempt))
        await deps.emit_node_event(S.VALIDATE_SCHEMA, "started")
        built = build_listing(
            ctx, long_title=chosen == "blocked", images=images, warn=chosen == "warnings"
        )
        listing = CanonicalListing.model_validate(built.model_dump(mode="json"))
        await deps.emit_node_event(S.VALIDATE_SCHEMA, "ok")
        await deps.emit_node_event(S.VALIDATE_RULES, "started")
        report = await deps.validate_rules(listing)
        await deps.emit_node_event(S.VALIDATE_RULES, "ok" if report.passed else "failed")
        await deps.emit_node_event(S.VALIDATE_VISION, "started")
        await deps.validate_images(listing)
        await deps.emit_node_event(S.VALIDATE_VISION, "ok")
        stages.append(_stage(ctx, report, attempt))
        if report.passed:
            break
    if report is None or listing is None:  # unreachable: the contract holds max_attempts >= 1
        raise RuntimeError("max_attempts must be at least 1")
    await deps.emit_node_event(S.QUALITY_CHECK, "started")

    image_reports: list[ImageReport] = []
    if chosen == "vision_warn":
        image_reports = _vision_reports(ctx, listing, stub_id(ctx, "image", 1))
    passed = report.passed
    error: ErrorReport | None = None
    if not passed:
        error = report.model_copy(
            update={"attempts_used": attempt, "max_attempts": ctx.max_attempts}
        )
    summary = ValidationSummary(
        listing_id=ctx.listing_id,
        passed=passed,
        stages=stages,
        blocking_count=len(report.blocking),
        warning_count=len(report.errors) - len(report.blocking),
        request_id=ctx.request_id,
    )
    payload: dict[str, Any] = {
        "request_id": ctx.request_id,
        "job_id": ctx.job_id,
        "listing": listing,
        "image_reports": image_reports,
        "validation": summary,
        "agent_runs": records,
        "blocks": _blocks(records),
        "attempts_used": attempt,
        "terminal_state": "generated" if passed else "blocked",
        "error": error,
    }
    return GenerationResult.model_validate(payload)
