"""The resolution executor (SI11 T12-T14; ADR-0028 proposed; P:B4, D2, D10; CP-D2, CP-D3, CP-D7,
CP-D8, CP-D9, CP-D12, CP-D21, CP-D22). It runs in the terminal hook of the job that produced the
marketplace's answer (a dry run, a submission or a status read), after that job's own writes, in
a SAVEPOINT: any failure rolls back to it, logs `resolution.failed` with ids, and leaves the job's
own result committed.

`resolve(uow, listing, entry=..., job=...)`:
1. loads the listing's open blocking issues of its marketplace's adapter (the dry run's channel
   for `preview`, the live channel otherwise) and the planner's context;
2. asks the marketplace's pure repairer (`registry.repairer`) for at most one repair per issue,
   on the payload as built without the stored repairs. The rules read the stored Product Type
   Definition's enum and unit of every attribute the payload carries (else the template's
   allowed values), the repairer's `repair_names` (the reverse index, the barcode alias, or the
   attribute a `listing` issue's text names) and, after a submission only, the version's
   approved images that passed their check and have a public copy (`image_slot`, CP-D23). An
   issue on an attribute whose live repair was written for another issue is the marketplace
   refusing the repaired value: no candidate, the planner's `repair_rejected` (staff);
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
   whose resend is not enqueued leaves its issue open with lane `auto_repair`. A planned repair
   the store refuses (a live repair of the attribute, or the key and rule tried once) is
   re-planned `staff` (`repair_rejected`), so no `auto_repair` step stands without a repair.
4. The cap (invariant 9): `cycles_since_human` counts the listing's jobs marked
   `trigger = "resolution"` since the last human event. A 4th automatic job is never enqueued:
   from `validated`, `needs_review` or `failed` the listing moves to `blocked/corrections_exhausted`;
   from `blocked` it stays (CP-D9) and the read says `stopped`.

5. The human lanes' writers (fix round 2), for every planned step, before any automatic job:
   - `seller` -> one Inbox question per fact or image (`open_questions` over those rows only,
     CP-D25); no status move (the open row already blocks, and the dry run stays reachable);
   - `marketplace` -> one `permission_decision` account task with the Seller Central step (T18;
     the same target as the write path's, so a write and a read share one task);
   - `staff` -> one `marketplace_error_review` account task per planner action (an unknown code
     shares the write path's `account.error_review`), the rows linked to it.
   Each row is linked to its task, so the inbox and the read name who acts next.

6. Content fixes (T15): a content or keyword refusal the deterministic content fix can change
   (`resolution.content.prepare` on the current version) is planned `auto_content`. When every
   blocking step is automatic and the cap allows it, one `listing_generate` job (`mode
   auto_fix`, `trigger resolution`, the base version's generator as `created_by`) is enqueued
   and the listing moves to `generating` on `auto_fix`; the job writes a fix version with only
   the fixed paths changed. That version is never resent on its own: a non-maker approves it
   ("Approve and resend"). Repairs planned beside it stay live and go out with that version.
7. AI rewrites (T16): with SI10's credit gate admitting (`PlanContext.ai_allowed`), a refusal
   the fix policy routes `ai_field` on a path the merge can keep alone takes the AI lane; with no
   `auto_content` step, the same job runs `mode ai_field` (`target_fields`, one feedback line per
   path): the model's result is merged so only those paths change, and the version waits for a
   non-maker the same way. Paused AI sends those refusals to a staff content review.
8. Client item 20 (0038): the brand's `tenants.auto_fix_enabled`. Off, the planner gives every
   fix lane to the seller (`approve_fix`): a repair candidate is written `held` (a suggested fix
   the adapter never applies, audited `listing_repair.suggested`), no fix job and no re-check or
   resend is enqueued, and the issue stays open in "Needs you". `apply_fixes(principal,
   listing_id)` (the seller's "Apply fixes", `POST /listings/{id}/fixes/apply`) runs this
   executor once with auto-fix allowed (`approved_by`): the held repairs are released (audited
   `listing_repair.approved`, the user as actor) and count as written, so the re-check, resend or
   fix job follows as it would have with the setting on. A plain retry stays automatic. With the
   setting turned back on, the next run releases the held repairs the same way (the system as
   actor) instead of refusing them as a second live repair.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from mkos_schemas.errors import ErrorItem

from app.adapters.amazon.precheck import link_in
from app.adapters.registry import repairer
from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.flags import PAUSED_REASON, publish_enabled
from app.core.logging import get_logger
from app.db.models.ops import JobRun
from app.repositories.jobs import EnqueueSpec
from app.repositories.listings import JobActor
from app.repositories.repairs import NewRepair
from app.repositories.uow import TenantUnitOfWork
from app.services.adapter_input import build_adapter_listing
from app.services.auto_fix.text import seller_central_step, staff_step
from app.services.issues import adapter_source
from app.services.remote_preview import request_preview
from app.services.resolution.plan import (
    APPROVE_FIX,
    SELLER_ACTIONS,
    IssueView,
    PlanContext,
    Step,
    plan,
    seller_answers,
    staff_action,
)
from app.workers.payloads import AutoFixPayload, PublishJobPayload, dump

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
    tasks: Mapping[str, Sequence[uuid.UUID]] = field(default_factory=dict)
    """lane -> the task ids the human lanes' writers opened or reused (step 5)."""


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


