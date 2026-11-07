"""`human_tasks` for marketplace accounts (DATABASE.md:1338-1355; S15): the one live
`reconnect_account` task per account (`ht_one_live_account`), opened when a refresh is rejected,
applied when the same seller reconnects, cancelled (`superseded`) when the account is revoked.
A new account that replaces a revoked one gets one already applied when a publish attempt was
stranded by the revoke (`applied_by_connect`). The 0004 guard allows only `open -> answered|cancelled` and `answered -> applied|cancelled`, so
applying is two steps. Inserts go through Core (the `request_id` column default stamps them).

SI3 adds the fact tasks a generation's photo screen opens (`missing_fact`, `conflicting_facts` on
`facts.attributes.<key>`, `resume_from='generate'`): one live task per product and target
(`ht_one_live_fact`, so a second run or the other marketplace opens no duplicate), cancelled
(`superseded`) when the seller writes the key, answered and applied by `tasks_answer`."""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import literal, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models.catalog import Product
from app.db.models.issues import HumanTask
from app.repositories.base import TenantScopedRepository

RECONNECT_KIND: Final = "reconnect_account"
RECONNECT_TARGET: Final = "account.oauth"
RECONNECT_ANSWER: Final = {"kind": "reconnect"}
RECONNECT_QUESTION: Final = "Reconnect this marketplace account."
RECONNECT_REASON: Final = (
    "The marketplace refused to refresh the connection; publishing to it waits for a reconnect."
)

CONNECTED_QUESTION: Final = "Connect the marketplace account again."
CONNECTED_REASON: Final = (
    "The account was revoked while a publish was running; connecting a new account resolves it."
)

_ANSWER: Final = text(
    "UPDATE human_tasks SET status = 'answered', answer = CAST(:answer AS jsonb), "
    "answered_by_user_id = :user, answered_at = now() "
    "WHERE tenant_id = :t AND marketplace_account_id = :a AND kind = 'reconnect_account' "
    "AND status = 'open'"
)
_APPLY: Final = text(
    "UPDATE human_tasks SET status = 'applied', applied_at = now() "
    "WHERE tenant_id = :t AND marketplace_account_id = :a AND kind = 'reconnect_account' "
    "AND status = 'answered'"
)
_CANCEL: Final = text(
    "UPDATE human_tasks SET status = 'cancelled', cancelled_reason = 'superseded' "
    "WHERE tenant_id = :t AND marketplace_account_id = :a AND kind = 'reconnect_account' "
    "AND status IN ('open', 'answered')"
)


TASK_PAGE_MAX: Final = 200
FACT_TASK_KINDS: Final = ("missing_fact", "conflicting_facts")
LIVE_STATUSES: Final = ("open", "answered")


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


