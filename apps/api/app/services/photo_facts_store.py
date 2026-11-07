"""Applying one generation's photo decision inside its terminal transaction (SI3 T12).

`apply_photo_decision` runs in `make_persist`, after the listing lock and before any run is
inserted, in this order:

1. The product row lock (`products.get_for_update`), held only for this short transaction, so two
   generations of one product (Amazon and Flipkart together) apply their decisions one at a time.
2. The newest recorded photo set. When it is not the one this job saw at read time, another
   photo decision was applied meanwhile: this run is `superseded` and writes nothing, opens
   nothing and records no set. Only the first decision applies, so both listings stay approvable.
3. Each write, in its own SAVEPOINT, through `FactWriter.set_photo_attribute` with the build-time
   envelope as the compare-and-set (a seller edit or reject made during the run wins).
4. Each question, in its own SAVEPOINT, through `HumanTaskRepository.open_fact_task`.
5. A refusal (an `InputError` or a guard SQLSTATE) is logged by key and code and skipped, so the
   version is never lost. Audit: one `product.fact_updated` row for the writes and one
   `human_task.opened` row for the questions, both `actor_type='system'` with the job id.
6. The facts are read again, for the re-check digest (`recheck_digest`).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.exc import DBAPIError

from app.core.errors import InputError
from app.core.logging import get_logger
from app.core.resolved_facts import ProductFacts, ResolvedFact
from app.repositories.uow import TenantUnitOfWork
from app.services.photo_facts import PhotoDecision, PhotoSet
from app.services.product_facts import fact_stamp

log = get_logger(__name__)
# the fact guard's PRV codes and a CHECK violation: a refused row, never a broken transaction
_REFUSED_SQLSTATES: Final = frozenset({"PRV01", "PRV02", "PRV03", "23514"})


@dataclass(frozen=True)
class PhotoApplied:
    reread: ProductFacts
    record_set: PhotoSet | None
    written: tuple[str, ...] = ()
    tasks: int = 0


def _envelope(fact: ResolvedFact | None) -> dict[str, Any] | None:
    """The own-row Fact at build time, as stored (None: absent, or only inherited)."""
    if fact is None or fact.inherited_from is not None:
        return None
    return fact.model_dump(
        mode="json", exclude={"key", "inherited_from", "is_critical"}, exclude_none=True
    )


def _refusal(exc: Exception) -> str | None:
    if isinstance(exc, InputError):
        return exc.code.value
    if isinstance(exc, DBAPIError):
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if isinstance(sqlstate, str) and sqlstate in _REFUSED_SQLSTATES:
            return sqlstate
    return None


async def apply_photo_decision(
    uow: TenantUnitOfWork,
    *,
    job_id: uuid.UUID,
    product_id: uuid.UUID,
    decision: PhotoDecision,
    pre_facts: ProductFacts,
    seen_set: PhotoSet | None,
    sent_set: PhotoSet,
    now: datetime,
) -> PhotoApplied:
    """The module docstring's steps; the re-read facts and the set to record (or None)."""
    product = await uow.products.get_for_update(product_id)
    if product is None:
        return PhotoApplied(reread=pre_facts, record_set=None)
    latest = await uow.job_runs.latest_photo_set(product_id)
    written: list[str] = []
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    opened: list[str] = []
    record_set: PhotoSet | None = None
    if latest != seen_set:
        log.info("generation.photo_superseded", job_id=str(job_id))
    else:
        stamp = fact_stamp(now)
        for write in decision.writes:
            expected = _envelope(pre_facts.attributes.get(write.key))
            fact = {
                "value": write.value,
                "normalized": write.value,
                "source": "ai",
                "captured_via": "agent",
                "agent_run_id": str(decision.run_id),
                "confidence": write.confidence,
                "evidence_ref": f"image:{write.image_id}",
                "updated_at": stamp,
            }
            try:
                async with uow.session.begin_nested():
                    landed = await uow.facts.set_photo_attribute(
                        product_id, write.key, fact, expected=expected
                    )
            except (InputError, DBAPIError) as exc:
                code = _refusal(exc)
                if code is None:
                    raise
                log.warning("generation.photo_write_skipped", key=write.key, code=code)
                continue
            if landed:
                written.append(write.key)
                before[write.key] = expected
                after[write.key] = fact
        for task in decision.tasks:
            try:
                async with uow.session.begin_nested():
                    landed = await uow.human_tasks.open_fact_task(
                        product_id=product_id,
                        kind=task.kind,
                        target_path=task.target_path,
                        question=task.question,
                        reason=task.reason,
                        expected_answer=task.expected_answer,
                        suggestions=list(task.suggestions),
                        evidence=list(task.evidence),
                        image_id=task.image_id,
                    )
            except (InputError, DBAPIError) as exc:
                code = _refusal(exc)
                if code is None:
                    raise
                log.warning("generation.photo_task_skipped", key=task.key, code=code)
                continue
            if landed:
                opened.append(task.target_path)
        if written:
            await uow.audit.write(
                tenant_id=uow.products.tenant_id,
                actor_type="system",
                actor_user_id=None,
                action="product.fact_updated",
                entity_type="product",
                entity_id=product_id,
                before=json.dumps({"attributes": before}, sort_keys=True),
                after=json.dumps({"attributes": after}, sort_keys=True),
                job_id=job_id,
            )
        if opened:
            await uow.audit.write(
                tenant_id=uow.products.tenant_id,
                actor_type="system",
                actor_user_id=None,
                action="human_task.opened",
                entity_type="product",
                entity_id=product_id,
                reason=",".join(opened),
                job_id=job_id,
            )
        if decision.run_id is not None:
            record_set = sent_set
    # the Core UPDATEs left the locked row stale in the identity map: reload it on the re-read
    uow.session.expire(product)
    reread = await uow.product_facts.resolve(product_id)
    return PhotoApplied(
        reread=pre_facts if reread is None else reread,
        record_set=record_set,
        written=tuple(written),
        tasks=len(opened),
    )