async def _ptd_extra(
    uow: TenantUnitOfWork, source: Any, module: Any, names: Sequence[str]
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """The stored Product Type Definition's enum and unit of every attribute the payload carries
    (`_enums` reads only the attributes the template gives allowed values)."""
    from app.services.product_type_schema import current_schemas

    enums: dict[str, tuple[str, ...]] = {}
    units: dict[str, tuple[str, ...]] = {}
    pinned = source.template
    if pinned is None or not names or not hasattr(module, "ptd_enum"):
        return enums, units
    schema, _parent = await current_schemas(
        uow, marketplace=source.marketplace, product_type=pinned.product_type, with_parent=False
    )
    if schema is None:
        return enums, units
    stored = await uow.ptd.schema_json(schema.definition_id)
    for name in sorted(set(names)):
        if found := module.ptd_enum(stored, name):
            enums[name] = found
        if hasattr(module, "ptd_units") and (unit := module.ptd_units(stored, name)):
            units[name] = unit
    return enums, units


async def _main_ok_images(
    uow: TenantUnitOfWork, listing: Any, image_ids: Sequence[uuid.UUID]
) -> tuple[dict[uuid.UUID, str], tuple[tuple[uuid.UUID, str], ...]]:
    """(the public locator of each version image that already has a public copy, the approved
    images among them whose stored check for this marketplace passed, in listing order). Only
    after a submission: a preview sends no image (CP-D23), and the copies exist only once a
    publish of the version wrote them."""
    from mkos_schemas.enums import Marketplace

    from app.services.public_images import public_url

    base = get_settings().image_public_base_url
    if base is None or not image_ids:
        return {}, ()
    reports = await uow.images.live_reports_for_images(list(image_ids))
    refs: dict[uuid.UUID, str] = {}
    passing: list[tuple[uuid.UUID, str]] = []
    for image_id in image_ids:
        image = await uow.images.get_live(image_id)
        if (
            image is None
            or image.public_id is None
            or image.public_written_at is None
            or image.clean_mime_type is None
        ):
            continue
        refs[image_id] = public_url(base, image.public_id, image.clean_mime_type)
        report = reports.get(image_id, {}).get(Marketplace(listing.marketplace))
        if report is not None and report.pass_:
            passing.append((image_id, refs[image_id]))
    return refs, tuple(passing)


@dataclass(frozen=True)
class _Found:
    candidates: Mapping[uuid.UUID, Any] = field(default_factory=dict)
    rejected: frozenset[uuid.UUID] = frozenset()
    """Issues on an attribute that already carries a live repair: the marketplace refused the
    repaired value (P:D10, one try: staff, never a second automatic guess)."""
    waiting: frozenset[uuid.UUID] = frozenset()
    """Issues whose own live repair is not sent yet (CP-D21: it goes out with the next
    submission); they stay `auto_repair`."""


def _live_key(item_id: uuid.UUID | None, attribute: str) -> tuple[uuid.UUID | None, str]:
    return item_id, attribute


async def _candidates(
    uow: TenantUnitOfWork,
    listing: Any,
    rows: Sequence[Any],
    *,
    stored: bool,
    entry: Entry = "preview",
    live: Sequence[Any] = (),
) -> _Found:
    """issue id -> the repairer's `Repair` for it (pure rules on the payload as built), and the
    issues whose attribute already has a live repair (left out of the candidates)."""
    module = repairer(listing.marketplace)
    if module is None or not rows:
        return _Found()
    source = await uow.listings.preview_source(listing.id)
    family = await uow.product_facts.resolve_family(listing.product_id)
    if source is None or source.version is None or family is None or source.template is None:
        return _Found()
    refs: dict[uuid.UUID, str] = {}
    main_ok: tuple[tuple[uuid.UUID, str], ...] = ()
    if entry != "preview":  # CP-D23: image repairs only after a submission
        plain = build_adapter_listing(source, facts=family, image_refs={}, idempotency_key="x")
        refs, main_ok = await _main_ok_images(
            uow, listing, [ref.image_id for ref in plain.listing.images]
        )
    item = build_adapter_listing(source, facts=family, image_refs=refs, idempotency_key="resolve")
    built = module.units_as_built(item)
    enums, units = await _enums(uow, source, module)
    sent_names = sorted({n for _unit, attributes in built for n in attributes})
    extra_enums, extra_units = await _ptd_extra(uow, source, module, sent_names)
    ctx = module.RepairContext(
        enums={**extra_enums, **enums},
        units={**extra_units, **units},
        stored=stored,
        main_ok_images=main_ok,
    )
    sku_by_item = {r.id: r.seller_sku for r in await uow.listing_items.for_listing(listing.id)}
    # (item, attribute) -> the issue each live repair was written for: the same open row is a
    # repair still waiting for its send (CP-D21), any other row is the marketplace's answer to it
    repaired = {_live_key(r.listing_item_id, r.attribute): r.source_issue_id for r in live}
    names_of = getattr(module, "repair_names", None)
    found: dict[uuid.UUID, Any] = {}
    rejected: set[uuid.UUID] = set()
    waiting: set[uuid.UUID] = set()
    for row in rows:
        units_for = [
            (unit, attributes)
            for unit, attributes in built
            if row.listing_item_id is None
            or sku_by_item.get(row.listing_item_id) == unit.seller_sku
        ]
        text = f"{row.raw_message or ''} {row.message or ''}"
        for unit, attributes in units_for:
            names = (
                names_of(item, row.field_path, attributes, text)
                if names_of is not None
                else module.attributes_for(item, row.field_path)
            )
            # a live repair of the attribute (for this item, or listing-wide) was sent and refused
            if any(
                key in repaired and repaired[key] != row.id
                for n in names
                for key in (_live_key(row.listing_item_id, n), _live_key(None, n))
            ):
                rejected.add(row.id)
                break
            if any(
                repaired.get(_live_key(i, n)) == row.id
                for n in names
                for i in (row.listing_item_id, None)
            ):
                waiting.add(row.id)
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
    return _Found(found, frozenset(rejected), frozenset(waiting))


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
    if not publish_enabled(listing.marketplace, get_settings()):
        # the publish kill switch: no automatic resend is enqueued (nothing piles up, no cycle
        # is used); the repaired issues stay open and go out with the next publish
        await uow.audit.write(
            tenant_id=listing.tenant_id,
            actor_type="system",
            actor_user_id=None,
            action="listing.publish_paused",
            entity_type="listing",
            entity_id=listing.id,
            reason=PAUSED_REASON,
            job_id=job.id,
            after=json.dumps({"trigger": "resolution"}),
        )
        log.warning(
            "publish.paused",
            listing_id=str(listing.id),
            job_run_id=str(job.id),
            marketplace=listing.marketplace,
            trigger="resolution",
        )
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


async def _content_fixable(
    uow: TenantUnitOfWork, listing: Any, rows: Sequence[Any]
) -> frozenset[uuid.UUID]:
    """T15: the rows the deterministic content fix can change on the current version."""
    from app.services.resolution.content import fixable, prepare

    candidates = [r for r in rows if fixable(r)]
    if not candidates or listing.current_version_id is None:
        return frozenset()
    version = await uow.listing_versions.get(listing.id, listing.current_version_id)
    found = await prepare(uow, listing, version, candidates)
    return frozenset() if found is None else frozenset(f.issue_id for f in found[1])


async def _ai_allowed(uow: TenantUnitOfWork, tenant_id: uuid.UUID) -> bool:
    """T16: the claim's own admission rule admits one generation now (SI10 D4: no credit
    pause or fresh low balance, the tenant's and the platform's daily caps; fix round 1), so a
    rewrite the claim would refuse is never planned: those refusals go to staff or the seller."""
    from datetime import UTC, datetime

    from app.workers.budget import admission_on

    return await admission_on(uow.session, tenant_id, datetime.now(UTC)) is None


def _ai_scope(
    listing: Any, steps: Sequence[Step], by_issue: Mapping[uuid.UUID, Any]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """T16: the `ai_field` steps' target paths (at most `MAX_TARGET_FIELDS`, the generation
    context's cap) and one feedback line per path (at most 5). A path cut here is not sent: its
    issue stays open for the next round (the fix version's dry run asks again)."""
    from mkos_schemas.jobs import MAX_TARGET_FIELDS

    from app.services.auto_fix.text import rewrite_feedback

    targets: dict[str, str] = {}
    for step in steps:
        if step.lane != "ai_field":
            continue
        row = by_issue[step.issue_id]
        targets.setdefault(
            row.field_path,
            rewrite_feedback(listing.marketplace, row.field_path, row.plain_message or row.message),
        )
    paths = tuple(sorted(targets))[:MAX_TARGET_FIELDS]
    return paths, tuple(targets[p] for p in paths)[:5]


async def _auto_fix(
    uow: TenantUnitOfWork,
    listing: Any,
    job: JobRun,
    steps: Sequence[Step],
    by_issue: Mapping[uuid.UUID, Any] | None = None,
) -> uuid.UUID | None:
    """T15/T16: enqueue the auto-fix job and move to `generating` (`auto_fix`; the job row
    first, as the status guard needs). `auto_content` steps make the deterministic job; with
    none, `ai_field` steps make the AI rewrite of those fields only. A replayed key or a refused
    move enqueues nothing."""
    lane = "auto_content" if any(s.lane == "auto_content" for s in steps) else "ai_field"
    mode = "auto_fix" if lane == "auto_content" else "ai_field"
    rows = by_issue or {}
    targets, feedback = ((), ()) if mode == "auto_fix" else _ai_scope(listing, steps, rows)
    # an `ai_field` issue is covered only when its path is one of the kept targets: one cut by
    # the cap stays open and is never closed as an AI fix of a field the model was not sent
    issue_ids = sorted(
        s.issue_id
        for s in steps
        if s.lane == lane and (mode == "auto_fix" or rows[s.issue_id].field_path in targets)
    )[:50]
    if not issue_ids or listing.current_version_id is None:
        return None
    version = await uow.listing_versions.get(listing.id, listing.current_version_id)
    if version is None:
        return None
    joined = ",".join(str(i) for i in issue_ids)
    key = hashlib.sha256(f"{mode}:{listing.id}:{version.id}:{joined}".encode()).hexdigest()
    payload = AutoFixPayload(
        listing_id=listing.id,
        mode=mode,
        base_version_id=version.id,
        issue_ids=tuple(issue_ids),
        target_fields=targets,
        feedback=feedback,
    )
    spec = EnqueueSpec(
        kind=JobKind.LISTING_GENERATE,
        entity_type="listing",
        entity_id=listing.id,
        listing_id=listing.id,
        created_by=version.generated_by_user_id,
        job_key=key,
        payload=payload.model_dump(mode="json"),  # `trigger` kept: the cap counts this job
    )
    try:
        async with uow.session.begin_nested():
            enqueued = await uow.job_runs.enqueue(spec)
            if not enqueued.created:
                raise _Replay
            await uow.listings.move(
                listing,
                to="generating",
                event="auto_fix",
                actor=JobActor(job.id),
                clear_approval=True,
            )
    except _Replay:
        return None
    except DomainError as exc:  # the status guard said no: the rows stay open for a human
        log.info(
            "resolution.auto_fix_refused",
            listing_id=str(listing.id),
            error_code=getattr(getattr(exc, "code", None), "value", type(exc).__name__),
        )
        return None
    log.info(
        "resolution.auto_fix",
        listing_id=str(listing.id),
        job_run_id=str(enqueued.job.id),
        issues=len(issue_ids),
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


STAFF_TARGETS: Final[Mapping[str, str]] = {"review": "account.error_review"}
PERMISSION_TARGET: Final = "account.category_approvals"


def staff_target(action: str) -> str:
    """The staff task's target for a planner action (`ck_human_tasks_target_path`)."""
    return STAFF_TARGETS.get(action, f"account.review.{action}")


async def _human_steps(
    uow: TenantUnitOfWork,
    listing: Any,
    steps: Sequence[Step],
    by_issue: Mapping[uuid.UUID, Any],
) -> dict[str, list[uuid.UUID]]:
    """Step 5 of the module docstring: the seller, marketplace and staff lanes' tasks. Returns
    lane -> the task ids opened or reused."""
    from app.services.issue_questions import open_questions

    opened: dict[str, list[uuid.UUID]] = {}
    # belt and braces (round 4): a seller step the inbox cannot take goes to staff before the
    # inbox is asked, and so does any seller row the inbox left with no question behind it (no
    # dictionary target, an image index outside the version): an open ERROR never stalls taskless.
    # SI13 (CP-D8) exempts two seller actions that open no question, `recategorize` and
    # `edit_identifier`: their issue row (lane seller, the field path and the templated message
    # naming the action) is the seller's signal over HTTP, the action is on the listing (the
    # recategorise route, the product's barcode edit), and the resolution read's next step names
    # it. They never reach the inbox and are never turned into staff.
    steps = [
        _to_staff(s, by_issue) if s.lane == "seller" and s.action not in SELLER_ACTIONS else s
        for s in steps
    ]
    seller = [
        by_issue[s.issue_id] for s in steps if s.lane == "seller" and s.action not in SELLER_ACTIONS
    ]
    if seller:
        source = await uow.listings.preview_source(listing.id)
        pinned = None if source is None else source.template
        asked = await open_questions(
            uow,
            listing,
            critical_keys=await uow.product_facts.critical_keys(),
            template=None if pinned is None else pinned.template,
            images=await _images(uow, listing),
            rows=seller,
        )
        # no `human_task_opened` move: the open marketplace row already blocks the listing
        # (`blocked/validation_errors` or `failed`), and a status that keeps the dry run and the
        # revalidate reachable lets a fact fixed outside the inbox clear it (the shared
        # blocking rule: a question never blocks by itself)
        if asked:
            opened["seller"] = asked
        left = set(await uow.listing_issues.unlinked([r.id for r in seller]))
        steps = [
            (
                Step(s.issue_id, "staff", s.cause, staff_action(by_issue[s.issue_id].field_path))
                if s.lane == "seller" and s.issue_id in left and s.action not in SELLER_ACTIONS
                else s
            )
            for s in steps
        ]
    account_steps: dict[tuple[str, str], list[Step]] = {}
    for step in steps:
        if step.lane == "marketplace":
            target = f"{PERMISSION_TARGET}.{listing.category}"[:120]
            account_steps.setdefault(("permission_decision", target), []).append(step)
        elif step.lane == "staff":
            target = staff_target(step.action)
            account_steps.setdefault(("marketplace_error_review", target), []).append(step)
    if not account_steps:
        return opened
    account = listing.marketplace_account_id or await uow.marketplace_accounts.active_for_publish(
        listing.marketplace, None
    )
    if account is None:
        log.info("resolution.no_account_for_task", listing_id=str(listing.id))
        return opened
    for (kind, target), group in sorted(account_steps.items()):
        if kind == "permission_decision":
            row = by_issue[group[0].issue_id]
            # SI13: a brand restriction found by the pre-check carries Amazon's Seller Central
            # link on its own line; only an allowed https host passes (CP-D13)
            link = link_in(row.raw_message) or link_in(row.message)
            question, reason = seller_central_step(row.marketplace_code, listing.category, link)
        else:
            question, reason = staff_step(group[0].action)
        task_id = await uow.human_tasks.open_account_task(
            kind, account, target, question, reason, {"kind": "acknowledge"}
        )
        if task_id is None:
            continue
        await uow.listing_issues.link_ids([s.issue_id for s in group], task_id)
        opened.setdefault("marketplace" if kind == "permission_decision" else "staff", []).append(
            task_id
        )
    log.info(
        "resolution.tasks_opened",
        listing_id=str(listing.id),
        lanes=",".join(sorted(opened)),
        tasks=sum(len(v) for v in opened.values()),
    )
    return opened


def _to_staff(step: Step, by_issue: Mapping[uuid.UUID, Any]) -> Step:
    path = by_issue[step.issue_id].field_path
    if seller_answers(path):
        return step
    return Step(step.issue_id, "staff", step.cause, staff_action(path))


async def _images(uow: TenantUnitOfWork, listing: Any) -> Sequence[Any]:
    """The current version's `ImageRef`s (`images[<i>]` -> image id for an image question)."""
    from app.services.generation_context import previous_listing

    if listing.current_version_id is None:
        return ()
    version = await uow.listing_versions.get(listing.id, listing.current_version_id)
    canonical = None if version is None else previous_listing(listing, version)
    return () if canonical is None else canonical.images


async def _recategorizable(uow: TenantUnitOfWork, listing: Any, rows: Sequence[Any]) -> bool:
    """SI13 T7: read only when an adapter CATEGORY_MISMATCH row is open (the planner's rule 2)."""
    from app.services.product_recategorize import recategorizable

    if not any(r.error_code == ErrorCode.CATEGORY_MISMATCH.value for r in rows):
        return False
    return await recategorizable(uow, listing)


async def _audit_held(
    uow: TenantUnitOfWork,
    listing: Any,
    job: JobRun,
    rows: Mapping[uuid.UUID, Any],
    *,
    action: str,
    user_id: uuid.UUID | None = None,
) -> None:
    """Client item 20: one audit row per suggested (`listing_repair.suggested`, the system) or
    released (`listing_repair.approved`, the seller) repair. `rows` is repair id -> its rule and
    attribute holder."""
    for repair_id, repair in rows.items():
        await uow.audit.write(
            tenant_id=listing.tenant_id,
            actor_type="system" if user_id is None else "user",
            actor_user_id=user_id,
            action=action,
            entity_type="listing_repair",
            entity_id=repair_id,
            after=json.dumps({"rule": repair.rule, "attribute": repair.attribute}),
            job_id=job.id,
        )


async def _resolve(
    uow: TenantUnitOfWork,
    listing: Any,
    *,
    entry: Entry,
    job: JobRun,
    approved_by: uuid.UUID | None = None,
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
    # client item 20: the held suggestions count as written for the one-live-row rules
    live = await uow.listing_repairs.live_for_listing(listing.id, include_held=True)
    held = {r.source_issue_id: r for r in live if r.held and r.source_issue_id is not None}
    auto_fix = approved_by is not None or await uow.tenants.auto_fix_enabled()
    found = await _candidates(uow, listing, rows, stored=stored, entry=entry, live=live)
    candidates = found.candidates
    recurrence = await uow.resolution.recurrence_resolutions(
        [r.recurrence_of_id for r in rows if r.recurrence_of_id is not None]
    )
    cycles = await uow.resolution.cycles_since_human(listing.id)
    content_fixable = await _content_fixable(uow, listing, rows)
    ctx = PlanContext(
        content_fixable=content_fixable,
        recategorizable=await _recategorizable(uow, listing, rows),
        ai_allowed=await _ai_allowed(uow, listing.tenant_id),
        critical_keys=await uow.product_facts.critical_keys(),
        repairable=frozenset(candidates),
        repair_rejected=found.rejected,
        approved_current=listing.approved_version_id == listing.current_version_id,
        cycles_used=cycles,
        locked_paths=frozenset(getattr(listing, "locked_paths", None) or ()),
        auto_fix_enabled=auto_fix,
    )
    steps = plan([_view(r, recurrence) for r in rows], ctx)
    by_issue = {r.id: r for r in rows}
    ids: dict[uuid.UUID, uuid.UUID] = {}
    suggested: dict[uuid.UUID, Any] = {}
    released: dict[uuid.UUID, Any] = {}
    if auto_fix:
        # client item 20: the seller applied the suggestions, or the owner turned automatic
        # fixes back on; each held repair goes live now (never left to a staff repair review)
        for repair_id in await uow.listing_repairs.release([r.id for r in held.values()]):
            freed = next(r for r in held.values() if r.id == repair_id)
            released[repair_id] = freed
            if freed.source_issue_id is not None and freed.source_issue_id in candidates:
                ids[freed.source_issue_id] = repair_id
    for step in steps:
        held_fix = step.action == APPROVE_FIX and step.issue_id in candidates
        if step.lane != "auto_repair" and not held_fix:
            continue
        if step.issue_id in ids:
            continue  # released above
        if held_fix and step.issue_id in held:
            continue  # already suggested, still waiting for the seller
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
                held=held_fix,
            )
        )
        if inserted is not None and held_fix:
            suggested[inserted] = repair
        elif inserted is not None:
            ids[step.issue_id] = inserted
    # a planned repair the store refused (a live repair of the attribute, or this key and rule
    # tried once already) is no automatic step: staff looks (never `auto_repair` with no repair)
    steps = tuple(
        (
            Step(s.issue_id, "staff", s.cause, "repair_rejected")
            if s.lane == "auto_repair" and s.issue_id not in ids and s.issue_id not in found.waiting
            else s
        )
        for s in steps
    )
    written = {i: candidates[i] for i in ids}
    fresh_ids = {i: r for i, r in ids.items() if r not in released}
    await _audit(uow, listing, job, {i: candidates[i] for i in fresh_ids}, fresh_ids)
    await _audit_held(uow, listing, job, suggested, action="listing_repair.suggested")
    await _audit_held(
        uow, listing, job, released, action="listing_repair.approved", user_id=approved_by
    )
    tasks = await _human_steps(uow, listing, steps, by_issue)
    automatic = all(s.lane in ("auto_repair", "auto_content", "ai_field", "retry") for s in steps)
    content = any(s.lane in ("auto_content", "ai_field") for s in steps)
    if not steps or not automatic or not (written or content):
        return ResolutionResult(steps=steps, repairs=tuple(ids.values()), tasks=tasks)
    if cycles >= MAX_CYCLES:
        await _cap(uow, listing, job)
        return ResolutionResult(steps=steps, repairs=tuple(ids.values()), capped=True, tasks=tasks)
    if content:
        # T15: a content fix makes a new version a non-maker approves; never a resend. The
        # repairs written beside it stay live and go out with that version (CP-D21)
        fix_job = await _auto_fix(uow, listing, job, steps, by_issue)
        return ResolutionResult(steps=steps, repairs=tuple(ids.values()), job_id=fix_job)
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


HUMAN_LANES: Final = frozenset({"seller", "marketplace", "staff"})


async def _hand_over(
    uow: TenantUnitOfWork, listing: Any, issue_ids: Sequence[uuid.UUID]
) -> dict[str, list[uuid.UUID]]:
    rows = [
        r
        for r in await uow.listing_issues.open_by_ids(listing.id, issue_ids)
        if r.human_task_id is None
    ]
    if not rows:
        return {}
    recurrence = await uow.resolution.recurrence_resolutions(
        [r.recurrence_of_id for r in rows if r.recurrence_of_id is not None]
    )
    # no content fix and no AI: the lanes D-27 gives a content refusal nobody can fix by code
    ctx = PlanContext(critical_keys=await uow.product_facts.critical_keys())
    by_issue = {r.id: r for r in rows}
    steps = [
        (
            s
            if s.lane in HUMAN_LANES
            else Step(s.issue_id, "staff", s.cause, staff_action(by_issue[s.issue_id].field_path))
        )
        for s in plan([_view(r, recurrence) for r in rows], ctx)
    ]
    return await _human_steps(uow, listing, steps, by_issue)


async def hand_over(
    uow: TenantUnitOfWork, listing: Any, issue_ids: Sequence[uuid.UUID], *, job: JobRun
) -> dict[str, list[uuid.UUID]]:
    """Fix round 1 (part B): a content or AI fix job (`auto_fix`, `ai_field`) that ended with no
    version (the AI refused at claim, the base no longer current, a context it could not build)
    leaves the refusals it was to fix open; each still-open row with no task goes to the human
    lanes the way D-27 routes it (a staff content review, or a seller question for a fact), so
    someone is always told to act. In its own SAVEPOINT; a failure is logged, never raised."""
    try:
        async with uow.session.begin_nested():
            opened = await _hand_over(uow, listing, issue_ids)
    except Exception as exc:  # the savepoint rolled back; the listing's move stays
        log.error(
            "resolution.hand_over_failed",
            listing_id=str(listing.id),
            job_run_id=str(job.id),
            error_class=type(exc).__name__,
        )
        return {}
    if opened:
        log.info(
            "resolution.handed_over",
            listing_id=str(listing.id),
            job_run_id=str(job.id),
            lanes=",".join(sorted(opened)),
        )
    return opened


async def resolve(
    uow: TenantUnitOfWork,
    listing: Any,
    *,
    entry: Entry,
    job: JobRun,
    approved_by: uuid.UUID | None = None,
) -> ResolutionResult | None:
    """The executor (module docstring), in its own SAVEPOINT. `approved_by` is the seller who
    applied Kuinn's suggested fixes (client item 20): this one run may fix automatically."""
    if listing is None or listing.paused_at is not None or listing.archived_at is not None:
        return None
    try:
        async with uow.session.begin_nested():
            result = await _resolve(uow, listing, entry=entry, job=job, approved_by=approved_by)
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
