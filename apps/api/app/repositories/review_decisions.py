"""`review_decisions` (DATABASE.md:795-806; S18 T4): append-only.

- `insert_regenerate_requested(...)`: the decision a user `regenerate` needs in its transaction
  (the events guard checks it is decided now by `app.user_id` on the current version).
- `latest_reject_feedback(listing_id, limit=5)`: the newest reject feedback, newest first (it
  feeds `GenerationContext.review_feedback`).
- `instructions_for_job(job_id)`: the `instructions` of the `regenerate_requested` row that
  started a job, or the `feedback` of the `reject` row of a `reject_regenerate` (S19; DATABASE.md
  :805); it feeds `GenerationContext.user_instructions`.
- `insert_approve(...)` / `insert_reject(...)` (S19 T3): the approve and reject decisions. One
  `approve` row per version (a second is `ConflictError`); a multi-item batch's approve of a
  listing that is not `validated` is refused by the bulk backstop (REV01 → 409).
"""

from __future__ import annotations

import builtins
import uuid
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.integrity import map_db_error, to_conflict
from app.db.models.listings import ReviewDecision
from app.db.session import USER_INFO_KEY
from app.repositories.base import TenantScopedRepository

REGENERATE_REQUESTED = "regenerate_requested"
FEEDBACK_LIMIT = 5
# a job is a regeneration when one of these decisions names it (a user regenerate, or a
# reject_regenerate's reject)
STARTS_REGENERATION = (REGENERATE_REQUESTED, "reject")


class ReviewDecisionRepository(TenantScopedRepository[ReviewDecision]):
    model = ReviewDecision

    def _decided_by(self) -> uuid.UUID:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("review_decisions write for another tenant's unit of work")
        decided_by = self.db.info.get(USER_INFO_KEY)
        if not isinstance(decided_by, uuid.UUID):
            raise ValueError("a review decision needs the user's unit of work")
        return decided_by

    async def _insert(self, values: dict[str, Any]) -> uuid.UUID:
        stmt = pg_insert(ReviewDecision).values(**values).returning(ReviewDecision.id)
        try:
            new_id: uuid.UUID = (await self.db.execute(stmt)).scalar_one()
        except IntegrityError as exc:
            raise to_conflict(exc) from exc
        except DBAPIError as exc:
            mapped = map_db_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        return new_id

    async def insert_approve(
        self,
        listing_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        batch_id: uuid.UUID,
        facts_digest: bytes,
        request_id: str | None = None,
    ) -> uuid.UUID:
        return await self._insert(
            {
                "tenant_id": self.tenant_id,
                "listing_id": listing_id,
                "listing_version_id": version_id,
                "decision": "approve",
                "facts_digest": facts_digest,
                "decided_by_user_id": self._decided_by(),
                "batch_id": batch_id,
                "request_id": request_id,
            }
        )

    async def approve_digest(self, version_id: uuid.UUID) -> bytes | None:
        """The `approve` decision's `facts_digest` of a version (S21: the publish pin, C:48)."""
        stmt = (
            self._select()
            .where(
                ReviewDecision.listing_version_id == version_id,
                ReviewDecision.decision == "approve",
            )
            .with_only_columns(ReviewDecision.facts_digest)
            .order_by(ReviewDecision.decided_at.desc())
            .limit(1)
        )
        found: bytes | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def insert_reject(
        self,
        listing_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        reason_code: str,
        feedback: str,
        batch_id: uuid.UUID,
        regeneration_job_id: uuid.UUID | None = None,
        request_id: str | None = None,
    ) -> uuid.UUID:
        return await self._insert(
            {
                "tenant_id": self.tenant_id,
                "listing_id": listing_id,
                "listing_version_id": version_id,
                "decision": "reject",
                "reason_code": reason_code,
                "feedback": feedback,
                "decided_by_user_id": self._decided_by(),
                "batch_id": batch_id,
                "regeneration_job_id": regeneration_job_id,
                "request_id": request_id,
            }
        )

    async def insert_regenerate_requested(
        self,
        listing_id: uuid.UUID,
        version_id: uuid.UUID | None,
        instructions: str | None,
        regeneration_job_id: uuid.UUID,
        *,
        request_id: str | None = None,
    ) -> uuid.UUID:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("review_decisions write for another tenant's unit of work")
        decided_by = self.db.info.get(USER_INFO_KEY)
        if not isinstance(decided_by, uuid.UUID):
            raise ValueError("a regenerate_requested decision needs the user's unit of work")
        stmt = (
            pg_insert(ReviewDecision)
            .values(
                tenant_id=self.tenant_id,
                listing_id=listing_id,
                listing_version_id=version_id,
                decision=REGENERATE_REQUESTED,
                instructions=instructions,
                decided_by_user_id=decided_by,
                regeneration_job_id=regeneration_job_id,
                request_id=request_id,
            )
            .returning(ReviewDecision.id)
        )
        new_id: uuid.UUID = (await self.db.execute(stmt)).scalar_one()
        return new_id

    async def latest_reject_feedback(
        self, listing_id: uuid.UUID, limit: int = FEEDBACK_LIMIT
    ) -> builtins.list[str]:
        if not 1 <= limit <= FEEDBACK_LIMIT:
            raise ValueError(f"limit must be in 1..{FEEDBACK_LIMIT}")
        stmt = (
            self._select()
            .where(
                ReviewDecision.listing_id == listing_id,
                ReviewDecision.decision == "reject",
                ReviewDecision.feedback.is_not(None),
            )
            .order_by(ReviewDecision.decided_at.desc(), ReviewDecision.id.desc())
            .limit(limit)
            .with_only_columns(ReviewDecision.feedback)
        )
        rows = (await self.db.execute(stmt)).scalars()
        return [text for text in rows if text and text.strip()]

    async def instructions_for_job(self, job_id: uuid.UUID) -> str | None:
        stmt = (
            self._select()
            .where(
                ReviewDecision.regeneration_job_id == job_id,
                ReviewDecision.decision.in_(STARTS_REGENERATION),
            )
            .order_by(ReviewDecision.decided_at.desc(), ReviewDecision.id.desc())
            .limit(1)
            .with_only_columns(
                ReviewDecision.decision, ReviewDecision.instructions, ReviewDecision.feedback
            )
        )
        row = (await self.db.execute(stmt)).one_or_none()
        if row is None:
            return None
        found: str | None = row.feedback if row.decision == "reject" else row.instructions
        return found

    async def has_regenerate_for_job(self, job_id: uuid.UUID) -> bool:
        stmt = (
            self._select()
            .where(
                ReviewDecision.regeneration_job_id == job_id,
                ReviewDecision.decision.in_(STARTS_REGENERATION),
            )
            .limit(1)
            .with_only_columns(ReviewDecision.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None
