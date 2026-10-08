"""The generation job's writes, inside its one fenced terminal transaction (S18 T15; D7; CP2, CP10;
CANON.md:113: the backend is the only writer of listing versions, `validation_runs` and
`agent_runs`).

`make_persist(plan)` returns the `persist(uow)` the wrapper runs after the fence lock:

1. the listing `FOR UPDATE`; it must still be `generating` and owned by this job (the newest
   `listing_generate` job of the listing), else nothing is written;
2. a saved version (`plan.move.saves_version`): `origin` `regeneration` when a
   `regenerate_requested` row names the job, else `generation`; `parent_version_id` the old
   current version; `generated_by_user_id` the human who triggered the job; the job id,
   `attempts_used` (CP2: the port calls the deps saw), the rules and template pins, the locale
   and the pipeline's image reports as given; the listing's `version` and `status` are stamped
   (CP3) and it becomes current;
3. one `rules_engine` run per recorded `validate_rules` call (attempt k, version NULL) and, with a
   version, the backend's re-check (attempt n+1, linked to it), each with the pins and the
   `facts_digest`;
4. one `agent_runs` row per record (`ON CONFLICT (id) DO NOTHING`), linked to the version when
   one was saved;
5. the move, with the job as actor.

Every insert is idempotent or fenced, so a replayed terminal write writes nothing twice.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.enums import ErrorSource, ListingStatus, Marketplace
from mkos_schemas.errors import ErrorReport
from mkos_schemas.images import ImageReport
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.validation import ValidationResult

from app.core.logging import get_logger
from app.repositories.listings import JobActor, NewVersion
from app.repositories.uow import TenantUnitOfWork
from app.services.generation_result import Move
from app.services.pipeline_deps import RulesCall

log = get_logger(__name__)
GENERATING: Final = "generating"


@dataclass(frozen=True, slots=True)
class Pins:
    rules_version: int
    template_id: uuid.UUID | None
    template_version: int | None
    facts_digest: bytes


@dataclass(frozen=True)
class PersistPlan:
    tenant_id: uuid.UUID
    job_id: uuid.UUID
    listing_id: uuid.UUID
    marketplace: Marketplace
    request_id: str
    created_by: uuid.UUID | None
    move: Move | None
    pins: Pins
    rules_calls: Sequence[RulesCall] = ()
    records: Sequence[AgentRunRecord] = ()
    listing: CanonicalListing | None = None
    image_reports: Sequence[ImageReport] = ()
    recheck: ErrorReport | None = None
    attempts_used: int = 0
    version_id: uuid.UUID = field(default_factory=uuid.uuid4)
    # the Outcome's result dict: a persist that finds the listing taken empties its version id
    result: dict[str, Any] | None = None


def _run(
    plan: PersistPlan,
    report: ErrorReport,
    attempt: int,
    version_id: uuid.UUID | None,
    duration_ms: int = 0,
) -> ValidationResult:
    return ValidationResult(
        tenant_id=plan.tenant_id,
        listing_id=plan.listing_id,
        listing_version_id=version_id,
        marketplace=plan.marketplace,
        source=ErrorSource.RULES_ENGINE,
        passed=report.passed,
        attempt=attempt,
        rules_version=plan.pins.rules_version,
        duration_ms=duration_ms,
        report=report,
        created_at=datetime.now(UTC),
    )


async def _insert_runs(
    uow: TenantUnitOfWork, plan: PersistPlan, version_id: uuid.UUID | None
) -> None:
    pins = plan.pins
    for call in plan.rules_calls:
        await uow.validation_runs.insert(
            _run(plan, call.report, call.attempt, None, call.duration_ms),
            template_id=pins.template_id,
            template_version=pins.template_version,
            facts_digest=pins.facts_digest,
            request_id=plan.request_id,
        )
    if version_id is not None and plan.recheck is not None:
        await uow.validation_runs.insert(
            _run(plan, plan.recheck, min(4, len(plan.rules_calls) + 1), version_id),
            template_id=pins.template_id,
            template_version=pins.template_version,
            facts_digest=pins.facts_digest,
            request_id=plan.request_id,
        )


async def _insert_records(
    uow: TenantUnitOfWork, plan: PersistPlan, version_id: uuid.UUID | None
) -> None:
    for record in plan.records:
        await uow.agent_runs.insert(record.model_copy(update={"listing_version_id": version_id}))


async def _owned(uow: TenantUnitOfWork, plan: PersistPlan) -> Any:
    listing = await uow.listings.get_for_update(plan.listing_id)
    if listing is None or listing.status != GENERATING:
        return None
    if await uow.listings.latest_generate_job_id(listing.id) != plan.job_id:
        return None
    return listing


def _discard(plan: PersistPlan) -> None:
    log.warning(
        "generation.persist_not_owner", job_id=str(plan.job_id), listing_id=str(plan.listing_id)
    )
    if plan.result is not None:
        plan.result.update({"listing_version_id": None, "discarded": True})


def make_persist(plan: PersistPlan) -> Any:
    """The `registry.Persist` of one generation job (module docstring)."""

    async def persist(uow: TenantUnitOfWork) -> None:
        listing = await _owned(uow, plan)
        saves = plan.move is not None and plan.move.saves_version and plan.listing is not None
        if listing is None:
            if saves:
                _discard(plan)
            return
        if not saves or plan.listing is None or plan.move is None:
            await _insert_runs(uow, plan, None)
            await _insert_records(uow, plan, None)
            return  # the job's terminal hook moves the listing to failed/generation_failed
        if plan.created_by is None:
            raise ValueError("a generation job needs the human who triggered it")
        number = await uow.listing_versions.next_version(listing.id)
        regeneration = await uow.review_decisions.has_regenerate_for_job(plan.job_id)
        content = plan.listing.model_copy(
            update={
                "version": number,
                "status": ListingStatus(plan.move.to),
                "listing_id": listing.id,
            }
        )
        version = await uow.listing_versions.insert(
            NewVersion(
                id=plan.version_id,
                listing_id=listing.id,
                version=number,
                content_json=content.model_dump(mode="json"),
                image_reports_json=[
                    r.model_dump(mode="json", by_alias=True) for r in plan.image_reports
                ],
                generated_by_user_id=plan.created_by,
                origin="regeneration" if regeneration else "generation",
                parent_version_id=listing.current_version_id,
                locale=content.locale,
                rules_version=plan.pins.rules_version,
                category_template_id=plan.pins.template_id,
                job_id=plan.job_id,
                attempts_used=plan.attempts_used,
            )
        )
        await _insert_runs(uow, plan, version.id)
        await _insert_records(uow, plan, version.id)
        await uow.listings.move(
            listing,
            to=plan.move.to,
            event=plan.move.event,
            reason=plan.move.reason,
            actor=JobActor(plan.job_id),
            current_version_id=version.id,
        )
        log.info(
            "generation.persisted",
            job_id=str(plan.job_id),
            listing_id=str(listing.id),
            version_id=str(version.id),
            status=plan.move.to,
        )

    return persist
