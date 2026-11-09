"""The diagnosis of one unmapped marketplace code (SI13 T12; P:B2, D2-D3; CP-D1 to CP-D3).

A `brand_embed` job with `entity_type 'error_proposal'` (JobKind stays closed), enqueued by
`schedule_error_diagnosis` (`app/workers/periodic.py`). Its body:

1. One short tenant read: the sample issue (code, field, severity, message), its listing (SKUs,
   product, brand, barcode, every party value, pinned product type, and the version's title,
   bullets, description, search and hidden terms and attribute values) and the critical keys.
2. `diagnosis.build_input`: Amazon's text redacted into `data["message"]` only. The issue's
   submitted value is redacted only when it looks private (`submitted_secrets`) and is never a
   `check` secret: "true" or "Standard" is the value the draft has to name.
3. `budget.reserve` of `DIAGNOSIS_RESERVATION_USD` on the attempt. A refusal finishes the proposal
   `failed/budget` (tries kept, retried in 10 minutes) and ends the job with `{"refused": scope}`.
4. The seam call with no transaction open: the pipeline's `complete_json`, looked up lazily
   (`importlib`, as `image_edit_seam.py`), with the backend's fixed `SYSTEM`/`DEVELOPER` prompts,
   `response_model=DiagnosisOut`, one try, at most 400 output tokens, `agent_name correction`,
   `trigger correct`, `prompt_version error-diagnosis-1`, the generation credentials, and a
   `GenerationContext` built with `model_construct` carrying only the record ids (CP-D2). A
   pipeline without it finishes `failed/not_available` (one try counted) with no call.
5. One tenant transaction: every returned `AgentRunRecord` (invariant 7), the reservation settled
   to zero, then `diagnosis.check` and `finish`: `proposed`, `invalid` (no text) or
   `failed/model_failed` (one more try, retried after `tries` hours, never past 3).
The terminal hook (`app.services.job_hooks`) fails a still-`pending` proposal when the job dies.
Besides `app/workers/generation.py` and the two image seams, this is the only module that reaches
`mkos_pipeline` (`tests/unit/test_si7_seam_imports.py`).
"""

from __future__ import annotations

import importlib
import re
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.jobs import GenerationContext

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.resolved_facts import ProductFacts
from app.repositories.error_proposals import MAX_TRIES, ErrorProposalRepository
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.pipeline_deps import diagnosis_credentials
from app.services.resolution import diagnosis
from app.workers import budget
from app.workers.payloads import DiagnosisPayload
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)

PIPELINE_MODULE: Final = "mkos_pipeline"
ENTRY_POINT: Final = "complete_json"
DIAGNOSIS_RESERVATION_USD: Final = Decimal("0.01")
BUDGET_RETRY: Final = timedelta(minutes=10)
CALL_DEADLINE: Final = timedelta(seconds=60)
MAX_OUTPUT_TOKENS: Final = 400
FIXED_FIELDS: Final = (
    "listing",
    "facts.category",
    "facts.identifier",
    "facts.brand_name",
    "facts.gender",
    "facts.mrp",
    "facts.selling_price",
    "content.title",
    "content.description",
    "content.bullets",
    "keywords.search_terms",
)


def seam_entry() -> Any:
    """The pipeline's `complete_json`, or None when the installed pipeline has none."""
    module = importlib.import_module(PIPELINE_MODULE)
    entry = getattr(module, ENTRY_POINT, None)
    return entry if callable(entry) else None


async def call_complete_json(**kwargs: Any) -> Any:
    entry = seam_entry()
    if entry is None:
        return None
    return await entry(**kwargs)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def content_secrets(content_json: Any) -> list[tuple[str, str]]:
    """The version's own product text Amazon may quote back: the title, every bullet, the
    description, the search and hidden terms, and the string attribute values. Free text, so each
    needs `FREE_TEXT_MIN` characters: a short value ("Red", "Large") is an ordinary word."""
    if not isinstance(content_json, dict):
        return []
    found: list[str] = []
    content = content_json.get("content")
    if isinstance(content, dict):
        for key in ("title", "bullets", "description"):
            found += _strings(content.get(key))
    keywords = content_json.get("keywords")
    if isinstance(keywords, dict):
        for key in ("search_terms", "hidden_terms"):
            found += _strings(keywords.get(key))
    attributes = content_json.get("attributes")
    if isinstance(attributes, dict):
        for value in attributes.values():
            found += _strings(value)
    return [(v, "{value}") for v in found if len(v.strip()) >= diagnosis.FREE_TEXT_MIN]


_PARTY_ROLES: Final = ("manufacturer", "marketer", "packer", "importer", "consumer_care")