class HumanTaskRepository(TenantScopedRepository[HumanTask]):
    model = HumanTask

    async def open_reconnect(self, account_id: uuid.UUID) -> bool:
        """Open the account's reconnect task; False when one is already live."""
        stmt = (
            insert(HumanTask)
            .values(
                tenant_id=self.tenant_id,
                kind=RECONNECT_KIND,
                target_path=RECONNECT_TARGET,
                marketplace_account_id=account_id,
                question=RECONNECT_QUESTION,
                reason=RECONNECT_REASON,
                expected_answer=dict(RECONNECT_ANSWER),
                resume_from="publish",
            )
            .on_conflict_do_nothing()
            .returning(HumanTask.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def apply_reconnect(self, account_id: uuid.UUID, user_id: uuid.UUID) -> int:
        """The live task answered by `user_id` and applied (two guarded steps); the count."""
        import json

        params = {"t": self.tenant_id, "a": account_id, "user": user_id}
        await self.db.execute(_ANSWER, {**params, "answer": json.dumps(RECONNECT_ANSWER)})
        return _rowcount(await self.db.execute(_APPLY, params))

    async def applied_by_connect(
        self, account_id: uuid.UUID, user_id: uuid.UUID
    ) -> uuid.UUID | None:
        """A `reconnect_account` task on a newly connected account, answered by `user_id` and
        applied at once (the connect is the resolution): what a publish attempt stranded by a
        revoke links its AUTH_EXPIRED issue to, so the publish gate and `human_resolution`
        accept the next attempt. None when the account already has a live reconnect task."""
        stmt = (
            insert(HumanTask)
            .values(
                tenant_id=self.tenant_id,
                kind=RECONNECT_KIND,
                target_path=RECONNECT_TARGET,
                marketplace_account_id=account_id,
                question=CONNECTED_QUESTION,
                reason=CONNECTED_REASON,
                expected_answer=dict(RECONNECT_ANSWER),
                resume_from="publish",
            )
            .on_conflict_do_nothing()
            .returning(HumanTask.id)
        )
        task_id: uuid.UUID | None = (await self.db.execute(stmt)).scalar_one_or_none()
        if task_id is None:
            return None
        await self.apply_reconnect(account_id, user_id)
        return task_id

    async def any_applied_after(self, task_ids: builtins.list[uuid.UUID], at: datetime) -> bool:
        """S21 `human_resolution`: one of `task_ids` reached `applied` after `at`."""
        if not task_ids:
            return False
        stmt = (
            self._select()
            .where(
                HumanTask.id.in_(task_ids),
                HumanTask.status == "applied",
                HumanTask.applied_at > at,
            )
            .with_only_columns(HumanTask.id)
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def live_account_task(
        self, account_id: uuid.UUID, kind: str, target_path: str
    ) -> uuid.UUID | None:
        stmt = (
            self._select()
            .where(
                HumanTask.marketplace_account_id == account_id,
                HumanTask.kind == kind,
                HumanTask.target_path == target_path,
                HumanTask.status.in_(("open", "answered")),
            )
            .with_only_columns(HumanTask.id)
        )
        found: uuid.UUID | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def open_account_task(
        self,
        kind: str,
        account_id: uuid.UUID,
        target_path: str,
        question: str,
        reason: str,
        expected_answer: dict[str, Any],
    ) -> uuid.UUID | None:
        """S21 CP8: one live account task per (account, kind, target), `resume_from='publish'`
        (`ht_one_live_account`, ON CONFLICT DO NOTHING); returns the live task's id."""
        stmt = (
            insert(HumanTask)
            .values(
                tenant_id=self.tenant_id,
                kind=kind,
                target_path=target_path,
                marketplace_account_id=account_id,
                question=question,
                reason=reason,
                expected_answer=expected_answer,
                resume_from="publish",
            )
            .on_conflict_do_nothing()
        )
        await self.db.execute(stmt)
        return await self.live_account_task(account_id, kind, target_path)

    # ---- SI3: fact tasks ----

    async def open_fact_task(
        self,
        *,
        product_id: uuid.UUID,
        kind: str,
        target_path: str,
        question: str,
        reason: str,
        expected_answer: dict[str, Any],
        suggestions: builtins.list[dict[str, Any]],
        evidence: builtins.list[dict[str, Any]],
        image_id: uuid.UUID | None = None,
    ) -> bool:
        """Open one fact question on a live product of this tenant; False when one is already
        live for the target (`ht_one_live_fact`) or the product is not this tenant's."""
        if kind not in FACT_TASK_KINDS:
            raise ValueError(f"{kind!r} is not a fact task kind")
        live = select(Product.id).where(
            self._criterion_for(Product), Product.id == product_id, Product.deleted_at.is_(None)
        )
        if (await self.db.execute(self._scoped_read(live))).first() is None:
            return False
        stmt = (
            insert(HumanTask)
            .values(
                tenant_id=self.tenant_id,
                kind=kind,
                product_id=product_id,
                target_path=target_path,
                image_id=image_id,
                question=question,
                reason=reason,
                expected_answer=expected_answer,
                suggestions=suggestions,
                evidence=evidence,
                resume_from="generate",
            )
            .on_conflict_do_nothing()
            .returning(HumanTask.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def cancel_fact_tasks(
        self, product_id: uuid.UUID, target_paths: builtins.list[str] | tuple[str, ...]
    ) -> int:
        """Cancel (`superseded`) the live fact tasks on these targets of one product: the seller
        wrote the value (CP-D11). The count."""
        if not target_paths:
            return 0
        stmt = self._scoped_write(
            update(HumanTask)
            .where(
                self._tenant_criterion(include_platform=False),
                HumanTask.product_id == product_id,
                HumanTask.kind.in_(FACT_TASK_KINDS),
                HumanTask.status.in_(LIVE_STATUSES),
                HumanTask.target_path.in_(builtins.list(target_paths)),
            )
            .values(status="cancelled", cancelled_reason="superseded")
        )
        return _rowcount(await self.db.execute(stmt))

    async def get_for_update(self, task_id: uuid.UUID) -> HumanTask | None:
        """One task of this tenant, row-locked for the answer (two answers serialise here)."""
        stmt = (
            self._select()
            .where(HumanTask.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        found: HumanTask | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def answer_and_apply(
        self, task_id: uuid.UUID, *, answer: dict[str, Any], user_id: uuid.UUID
    ) -> int:
        """`open -> answered -> applied` in two guarded steps (the 0004 path); 1 when applied,
        0 when the task was not open (cancelled, or another tenant's)."""
        answered = self._scoped_write(
            update(HumanTask)
            .where(
                self._tenant_criterion(include_platform=False),
                HumanTask.id == task_id,
                HumanTask.status == "open",
            )
            .values(
                status="answered",
                answer=answer,
                answered_by_user_id=user_id,
                answered_at=text("now()"),
            )
        )
        if _rowcount(await self.db.execute(answered)) == 0:
            return 0
        applied = self._scoped_write(
            update(HumanTask)
            .where(
                self._tenant_criterion(include_platform=False),
                HumanTask.id == task_id,
                HumanTask.status == "answered",
            )
            .values(status="applied", applied_at=text("now()"))
        )
        return _rowcount(await self.db.execute(applied))

    async def cancel_reconnect(self, account_id: uuid.UUID) -> int:
        return _rowcount(await self.db.execute(_CANCEL, {"t": self.tenant_id, "a": account_id}))

    async def page(
        self,
        status: str,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> tuple[builtins.list[HumanTask], bool]:
        """S19 T21: the task inbox, newest first by `(created_at, id)` (the inbox index
        `(tenant_id, status, created_at DESC, id DESC)`), and whether more rows follow."""
        if isinstance(limit, bool) or not 1 <= limit <= TASK_PAGE_MAX:
            raise ValueError(f"limit must be in 1..{TASK_PAGE_MAX}")
        stmt = self._select().where(HumanTask.status == status)
        if after is not None:
            at, row_id = after
            bound = tuple_(
                literal(at, HumanTask.created_at.type), literal(row_id, HumanTask.id.type)
            )
            stmt = stmt.where(tuple_(HumanTask.created_at, HumanTask.id) < bound)
        stmt = stmt.order_by(HumanTask.created_at.desc(), HumanTask.id.desc()).limit(limit + 1)
        rows = builtins.list((await self.db.execute(stmt)).scalars())
        return rows[:limit], len(rows) > limit
