"""The resolution executor (SI11 T12-T14; ADR-0028 proposed; P:B4, D2, D10; CP-D2, CP-D3, CP-D7,
CP-D8, CP-D9, CP-D12, CP-D21, CP-D22). It runs in the terminal hook of the job that produced the
marketplace's answer (a dry run, a submission or a status read), after that job's own writes, in
a SAVEPOINT: any failure rolls back to it, logs `resolution.failed` with ids, and leaves the job's
own result committed.

`resolve(uow, listing, entry=..., job=...)`:
1. loads the listing's open blocking issues of its marketplace's adapter (the dry run's channel
   for `preview`, the live channel otherwise) and the planner's context;
2. asks the marketplace's pure repairer (`registry.repairer`) for at most one repair per issue,
   on the payload as built without the stored repairs;
3. plans (`resolution.plan`), writes the repairs (one try per issue key and rule), then enqueues
   the job that sends the repaired payload, only when every blocking step is `auto_repair` (or
   `retry`) and the cap allows it:
   - before approval (`validated`, `needs_review`, `blocked/validation_errors`): one re-check of
     the same version (`request_preview(trigger="resolution")`, a new key for the new repairs);
   - after a failed submission or a rejecting read (`failed`, the approval still current): one
     automatic resend (`failed -> publishing` on `auto_resubmit`, runs now, CP-D22), copying the
     previous publish job's requester (T41);
   - `published` (BUYABLE with ERROR issues, CP-D21): no resend; the repairs wait for the next
     submission of any version.
   Only once that job is enqueued are the repaired issues closed `auto_fixed` (CP-D3): a repair
   whose resend is not enqueued leaves its issue open with lane `auto_repair`.
4. The cap (invariant 9): `cycles_since_human` counts the listing's jobs marked
   `trigger = "resolution"` since the last human event. A 4th automatic job is never enqueued:
   from `validated`, `needs_review` or `failed` the listing moves to `blocked/corrections_exhausted`;
   from `blocked` it stays (CP-D9) and the read says `stopped`.

Content and AI fixes (a new version a non-maker approves), seller questions and marketplace or
staff tasks are the other lanes' writers (T15-T18).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from mkos_schemas.enums import JobKind
from mkos_schemas.errors import ErrorItem

from app.adapters.registry import repairer
from app.core.errors import DomainError
from app.core.logging import get_logger
from app.db.models.ops import JobRun
from app.repositories.jobs import EnqueueSpec
from app.repositories.listings import JobActor
from app.repositories.repairs import NewRepair
from app.repositories.uow import TenantUnitOfWork
from app.services.adapter_input import build_adapter_listing
from app.services.issues import adapter_source
from app.services.remote_preview import request_preview
from app.services.resolution.plan import IssueView, PlanContext, Step, plan
from app.workers.payloads import PublishJobPayload, dump

log = get_logger(__name__)

Entry = Literal["preview", "submission", "read"]
MAX_CYCLES: Final = 3
PRE_APPROVAL: Final = frozenset({"validated", "needs_review"})
CAP_FROM: Final = frozenset({"validated", "needs_review", "failed"})
HELD_ON_MARKETPLACE: Final = ("accepted", "partially_accepted", "live")


class _Replay(Exception):  # noqa: N818 - rolls back the close when the resend already exists
    pass


@dataclass(frozen=True)
class ResolutionResult:
    steps: tuple[Step, ...] = ()
    repairs: tuple[uuid.UUID, ...] = ()
    job_id: uuid.UUID | None = None
    capped: bool = False
    notes: Mapping[str, str] = field(default_factory=dict)


def _view(row: Any, recurrence: Mapping[uuid.UUID, str]) -> IssueView:
    return IssueView(
        id=row.id,
        source=row.source,
        field_path=row.field_path,
        error_code=row.error_code,
        error_class=row.error_class,
        severity=row.severity,
        marketplace_code=row.marketplace_code,
        channel="preview" if row.dedupe_key.endswith("|preview") else "live",
        recurrence_resolution=(
            recurrence.get(row.recurrence_of_id) if row.recurrence_of_id is not None else None
        ),
    )


def _item(row: Any) -> ErrorItem:
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import ErrorSource, Severity

    try:
        code = ErrorCode(row.error_code)
    except ValueError:
        code = ErrorCode.MARKETPLACE_UNKNOWN_ERROR
    return ErrorItem(
        field=row.field_path,
        error_code=code,
        severity=Severity.ERROR,
        message=row.message,
        source=ErrorSource(row.source),
        retryable=False,
        raw_code=row.marketplace_code,
    )


async def _enums(
    uow: TenantUnitOfWork, source: Any, module: Any
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """The marketplace's enum and unit per attribute: the stored Product Type Definition when one
    is current, else the pinned template's allowed values (keyed by marketplace attribute)."""
    from app.services.product_type_schema import current_schemas

    enums: dict[str, tuple[str, ...]] = {}
    units: dict[str, tuple[str, ...]] = {}
    pinned = source.template
    if pinned is None:
        return enums, units
    for spec in pinned.template.specs.values():
        name = spec.marketplace_field
        if name and spec.allowed_values:
            enums[str(name)] = tuple(str(v) for v in spec.allowed_values)
    schema, _parent = await current_schemas(
        uow,
        marketplace=source.marketplace,
        product_type=pinned.product_type,
        with_parent=False,
    )
    if schema is not None and hasattr(module, "ptd_enum"):
        stored = await uow.ptd.schema_json(schema.definition_id)
        for name in list(enums):
            found = module.ptd_enum(stored, name)
            if found:
                enums[name] = found
    return enums, units


