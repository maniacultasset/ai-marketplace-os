"""`ErrorProposalRepository`: AI-drafted error-map rows for unmapped marketplace codes (SI13 T11;
DATABASE.md `marketplace_error_proposals`, revision 0024).

The table is platform-global (no `tenant_id`, no RLS): a platform reader-writer like
`ErrorMapRepository` (`PLATFORM_READERS`, reason "platform-global, no tenant data"). The worker
drafts through it (`claim`, `finish`, `fail_pending`); only the migrator's staff CLI reviews
(`app.db.platform_loads`). The drafting guard (`marketplace_error_proposals_guard`) refuses any
other move, and nothing on the issue, planner, task or seller path imports this module
(`tests/meta/test_proposals_never_act.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar, Final

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.platform import MarketplaceErrorProposal

MAX_TRIES: Final = 3
DRAFT_COLUMNS: Final = frozenset(
    {
        "plain_cause",
        "suggested_fix",
        "lane_class",
        "error_code",
        "error_class",
        "field_hint",
        "model_used",
        "prompt_version",
        "agent_run_id",
    }
)
_CLAIM: Final = text("""
    INSERT INTO marketplace_error_proposals
      (marketplace, match_on, raw_value, status, tries, sample_issue_id)
    VALUES (:marketplace, 'code', :code, 'pending', 0, :sample)
    ON CONFLICT (marketplace, match_on, raw_value) DO UPDATE
      SET status = 'pending', failure_reason = NULL, next_try_at = NULL,
          sample_issue_id = EXCLUDED.sample_issue_id
      WHERE marketplace_error_proposals.status = 'failed'
        AND marketplace_error_proposals.tries < :max_tries
        AND (marketplace_error_proposals.next_try_at IS NULL
             OR marketplace_error_proposals.next_try_at <= now())
    RETURNING id
    """)


class ErrorProposalRepository:
    model: ClassVar[type[MarketplaceErrorProposal]] = MarketplaceErrorProposal

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def claim(
        self, marketplace: str, code: str, sample_issue_id: uuid.UUID | None
    ) -> uuid.UUID | None:
        """A new `pending` row, or a retryable `failed` row turned back to `pending`; None when
        the code already has a live, proposed, reviewed or exhausted row."""
        found = (
            await self.db.execute(
                _CLAIM,
                {
                    "marketplace": marketplace,
                    "code": code,
                    "sample": sample_issue_id,
                    "max_tries": MAX_TRIES,
                },
            )
        ).scalar_one_or_none()
        return None if found is None else uuid.UUID(str(found))

    async def set_job(self, proposal_id: uuid.UUID, job_id: uuid.UUID) -> None:
        p = MarketplaceErrorProposal
        await self.db.execute(
            update(p).where(p.id == proposal_id, p.status == "pending").values(job_id=job_id)
        )

    async def finish(
        self,
        proposal_id: uuid.UUID,
        *,
        status: str,
        fields: Mapping[str, Any] | None = None,
        failure_reason: str | None = None,
        tries: int | None = None,
        next_try_at: datetime | None = None,
    ) -> bool:
        """`pending` -> `proposed` (the checked draft in `fields`), `invalid` (no text) or `failed`
        (a reason, the new try count and the backoff). False when the row is not pending."""
        if status not in ("proposed", "invalid", "failed"):
            raise ValueError("a draft ends proposed, invalid or failed")
        values: dict[str, Any] = {"status": status}
        if status == "proposed":
            extra = set(fields or {}) - DRAFT_COLUMNS
            if extra:
                raise ValueError(f"not a draft column: {sorted(extra)}")
            values.update(fields or {})
        else:
            kept = ("prompt_version", "model_used", "agent_run_id")  # never text
            values.update({k: v for k, v in (fields or {}).items() if k in kept})
        if status == "failed":
            values["failure_reason"] = failure_reason
            values["next_try_at"] = next_try_at
            if tries is not None:
                values["tries"] = min(tries, MAX_TRIES)
        elif status == "invalid":
            values["failure_reason"] = "invalid_output"
        p = MarketplaceErrorProposal
        stmt = (
            update(p)
            .where(p.id == proposal_id, p.status == "pending")
            .values(**values)
            .returning(p.id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none() is not None

    async def fail_pending(self, proposal_id: uuid.UUID) -> bool:
        """The job ended without finishing the draft (a crash, a dead letter): `failed`,
        `model_failed`, one more try, retry after `tries` hours."""
        found = (
            await self.db.execute(
                text(
                    "UPDATE marketplace_error_proposals SET status = 'failed', "
                    "failure_reason = 'model_failed', tries = LEAST(tries + 1, :max_tries), "
                    "next_try_at = now() + make_interval(hours => LEAST(tries + 1, :max_tries)) "
                    "WHERE id = :id AND status = 'pending' RETURNING id"
                ),
                {"id": proposal_id, "max_tries": MAX_TRIES},
            )
        ).scalar_one_or_none()
        return found is not None

    async def get(self, proposal_id: uuid.UUID) -> MarketplaceErrorProposal | None:
        return await self.db.get(MarketplaceErrorProposal, proposal_id)

    async def list(
        self, status: str | None = None, limit: int = 200
    ) -> list[MarketplaceErrorProposal]:
        p = MarketplaceErrorProposal
        stmt = select(p).order_by(p.created_at.desc(), p.id).limit(max(1, min(limit, 500)))
        if status is not None:
            stmt = stmt.where(p.status == status)
        return list((await self.db.execute(stmt)).scalars())

    async def known_codes(self, marketplace: str) -> dict[str, tuple[str, int, Any]]:
        """code -> (status, tries, next_try_at) for every proposal of the marketplace."""
        p = MarketplaceErrorProposal
        stmt = select(p.raw_value, p.status, p.tries, p.next_try_at).where(
            p.marketplace == marketplace, p.match_on == "code"
        )
        return {
            str(r.raw_value): (str(r.status), int(r.tries), r.next_try_at)
            for r in await self.db.execute(stmt)
        }
