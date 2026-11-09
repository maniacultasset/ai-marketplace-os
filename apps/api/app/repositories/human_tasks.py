"""`human_tasks` for marketplace accounts (DATABASE.md:1338-1355; S15): the one live
`reconnect_account` task per account (`ht_one_live_account`), opened when a refresh is rejected,
applied when the same seller reconnects, cancelled (`superseded`) when the account is revoked.
A new account that replaces a revoked one gets one already applied when a publish attempt was
stranded by the revoke (`applied_by_connect`). The 0004 guard allows only `open -> answered|cancelled` and `answered -> applied|cancelled`, so
applying is two steps. Inserts go through Core (the `request_id` column default stamps them).

SI3 adds the fact tasks a generation's photo screen opens (`missing_fact`, `conflicting_facts` on
`facts.attributes.<key>`, `resume_from='generate'`): one live task per product and target
(`ht_one_live_fact`, so a second run or the other marketplace opens no duplicate), cancelled
(`superseded`) when the seller writes the key or rejects its photo value, answered and applied by
`tasks_answer`."""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import exists, literal, select, text, tuple_, update
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
LIVE: Final = ("open", "answered")
MISSING_FACT: Final = "missing_fact"
IMAGE_REPLACEMENT: Final = "image_replacement"
FILE_ANSWER: Final = {"kind": "file"}
# SI14: one live category review per product (`human_tasks_one_category_review`, 0027)
CATEGORY_REVIEW: Final = "category_review"
CATEGORY_REVIEW_QUESTION: Final = "Which category fits this product?"
_LIVE_REVIEW: Final = text("kind = 'category_review' AND status IN ('open', 'answered')")


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

    # ---- SI4 A12: fact and image questions for the issue inbox ----

    async def _live_fact(
        self, product_id: uuid.UUID, colourway_id: uuid.UUID | None, target_path: str
    ) -> uuid.UUID | None:
        """The live fact task at the target, taken FOR UPDATE: a concurrent `cancel_if_closed`
        waits for this transaction, so it sees the link the caller writes next. READ COMMITTED
        re-checks the status after a wait, so a task a canceller committed first reads as gone."""
        colourway = (
            HumanTask.colourway_id.is_(None)
            if colourway_id is None
            else HumanTask.colourway_id == colourway_id
        )
        stmt = (
            self._select()
            .where(
                HumanTask.product_id == product_id,
                colourway,
                HumanTask.target_path == target_path,
                HumanTask.marketplace_account_id.is_(None),
                HumanTask.status.in_(LIVE),
            )
            .with_only_columns(HumanTask.id)
            .with_for_update()
        )
        found: uuid.UUID | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def open_fact_task(
        self,
        product_id: uuid.UUID,
        target_path: str,
        question: str,
        reason: str,
        expected_answer: dict[str, Any],
        *,
        colourway_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """One live `missing_fact` task per fact (`ht_one_live_fact`, ON CONFLICT DO NOTHING),
        `resume_from='generate'`; a live task another lane opened at the same target is reused.
        Returns the live task's id, locked until the caller commits. If a canceller committed
        between the insert and the locked read, both run once more; a second miss raises, so an
        issue is never left linked to nothing."""
        for _attempt in range(2):
            await self.db.execute(
                insert(HumanTask)
                .values(
                    tenant_id=self.tenant_id,
                    kind=MISSING_FACT,
                    product_id=product_id,
                    colourway_id=colourway_id,
                    target_path=target_path,
                    question=question,
                    reason=reason,
                    expected_answer=expected_answer,
                    resume_from="generate",
                )
                .on_conflict_do_nothing()
            )
            found = await self._live_fact(product_id, colourway_id, target_path)
            if found is not None:
                return found
        raise RuntimeError("no live fact task after a re-open")

    async def open_image_task(
        self, product_id: uuid.UUID, image_id: uuid.UUID, question: str, reason: str
    ) -> uuid.UUID:
        """One live `image_replacement` task per image (CP-D12: no unique index covers it). The
        product's image lock serialises two marketplace listings of one product, then a live
        task for the image is reused FOR UPDATE, else one is inserted (`resume_from='validate'`)."""
        from app.repositories.images import ImageRepository

        await ImageRepository(self.db, self.tenant_id).lock_product_images(product_id)
        stmt = (
            self._select()
            .where(
                HumanTask.kind == IMAGE_REPLACEMENT,
                HumanTask.image_id == image_id,
                HumanTask.status.in_(LIVE),
            )
            .with_only_columns(HumanTask.id)
            .order_by(HumanTask.created_at, HumanTask.id)
            .limit(1)
            .with_for_update()
        )
        found: uuid.UUID | None = (await self.db.execute(stmt)).scalar_one_or_none()
        if found is not None:
            return found
        created: uuid.UUID = (
            await self.db.execute(
                insert(HumanTask)
                .values(
                    tenant_id=self.tenant_id,
                    kind=IMAGE_REPLACEMENT,
                    product_id=product_id,
                    image_id=image_id,
                    question=question,
                    reason=reason,
                    expected_answer=dict(FILE_ANSWER),
                    resume_from="validate",
                )
                .returning(HumanTask.id)
            )
        ).scalar_one()
        return created

    async def cancel_if_closed(self, task_id: uuid.UUID) -> bool:
        """Cancel a live task (`issues_closed`) once at least one issue links to it and none of
        its linked issues is open. A task no issue links to (another lane's photo task) is never
        cancelled here. The row lock orders two closers, and an opener against a closer."""
        from app.repositories.listing_issues import ListingIssueRepository

        stmt = (
            self._select()
            .where(HumanTask.id == task_id, HumanTask.status.in_(LIVE))
            .with_only_columns(HumanTask.id)
            .with_for_update()
        )
        if (await self.db.execute(stmt)).scalar_one_or_none() is None:
            return False
        total, still_open = await ListingIssueRepository(self.db, self.tenant_id).task_link_counts(
            task_id
        )
        if total == 0 or still_open:
            return False
        criterion = self._tenant_criterion(include_platform=False)
        cancel = self._scoped_write(
            update(HumanTask)
            .where(criterion, HumanTask.id == task_id, HumanTask.status.in_(LIVE))
            .values(status="cancelled", cancelled_reason="issues_closed")
        ).returning(HumanTask.id)
        return (await self.db.execute(cancel)).scalar_one_or_none() is not None

    # ---- SI3: fact tasks ----

    async def open_photo_fact_task(
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
        """Open one photo-screen fact question on a live product of this tenant; False when one
        is already live for the target (`ht_one_live_fact`) or the product is not this tenant's.
        Named apart from the issue-question opener `open_fact_task` (SI4), which returns the live
        task's id; once both have merged, one opener can take kind, suggestions, evidence and
        image or colourway and return the live id."""
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
                HumanTask.status.in_(LIVE),
                HumanTask.target_path.in_(builtins.list(target_paths)),
            )
            .values(status="cancelled", cancelled_reason="superseded")
        )
        return _rowcount(await self.db.execute(stmt))

    async def cancel_rejected_fact_tasks(
        self, product_id: uuid.UUID, target_paths: builtins.list[str] | tuple[str, ...]
    ) -> builtins.list[str]:
        """Cancel (`superseded`) the live fact questions on these targets of one product after the
        seller rejected the photo value for them: the seller has decided the key, so the Inbox
        stops asking. The target paths cancelled, in order; empty when none was live (a repeat)."""
        if not target_paths:
            return []
        stmt = self._scoped_write(
            update(HumanTask)
            .where(
                self._tenant_criterion(include_platform=False),
                HumanTask.product_id == product_id,
                HumanTask.kind.in_(FACT_TASK_KINDS),
                HumanTask.status.in_(LIVE),
                HumanTask.target_path.in_(builtins.list(target_paths)),
            )
            .values(status="cancelled", cancelled_reason="superseded")
        ).returning(HumanTask.target_path)
        return sorted(str(path) for path in (await self.db.execute(stmt)).scalars())

    async def cancel_for_products(
        self, product_ids: builtins.list[uuid.UUID]
    ) -> builtins.list[uuid.UUID]:
        """Product delete: every live (`open`/`answered`) task of these products becomes
        `cancelled` with reason `product_deleted`. The ids, in id order."""
        if not product_ids:
            return []
        stmt = self._scoped_write(
            update(HumanTask)
            .where(
                self._tenant_criterion(include_platform=False),
                HumanTask.product_id.in_(product_ids),
                HumanTask.status.in_(LIVE),
            )
            .values(status="cancelled", cancelled_reason="product_deleted")
        ).returning(HumanTask.id)
        return sorted((await self.db.execute(stmt)).scalars())

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

    async def open_category_review(
        self,
        product_id: uuid.UUID,
        *,
        reason: str,
        allowed: builtins.list[str],
        suggestions: builtins.list[str],
    ) -> bool:
        """SI14: the product's category review; False when one is already live. The conflict
        target is inferred from the partial unique index (a partial index cannot be named in
        `ON CONFLICT ON CONSTRAINT`)."""
        stmt = (
            insert(HumanTask)
            .values(
                tenant_id=self.tenant_id,
                kind=CATEGORY_REVIEW,
                product_id=product_id,
                question=CATEGORY_REVIEW_QUESTION,
                reason=reason,
                expected_answer={"kind": "enum", "allowed": allowed},
                suggestions=suggestions[:10],
                resume_from="generate",
            )
            .on_conflict_do_nothing(
                index_elements=[HumanTask.tenant_id, HumanTask.product_id],
                index_where=_LIVE_REVIEW,
            )
            .returning(HumanTask.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def live_category_review(self, product_id: uuid.UUID) -> HumanTask | None:
        """SI14: the product's open or answered category review, row-locked."""
        stmt = (
            self._select()
            .where(
                HumanTask.product_id == product_id,
                HumanTask.kind == CATEGORY_REVIEW,
                HumanTask.status.in_(LIVE),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        found: HumanTask | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

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
        # a task of a deleted product leaves the inbox, whatever its status
        gone = exists(
            select(Product.id).where(
                Product.tenant_id == HumanTask.tenant_id,
                Product.id == HumanTask.product_id,
                Product.deleted_at.is_not(None),
            )
        )
        stmt = self._select().where(HumanTask.status == status, ~gone)
        if after is not None:
            at, row_id = after
            bound = tuple_(
                literal(at, HumanTask.created_at.type), literal(row_id, HumanTask.id.type)
            )
            stmt = stmt.where(tuple_(HumanTask.created_at, HumanTask.id) < bound)
        stmt = stmt.order_by(HumanTask.created_at.desc(), HumanTask.id.desc()).limit(limit + 1)
        rows = builtins.list((await self.db.execute(stmt)).scalars())
        return rows[:limit], len(rows) > limit