async def _on_marketplace(uow: TenantUnitOfWork, listing: Any) -> bool:
    """CP-D12: Amazon holds the SKU (an accepted, partial or live attempt ever; or an item a read
    found or a write accepted)."""
    if await uow.submissions.listing_has_outcome(listing.id, HELD_ON_MARKETPLACE):
        return True
    for row in await uow.listing_items.for_listing(listing.id):
        if row.marketplace_state != "not_submitted" or row.external_id is not None:
            return True
    return False


async def _candidates(
    uow: TenantUnitOfWork, listing: Any, rows: Sequence[Any], *, stored: bool
) -> dict[uuid.UUID, Any]:
    """issue id -> the repairer's `Repair` for it (pure rules on the payload as built)."""
    module = repairer(listing.marketplace)
    if module is None or not rows:
        return {}
    source = await uow.listings.preview_source(listing.id)
    family = await uow.product_facts.resolve_family(listing.product_id)
    if source is None or source.version is None or family is None or source.template is None:
        return {}
    item = build_adapter_listing(source, facts=family, image_refs={}, idempotency_key="resolve")
    built = module.units_as_built(item)
    enums, units = await _enums(uow, source, module)
    ctx = module.RepairContext(enums=enums, units=units, stored=stored)
    sku_by_item = {r.id: r.seller_sku for r in await uow.listing_items.for_listing(listing.id)}
    found: dict[uuid.UUID, Any] = {}
    for row in rows:
        names = module.attributes_for(item, row.field_path)
        units_for = [
            (unit, attributes)
            for unit, attributes in built
            if row.listing_item_id is None
            or sku_by_item.get(row.listing_item_id) == unit.seller_sku
        ]
        for unit, attributes in units_for:
            hit = next(
                (
                    r
                    for n in names
                    if (r := module.propose(item, unit, _item(row), n, attributes, ctx))
                ),
                None,
            )
            if hit is not None:
                found[row.id] = hit
                break
    return found


async def _cap(uow: TenantUnitOfWork, listing: Any, job: JobRun) -> bool:
    """CP-D9: the 4th automatic job is never enqueued; move to `corrections_exhausted` when an
    edge exists from here. Returns True (capped)."""
    if listing.status in CAP_FROM:
        await uow.listings.move(
            listing,
            to="blocked",
            event="corrections_exhausted",
            reason="corrections_exhausted",
            actor=JobActor(job.id),
            clear_approval=False,
        )
    log.info("resolution.capped", listing_id=str(listing.id), job_run_id=str(job.id))
    return True


