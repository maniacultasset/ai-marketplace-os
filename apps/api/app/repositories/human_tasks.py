"""`human_tasks` for marketplace accounts (DATABASE.md:1338-1355; S15): the one live
`reconnect_account` task per account (`ht_one_live_account`), opened when a refresh is rejected,
applied when the same seller reconnects, cancelled (`superseded`) when the account is revoked.
The 0004 guard allows only `open -> answered|cancelled` and `answered -> applied|cancelled`, so
applying is two steps. Inserts go through Core (the `request_id` column default stamps them)."""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from app.db.models.issues import HumanTask
from app.repositories.base import TenantScopedRepository

RECONNECT_KIND: Final = "reconnect_account"
RECONNECT_TARGET: Final = "account.oauth"
RECONNECT_ANSWER: Final = {"kind": "reconnect"}
RECONNECT_QUESTION: Final = "Reconnect this marketplace account."
RECONNECT_REASON: Final = (
    "The marketplace refused to refresh the connection; publishing to it waits for a reconnect."
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

    async def cancel_reconnect(self, account_id: uuid.UUID) -> int:
        return _rowcount(await self.db.execute(_CANCEL, {"t": self.tenant_id, "a": account_id}))