def party_values(facts: ProductFacts | None) -> list[tuple[str, str]]:
    """Every party string of the product (manufacturer, marketer, packer, importer, consumer
    care): name, email, phone and GSTIN (exact tokens), the full address and each run of two or
    more address parts. A single part ("India", "Surat") is an ordinary word, never a secret."""
    if facts is None:
        return []
    found: set[str] = set()
    for role in _PARTY_ROLES:
        party = getattr(facts.compliance, role)
        if party is None:
            continue
        for value in (party.legal_name, party.email, party.phone, party.gstin):
            if value and value.strip():
                found.add(value.strip())
        address = (party.address or "").strip()
        if address:
            parts = [p for p in re.split(r"\s*[,\n]\s*", address) if p]
            found.add(address)
            for i in range(len(parts)):
                for j in range(i + 2, len(parts) + 1):
                    found.add(", ".join(parts[i:j]))
    return [(v, "{value}") for v in sorted(found) if len(v) >= diagnosis.SECRET_MIN]


def submitted_secrets(value: str | None, *, exact: Iterable[str]) -> list[tuple[str, str]]:
    """Amazon's submitted value as a redaction pair, only when it looks private: it is one of the
    `exact` tokens (SKU, brand, barcode, party values; any case), or it has `FREE_TEXT_MIN`
    characters and a space, digit, `@` or `+`. A boolean, enum or unit value ("true", "Standard",
    "Count") stays: the model must name the bad value to explain the fix. Never a `check` secret."""
    shown = (value or "").strip()
    if not shown:
        return []
    folded = diagnosis.fold(shown).casefold()
    if any(diagnosis.fold(e).casefold() == folded for e in exact if e):
        return [(shown, "{value}")]
    if len(folded) >= diagnosis.FREE_TEXT_MIN and any(
        ch.isspace() or ch.isdigit() or ch in "@+" for ch in folded
    ):
        return [(shown, "{value}")]
    return []


async def _read(uow: TenantUnitOfWork, sample_id: uuid.UUID) -> dict[str, Any] | None:
    issue = await uow.listing_issues.get(sample_id)
    if issue is None:
        return None
    listing = await uow.listings.get(issue.listing_id)
    secrets: list[tuple[str, str]] = []
    exact: list[str] = []
    fields: set[str] = set(FIXED_FIELDS)
    product_type = None
    product_id = None
    if listing is not None:
        product_id = listing.product_id
        for row in await uow.listing_items.for_listing(listing.id):
            secrets.append((row.seller_sku, "{sku}"))
        if listing.marketplace_sku:
            secrets.append((listing.marketplace_sku, "{sku}"))
        product = await uow.products.get_by_id(listing.product_id)
        secrets += party_values(await uow.product_facts.resolve(listing.product_id))
        if product is not None:
            secrets.append((product.sku, "{sku}"))
            if product.brand_name:
                secrets.append((product.brand_name, "{brand}"))
            if getattr(product, "identifier_value", None):
                secrets.append((str(product.identifier_value), "{value}"))
        exact = [v for v, _ in secrets]  # the exact tokens, before the free text
        version_id = listing.current_version_id
        version = (
            None if version_id is None else await uow.listing_versions.get(listing.id, version_id)
        )
        if version is not None:
            secrets += content_secrets(version.content_json)
            attributes = (version.content_json or {}).get("attributes") or {}
            if isinstance(attributes, dict):
                fields.update(f"attributes.{key}" for key in attributes)
            if version.category_template_id is not None:
                pinned = await uow.templates.pinned(version.category_template_id)
                product_type = None if pinned is None else pinned.product_type
    return {
        "issue": issue,
        "listing_id": None if listing is None else listing.id,
        "product_id": product_id,
        "product_type": product_type,
        "secrets": secrets,
        "redact": secrets + submitted_secrets(issue.value_submitted, exact=exact),
        "fields": sorted(fields),
        "critical": await uow.product_facts.critical_keys(),
    }


async def _finish(
    ctx: JobContext,
    proposal_id: uuid.UUID,
    *,
    status: str,
    records: Sequence[AgentRunRecord] = (),
    fields: dict[str, Any] | None = None,
    reason: str | None = None,
    tries: int | None = None,
    retry_in: timedelta | None = None,
    settle: bool = False,
) -> None:
    async with tenant_uow(ctx.tenant_id) as uow:
        for record in records:
            await uow.agent_runs.insert(record)
        if settle:
            await uow.job_runs.reserve_cost(ctx.job_run_id, ctx.attempt, Decimal(0))
        await ErrorProposalRepository(uow.session).finish(
            proposal_id,
            status=status,
            fields=fields,
            failure_reason=reason,
            tries=tries,
            next_try_at=None if retry_in is None else datetime.now(UTC) + retry_in,
        )