async def _close_repaired(
    uow: TenantUnitOfWork, steps: Sequence[Step], repairs: Mapping[uuid.UUID, Any]
) -> None:
    for step in steps:
        if step.lane == "auto_repair" and step.issue_id in repairs:
            await uow.listing_issues.close_auto_fixed(
                step.issue_id, f"repair:{repairs[step.issue_id].rule}"
            )


async def _previous_publish(uow: TenantUnitOfWork, listing: Any) -> tuple[Any, Any] | None:
    """The version's last submission and the publish job that sent it (its requester)."""
    prev = await uow.submissions.last_for_version(listing.id, listing.approved_version_id)
    if prev is None:
        return None
    owner = await uow.job_runs.get_by_id(prev.job_id)
    if owner is None or owner.created_by is None:
        return None
    return prev, owner


async def _auto_resubmit(
    uow: TenantUnitOfWork,
    listing: Any,
    job: JobRun,
    steps: Sequence[Step] = (),
    repairs: Mapping[uuid.UUID, Any] | None = None,
) -> uuid.UUID | None:
    """T13 (T41): the automatic resend of the approved version (module docstring, step 3). The
    repaired issues close `auto_fixed` in the same savepoint as the enqueue (CP-D3: the publish
    gate refuses a resend while a user-action ERROR of the version is open); a refused resend
    rolls the close back with it."""
    if (
        listing.status != "failed"
        or listing.approved_version_id is None
        or listing.approved_version_id != listing.current_version_id
    ):
        return None
    version = listing.approved_version_id
    count = await uow.submissions.count_for_version(listing.id, version)
    if count >= MAX_CYCLES:
        return None
    previous = await _previous_publish(uow, listing)
    if previous is None:
        return None
    _prev, owner = previous
    mode = "retry_fields" if await _on_marketplace(uow, listing) else "publish"
    key = hashlib.sha256(f"auto_resubmit:{listing.id}:{version}:{count + 1}".encode()).hexdigest()
    spec = EnqueueSpec(
        kind=JobKind.LISTING_PUBLISH,
        entity_type="listing",
        entity_id=listing.id,
        listing_id=listing.id,
        created_by=owner.created_by,
        job_key=key,
        payload=dump(
            PublishJobPayload(
                listing_id=listing.id, listing_version_id=version, mode=mode, trigger="resolution"
            )
        ),
    )
    try:
        async with uow.session.begin_nested():
            await _close_repaired(uow, steps, repairs or {})
            enqueued = await uow.job_runs.enqueue(spec)
            if not enqueued.created:
                raise _Replay
            await uow.listings.move(
                listing, to="publishing", event="auto_resubmit", actor=JobActor(job.id)
            )
    except _Replay:
        return None
    except DomainError as exc:  # the publish gate or the status guard said no: a human step
        log.info(
            "resolution.resubmit_refused",
            listing_id=str(listing.id),
            error_code=getattr(getattr(exc, "code", None), "value", type(exc).__name__),
        )
        return None
    log.info(
        "resolution.auto_resubmit",
        listing_id=str(listing.id),
        job_run_id=str(enqueued.job.id),
        mode=mode,
        attempt=count + 1,
    )
    return enqueued.job.id


async def _audit(
    uow: TenantUnitOfWork,
    listing: Any,
    job: JobRun,
    repairs: Mapping[uuid.UUID, Any],
    ids: Mapping[uuid.UUID, uuid.UUID],
) -> None:
    for issue_id, repair in repairs.items():
        repair_id = ids.get(issue_id)
        if repair_id is None:
            continue
        await uow.audit.write(
            tenant_id=listing.tenant_id,
            actor_type="system",
            actor_user_id=None,
            action="listing_repair.created",
            entity_type="listing_repair",
            entity_id=repair_id,
            after=json.dumps({"rule": repair.rule, "attribute": repair.attribute}),
            job_id=job.id,
        )


