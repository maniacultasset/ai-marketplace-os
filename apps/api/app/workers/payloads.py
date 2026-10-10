"""Job payloads v1 of the publish family (S21 CP11; DW8; EXECUTION-PLAN.md:291-293).

`job_runs.payload_json` of a `listing_publish` job is `PublishJobPayload` and of a
`listing_status_sync` job `StatusSyncPayload`. Both ignore unknown keys, so the previous worker
still runs a job the new API enqueued; a change is additive only (a new optional key), and a `v`
other than 1 is refused (`ValueError`), never guessed. The retry paths are computed by the job from
`publish_field_results` (DATABASE.md:887), never carried in the payload.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

PublishMode = Literal["publish", "retry_fields", "resume"]
SyncReason = Literal["scheduled", "refresh", "daily"]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    v: Literal[1] = 1


class PublishJobPayload(_Payload):
    listing_id: UUID
    listing_version_id: UUID
    mode: PublishMode = "publish"


class StatusSyncPayload(_Payload):
    listing_id: UUID
    reason: SyncReason = "scheduled"


def dump(payload: _Payload) -> dict[str, Any]:
    """The JSON stored in `job_runs.payload_json`."""
    return payload.model_dump(mode="json")