async def run_error_diagnosis(ctx: JobContext) -> Outcome:
    payload = DiagnosisPayload.model_validate(ctx.payload)
    pid = payload.proposal_id
    async with tenant_uow(ctx.tenant_id) as uow:
        proposal = await ErrorProposalRepository(uow.session).get(pid)
        if proposal is None or proposal.status != "pending":
            return Outcome("succeeded", result={"skipped": "not_pending"})
        tries = int(proposal.tries)
        code = str(proposal.raw_value)
        found = await _read(uow, payload.sample_issue_id)
    if found is None:
        await _finish(
            ctx,
            pid,
            status="failed",
            reason="model_failed",
            tries=tries + 1,
            retry_in=timedelta(hours=tries + 1),
        )
        return Outcome("succeeded", result={"diagnosis": "no_sample"})
    issue = found["issue"]
    data = diagnosis.build_input(
        code,
        issue.field_path,
        found["product_type"],
        issue.severity,
        issue.message or "",
        found["redact"],
        fields=found["fields"],
    )
    if seam_entry() is None:
        await _finish(
            ctx,
            pid,
            status="failed",
            reason="not_available",
            tries=tries + 1,
            retry_in=timedelta(hours=tries + 1),
        )
        return Outcome("succeeded", result={"diagnosis": "not_available"})
    try:
        reserved = await budget.reserve(
            ctx.tenant_id, ctx.job_run_id, ctx.attempt, amount=DIAGNOSIS_RESERVATION_USD
        )
    except budget.BudgetRefused as refused:
        await _finish(
            ctx, pid, status="failed", reason="budget", tries=tries, retry_in=BUDGET_RETRY
        )
        return Outcome("succeeded", result={"refused": refused.scope})
    if not reserved:
        return Outcome("succeeded", result={"skipped": "fence"})
    settings = get_settings()
    credentials = diagnosis_credentials(settings)
    ids: dict[str, Any] = {
        "tenant_id": ctx.tenant_id,
        "request_id": ctx.request_id,
        "product_id": found["product_id"],
        "listing_id": found["listing_id"],
        "job_id": ctx.job_run_id,
    }
    record_ctx = GenerationContext.model_construct(**ids)  # the record ids only (CP-D2)
    records: list[AgentRunRecord] = []
    try:
        out = await call_complete_json(
            credentials=credentials,
            purpose="generation",
            prompt_version=diagnosis.PROMPT_VERSION,
            system=diagnosis.SYSTEM,
            developer=diagnosis.DEVELOPER,
            data=data,
            response_model=diagnosis.DiagnosisOut,
            max_tries=1,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            deadline_at=datetime.now(UTC) + CALL_DEADLINE,
            agent_name="correction",
            trigger="correct",
            attempt=ctx.attempt,
            records=records,
            ctx=record_ctx,
        )
    except Exception as exc:
        log.info("diagnosis.model_failed", job_run_id=str(ctx.job_run_id), error=type(exc).__name__)
        await _finish(
            ctx,
            pid,
            status="failed",
            records=records,
            reason="model_failed",
            tries=min(tries + 1, MAX_TRIES),
            retry_in=timedelta(hours=tries + 1),
            settle=True,
        )
        return Outcome("succeeded", result={"diagnosis": "model_failed"})
    model_used = records[-1].model_used if records else credentials.model
    meta = {
        "prompt_version": diagnosis.PROMPT_VERSION,
        "model_used": (model_used or "")[:128] or None,
        "agent_run_id": records[-1].id if records else None,
    }
    if not isinstance(out, diagnosis.DiagnosisOut):
        verdict: diagnosis.Proposed | diagnosis.Invalid = diagnosis.Invalid("shape")
    else:
        verdict = diagnosis.check(
            out,
            field_index=frozenset(found["fields"]),
            critical_keys=found["critical"],
            secrets=diagnosis.secret_values(found["secrets"]),
        )
    if isinstance(verdict, diagnosis.Proposed):
        await _finish(
            ctx,
            pid,
            status="proposed",
            records=records,
            fields={**verdict.columns(), **meta},
            settle=True,
        )
        outcome = "proposed"
    else:
        await _finish(ctx, pid, status="invalid", records=records, fields=meta, settle=True)
        outcome = "invalid"
    log.info("diagnosis.finished", job_run_id=str(ctx.job_run_id), outcome=outcome)
    return Outcome("succeeded", result={"diagnosis": outcome})


async def on_proposal_terminal(session: Any, job: Any) -> None:
    """A diagnosis job that died (failed, dead letter, cancelled) leaves no pending proposal."""
    if job.status in ("failed", "dead_letter", "cancelled") and job.entity_id is not None:
        await ErrorProposalRepository(session).fail_pending(job.entity_id)