async def _resolve(
    uow: TenantUnitOfWork, listing: Any, *, entry: Entry, job: JobRun
) -> ResolutionResult:
    channel: Literal["live", "preview"] = "preview" if entry == "preview" else "live"
    rows = await uow.listing_issues.open_errors(
        listing.id, [adapter_source(listing.marketplace)], channel=channel
    )
    if entry == "preview":
        rows = [r for r in rows if r.listing_version_id == listing.current_version_id]
    if not rows:
        return ResolutionResult()
    stored = await _on_marketplace(uow, listing)
    candidates = await _candidates(uow, listing, rows, stored=stored)
    recurrence = await uow.resolution.recurrence_resolutions(
        [r.recurrence_of_id for r in rows if r.recurrence_of_id is not None]
    )
    cycles = await uow.resolution.cycles_since_human(listing.id)
    ctx = PlanContext(
        critical_keys=await uow.product_facts.critical_keys(),
        repairable=frozenset(candidates),
        approved_current=listing.approved_version_id == listing.current_version_id,
        cycles_used=cycles,
    )
    steps = plan([_view(r, recurrence) for r in rows], ctx)
    by_issue = {r.id: r for r in rows}
    ids: dict[uuid.UUID, uuid.UUID] = {}
    for step in steps:
        if step.lane != "auto_repair":
            continue
        repair, row = candidates[step.issue_id], by_issue[step.issue_id]
        inserted = await uow.listing_repairs.insert(
            NewRepair(
                listing_id=listing.id,
                marketplace=listing.marketplace,
                listing_item_id=row.listing_item_id,
                field_path=repair.field_path,
                attribute=repair.attribute,
                rule=repair.rule,
                issue_key=row.dedupe_key,
                source_issue_id=row.id,
                job_id=job.id,
                source_digest=repair.source_digest,
                to_digest=repair.to_digest,
                value_json=None if repair.value is None else [dict(v) for v in repair.value],
                alias_version=repair.alias_version,
            )
        )
        if inserted is not None:
            ids[step.issue_id] = inserted
    written = {i: candidates[i] for i in ids}
    await _audit(uow, listing, job, written, ids)
    automatic = all(s.lane in ("auto_repair", "retry") for s in steps)
    if not steps or not automatic or not written:
        return ResolutionResult(steps=steps, repairs=tuple(ids.values()))
    if cycles >= MAX_CYCLES:
        await _cap(uow, listing, job)
        return ResolutionResult(steps=steps, repairs=tuple(ids.values()), capped=True)
    next_job: uuid.UUID | None = None
    if entry == "preview":
        fresh = await uow.listings.get(listing.id)
        next_job = await request_preview(
            uow, fresh, listing.current_version_id, trigger="resolution"
        )
    elif listing.status == "failed":
        next_job = await _auto_resubmit(uow, listing, job, steps, written)
        return ResolutionResult(steps=steps, repairs=tuple(ids.values()), job_id=next_job)
    if next_job is not None:
        await _close_repaired(uow, steps, written)
    return ResolutionResult(steps=steps, repairs=tuple(ids.values()), job_id=next_job)


async def resolve(
    uow: TenantUnitOfWork, listing: Any, *, entry: Entry, job: JobRun
) -> ResolutionResult | None:
    """The executor (module docstring), in its own SAVEPOINT."""
    if listing is None or listing.paused_at is not None or listing.archived_at is not None:
        return None
    try:
        async with uow.session.begin_nested():
            result = await _resolve(uow, listing, entry=entry, job=job)
    except Exception as exc:  # the savepoint rolled back; the job's own writes stay
        log.error(
            "resolution.failed",
            listing_id=str(listing.id),
            job_run_id=str(job.id),
            entry=entry,
            error_class=type(exc).__name__,
        )
        return None
    if result.steps:
        log.info(
            "resolution.planned",
            listing_id=str(listing.id),
            job_run_id=str(job.id),
            entry=entry,
            lanes=",".join(sorted({s.lane for s in result.steps})),
            repairs=len(result.repairs),
            next_job=str(result.job_id) if result.job_id else None,
            capped=result.capped,
        )
    return result
